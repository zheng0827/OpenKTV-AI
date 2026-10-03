import csv
import tempfile
import unittest
from pathlib import Path

from core.library import plain_lyrics_from_lrc, stable_song_id, upsert_catalog_entry


class CatalogTests(unittest.TestCase):
    def test_song_id_is_stable_and_normalized(self):
        self.assertEqual(stable_song_id("Artist", "Song"), stable_song_id(" artist ", "song"))
        self.assertNotEqual(stable_song_id("Artist A", "Song"), stable_song_id("Artist B", "Song"))

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


if __name__ == "__main__":
    unittest.main()
