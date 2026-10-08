import json
import tempfile
import unittest
from pathlib import Path

from core.library import fetch_lrclib_lyrics_by_url
from script.download_songs import read_input_jobs


class DownloadInputTests(unittest.TestCase):
    def test_pipe_delimited_txt_supports_per_song_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "urls.txt"
            source.write_text(
                "youtube_url|lrclib_url|sparate mode|text aligan model|title|artist|stems|device\n"
                "https://youtu.be/abc|https://lrclib.net/api/get/42|uvr|whisperx|Song|Artist|2|cpu\n",
                encoding="utf-8",
            )
            self.assertEqual(read_input_jobs(source), [{
                "youtube_url": "https://youtu.be/abc",
                "lrclib_url": "https://lrclib.net/api/get/42",
                "separator_mode": "uvr",
                "alignment_model": "whisperx",
                "title": "Song",
                "artist": "Artist",
                "stems": "2",
                "device": "cpu",
            }])

    def test_legacy_url_lines_and_json_inputs_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            text_file = Path(directory) / "urls.txt"
            text_file.write_text("# comment\n\nhttps://youtu.be/one\nhttps://youtu.be/two\n", encoding="utf-8")
            self.assertEqual(
                read_input_jobs(text_file),
                [{"youtube_url": "https://youtu.be/one"}, {"youtube_url": "https://youtu.be/two"}],
            )
            json_file = Path(directory) / "songs.json"
            json_file.write_text(json.dumps([{"yt_url": "https://youtu.be/one", "separator": "hybrid"}]), encoding="utf-8")
            self.assertEqual(
                read_input_jobs(json_file),
                [{"youtube_url": "https://youtu.be/one", "separator_mode": "hybrid"}],
            )

    def test_csv_and_jsonl_inputs_support_quoted_values(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_file = Path(directory) / "songs.csv"
            csv_file.write_text(
                'yt_url,lyrics_url,separator,text_alignment_model,title\n'
                'https://youtu.be/abc,https://lrclib.net/api/get/42,demucs,ctc,"Title, with comma"\n',
                encoding="utf-8",
            )
            self.assertEqual(read_input_jobs(csv_file), [{
                "youtube_url": "https://youtu.be/abc",
                "lrclib_url": "https://lrclib.net/api/get/42",
                "separator_mode": "demucs",
                "alignment_model": "ctc",
                "title": "Title, with comma",
            }])
            jsonl_file = Path(directory) / "songs.jsonl"
            jsonl_file.write_text(
                '{"youtube_url":"https://youtu.be/abc","alignment":"qwen"}\n',
                encoding="utf-8",
            )
            self.assertEqual(read_input_jobs(jsonl_file), [{
                "youtube_url": "https://youtu.be/abc",
                "alignment_model": "qwen",
            }])

    def test_lrclib_url_rejects_untrusted_hosts(self):
        with self.assertRaises(ValueError):
            fetch_lrclib_lyrics_by_url("https://example.com/api/get/1")


if __name__ == "__main__":
    unittest.main()
