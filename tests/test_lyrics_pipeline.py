import unittest

from openktv_ai.lyrics_pipeline.aligner import LyricLine, align_lyrics, detect_dialogue, parse_lyrics_text


class LyricsPipelineTests(unittest.TestCase):
    def test_parse_lyrics_text_skips_metadata(self):
        text = "@format ktv-lrc\n\n第一句\n$1 2 token\n第二句\n"
        self.assertEqual(parse_lyrics_text(text), ["第一句", "第二句"])

    def test_parse_lyrics_text_strips_lrc_timestamp_and_noise(self):
        text = "[00:01.20]☆☆☆\n[00:03.00] 第一行 \n[00:05.00]第二行"
        self.assertEqual(parse_lyrics_text(text), ["第一行", "第二行"])

    def test_align_lyrics_uses_segment_words(self):
        lines = ["你好"]
        segments = [{"start": 1.0, "end": 2.0, "text": "你好", "words": [{"text": "你", "start": 1.0, "end": 1.4}, {"text": "好", "start": 1.4, "end": 2.0}]}]
        aligned = align_lyrics(lines, segments)
        self.assertEqual(len(aligned), 1)
        self.assertEqual(len(aligned[0].words), 2)

    def test_align_lyrics_keeps_unmatched_lines_in_order(self):
        lines = ["第一句", "中間插入", "第三句"]
        segments = [
            {"start": 1.0, "end": 2.0, "text": "第一句", "words": []},
            {"start": 6.0, "end": 7.0, "text": "第三句", "words": []},
        ]
        aligned = align_lyrics(lines, segments)
        self.assertEqual([line.text for line in aligned], lines)
        self.assertLessEqual(aligned[0].end, aligned[1].start)
        self.assertLessEqual(aligned[1].end, aligned[2].start)

    def test_detect_dialogue_non_overlap(self):
        aligned = [LyricLine(start=1.0, end=2.0, text="歌詞", words=[])]
        segments = [
            {"start": 1.2, "end": 1.9, "text": "歌詞", "words": []},
            {"start": 2.2, "end": 3.0, "text": "旁白", "words": []},
        ]
        dialogue = detect_dialogue(aligned, segments)
        self.assertEqual(len(dialogue), 1)
        self.assertEqual(dialogue[0]["text"], "旁白")


if __name__ == '__main__':
    unittest.main()
