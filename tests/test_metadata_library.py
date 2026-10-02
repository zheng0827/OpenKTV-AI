import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.library import update_library_index
from core.metadata import match_spotify_track, normalize_youtube_title, parse_youtube_title, safe_filename


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


if __name__ == "__main__":
    unittest.main()
