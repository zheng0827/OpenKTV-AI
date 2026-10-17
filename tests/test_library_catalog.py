import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.library import CATALOG_FIELDS, delete_catalog_entry, plain_lyrics_from_lrc, stable_song_id, update_catalog_entry, upsert_catalog_entry
from core.metadata import extract_youtube_metadata, parse_youtube_title
from core.config import load_settings
from core.web import create_app


class CatalogTests(unittest.TestCase):
    def test_processing_service_health_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            app, _socketio, _settings = create_app(load_settings(Path(directory)))
            response = app.test_client().get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True, "service": "python"})

    def test_song_id_is_stable_and_normalized(self):
        self.assertEqual(stable_song_id("Artist", "Song"), stable_song_id(" artist ", "song"))
        self.assertNotEqual(stable_song_id("Artist A", "Song"), stable_song_id("Artist B", "Song"))

    def test_youtube_title_parser_removes_release_noise(self):
        parsed = parse_youtube_title("歌手 - 歌名 (Official Music Video)", "頻道")
        self.assertEqual(parsed["artist"], "歌手")
        self.assertEqual(parsed["title"], "歌名")

    def test_youtube_metadata_rejects_non_youtube_urls(self):
        with self.assertRaises(ValueError):
            extract_youtube_metadata("http://127.0.0.1/file.mp4")

    def test_plain_lyrics_drops_ktv_markers_without_repeating_words(self):
        with tempfile.TemporaryDirectory() as directory:
            lyrics = Path(directory) / "song.lrc"
            lyrics.write_text(
                "@format ktv-lrc\n@lyrics zh-tw\n%0.000 2.000 你好\n"
                "$0.000 1.000 你\n$1.000 2.000 好\n",
                encoding="utf-8",
            )
            self.assertEqual(plain_lyrics_from_lrc(lyrics), "你好")

    def test_catalog_upsert_deduplicates_and_csv_escapes(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "library.csv"
            upsert_catalog_entry(catalog, {"artist": "歌手", "title": "歌名", "album": "專輯, \"一\"", "lyrics": "第一句 第二句"})
            upsert_catalog_entry(catalog, {"artist": "歌手", "title": "歌名", "lyrics": ""})
            with catalog.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["album"], "專輯, \"一\"")
            self.assertEqual(rows[0]["lyrics"], "第一句 第二句")
            self.assertTrue(rows[0]["id"])

    def test_catalog_edits_and_deletes_keep_the_fixed_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "library.csv"
            upsert_catalog_entry(catalog, {"artist": "歌手", "title": "歌名", "album": "專輯"})
            with catalog.open(encoding="utf-8", newline="") as handle:
                song_id = next(csv.DictReader(handle))["id"]

            updated = update_catalog_entry(catalog, song_id, {"title": "新歌名", "genre": "流行", "invalid": "ignored"})
            self.assertEqual(updated["title"], "新歌名")
            with catalog.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                row = next(reader)
                self.assertEqual(reader.fieldnames, CATALOG_FIELDS)
                self.assertEqual(row["genre"], "流行")
                self.assertEqual(row["album"], "專輯")

            deleted = delete_catalog_entry(catalog, song_id)
            self.assertEqual(deleted["title"], "新歌名")
            with catalog.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_catalog_edit_endpoint_requires_token_and_preserves_schema(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KTV_LIBRARY_ADMIN_TOKEN": "test-admin-token"}):
            settings = load_settings(Path(directory))
            settings.songs_dir.mkdir(parents=True, exist_ok=True)
            upsert_catalog_entry(settings.library_index_path, {"artist": "歌手", "title": "歌名"})
            app, _socketio, _settings = create_app(settings)
            client = app.test_client()
            with settings.library_index_path.open(encoding="utf-8", newline="") as handle:
                song_id = next(csv.DictReader(handle))["id"]

            self.assertEqual(client.patch(f"/api/catalog/{song_id}", json={"title": "更新"}).status_code, 401)
            response = client.patch(
                f"/api/catalog/{song_id}",
                json={"title": "更新"},
                headers={"Authorization": "******"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["song"]["title"], "更新")
            self.assertEqual(
                client.delete(
                    f"/api/catalog/{song_id}",
                    headers={"Authorization": "******"},
                ).status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
