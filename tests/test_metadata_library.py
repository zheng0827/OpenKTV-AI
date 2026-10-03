import csv
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import ensure_processing_api_tokens, load_settings
from core.library import update_library_index
from core.metadata import match_spotify_track, normalize_youtube_title, parse_youtube_title, safe_filename
from scripts.process_urls import read_rows


class MetadataTests(unittest.TestCase):
    def test_normalizes_youtube_title_and_strips_promotional_suffix(self):
        parsed = parse_youtube_title("Ａｒｔｉｓｔ - Song [Official Music Video]", "Channel")
        self.assertEqual(parsed["artist"], "Artist")
        self.assertEqual(parsed["song"], "Song")
        self.assertEqual(normalize_youtube_title("Song (Karaoke Version)"), "Song")
        self.assertEqual(safe_filename('Artist: Song/Title?'), "Artist SongTitle")

    def test_spotify_match_prefers_original_over_cover(self):
        tracks = [
            {
                "name": "Song (Cover)",
                "artists": [{"id": "cover-artist", "name": "Artist"}],
                "album": {"name": "Cover Singles", "release_date": "2020"},
                "duration_ms": 200000,
            },
            {
                "name": "Song",
                "artists": [{"id": "original-artist", "name": "Artist"}],
                "album": {"name": "Album", "release_date": "1999-01-01"},
                "duration_ms": 180000,
                "id": "track-id",
                "external_urls": {"spotify": "https://open.spotify.com/track/track-id"},
            },
        ]
        with patch("core.metadata._spotify_access_token", return_value="test-token"), patch(
            "core.metadata._spotify_request",
            side_effect=[
                {"tracks": {"items": tracks}},
                {"genres": ["pop"]},
            ],
        ):
            result = match_spotify_track("Song", "Artist", minimum_score=0.72)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["track"]["id"], "track-id")
        self.assertEqual(result["track"]["release_year"], "1999")


class LibraryTests(unittest.TestCase):
    def test_writes_complete_csv_and_flattens_ktv_lyrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            songs_dir = Path(temporary) / "songs"
            songs_dir.mkdir()
            (songs_dir / "Artist - Song.mp4").write_bytes(b"video")
            (songs_dir / "Artist - Song.instrumental.m4a").write_bytes(b"audio")
            (songs_dir / "Artist - Song.lrc").write_text(
                "@format ktv-lrc\n@lyrics zh-tw\n@lyric_synced word\n@lyric zh-tw\n"
                "%1.000 2.000 歌詞\n$1.000 1.200 歌\n$1.200 2.000 詞\n",
                encoding="utf-8",
            )
            output = Path(temporary) / "library.csv"
            update_library_index(
                songs_dir,
                output,
                {
                    "Artist - Song.mp4": {
                        "spotify_track_id": "spotify-id",
                        "artist_name": "Artist",
                        "song_name": "Song",
                        "album": "Album",
                        "genre": "pop",
                        "source_url": "https://youtu.be/video-id",
                        "separator_mode": "hybrid",
                        "lyrics_alignment_model": "ctc",
                    }
                },
            )
            with output.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["artist_name"], "Artist")
            self.assertEqual(row["lyrics_plain_text"], "歌詞")
            self.assertEqual(row["accompaniment_filename"], "Artist - Song.instrumental.m4a")
            self.assertTrue(row["id"].startswith("spotify-id-"))
            self.assertEqual(row["separator_mode"], "hybrid")


class ProcessingApiTests(unittest.TestCase):
    def test_job_api_auth_validation_and_concurrency_limit(self):
        token = "test-token-that-is-long-enough-for-the-job-api"
        pipeline_stub = types.ModuleType("core.unified_nightingale")
        pipeline_stub.KTVProcessor = object
        with patch.dict(os.environ, {"KTV_JOB_API_TOKEN": token}):
            with patch.dict(sys.modules, {"core.unified_nightingale": pipeline_stub}):
                from core.web import create_app

                app, _, _ = create_app()
                client = app.test_client()
                self.assertEqual(client.get("/api/jobs/not-a-job").status_code, 401)
                headers = {"Authorization": "Bearer " + token}
                self.assertEqual(
                    client.post("/api/jobs", json={"url": "https://example.com/video"}, headers=headers).status_code,
                    400,
                )
                with patch("core.web.threading.Thread") as thread:
                    accepted = client.post(
                        "/api/jobs",
                        json={"url": "https://www.youtube.com/watch?v=video-id"},
                        headers=headers,
                    )
                    self.assertEqual(accepted.status_code, 202)
                    thread.return_value.start.assert_called_once()
                    self.assertEqual(
                        client.post(
                            "/api/jobs",
                            json={"url": "https://www.youtube.com/watch?v=another-id"},
                            headers=headers,
                        ).status_code,
                        429,
                    )


class BatchListTests(unittest.TestCase):
    def test_reads_rows_and_corrects_common_hybrid_typo(self):
        with tempfile.TemporaryDirectory() as temporary:
            urls_file = Path(temporary) / "urls.txt"
            urls_file.write_text(
                    "url,separation_mode,alignment_mode\n"
                    "https://youtu.be/abc123,hybird,whisperx\n"
                    "# ignored comment\n"
                    "https://www.youtube.com/watch?v=def456,uvr,qwen\n",
                    encoding="utf-8",
            )
            self.assertEqual(
                    list(read_rows(urls_file)),
                    [
                        (2, "https://youtu.be/abc123", "hybrid", "whisperx"),
                        (4, "https://www.youtube.com/watch?v=def456", "uvr", "qwen"),
                    ],
            )

    def test_rejects_invalid_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            urls_file = Path(temporary) / "urls.txt"
            urls_file.write_text("https://youtu.be/abc123,bad,ctc\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "分離模式"):
                    list(read_rows(urls_file))

    def test_rejects_lookalike_youtube_hostname(self):
        with tempfile.TemporaryDirectory() as temporary:
            urls_file = Path(temporary) / "urls.txt"
            urls_file.write_text("https://www.youtube.com.evil.test/watch?v=x,hybrid,ctc\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "YouTube HTTPS URL"):
                    list(read_rows(urls_file))


class ConfigurationTests(unittest.TestCase):
    def test_normalizes_hybird_environment_typo(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                    os.environ,
                    {
                        "KTV_CONFIG_PATH": str(Path(temporary) / "missing-config.yaml"),
                        "KTV_SEPARATOR_BACKEND": "hybird",
                    },
            ):
                    self.assertEqual(load_settings(Path(temporary)).separator_backend, "hybrid")

    def test_creates_ephemeral_matching_processing_tokens_when_invalid(self):
        environment = {
            "KTV_JOB_API_TOKEN": "too-short",
            "KTV_PROCESSING_API_TOKEN": "also-too-short",
        }
        self.assertTrue(ensure_processing_api_tokens(environment))
        self.assertEqual(environment["KTV_JOB_API_TOKEN"], environment["KTV_PROCESSING_API_TOKEN"])
        self.assertGreaterEqual(len(environment["KTV_JOB_API_TOKEN"]), 32)
        self.assertFalse(ensure_processing_api_tokens(environment))


if __name__ == "__main__":
    unittest.main()
