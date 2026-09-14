import unittest

from openktv_ai.lyrics_pipeline.aligner import (
    LyricLine,
    align_lyrics,
    align_gt_lyrics_strict,
    build_text_transform,
    build_word_level_lines_from_segments,
    detect_dialogue,
    parse_lyrics_text,
    should_fallback_to_transcript_sync,
)


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

    def test_align_lyrics_prefix_pending_lines_stay_near_first_anchor(self):
        lines = ["前面未對上", "第二句"]
        segments = [{"start": 20.0, "end": 21.0, "text": "第二句", "words": []}]
        aligned = align_lyrics(lines, segments)
        self.assertEqual([line.text for line in aligned], lines)
        self.assertGreater(aligned[0].start, 0.0)
        self.assertGreaterEqual(aligned[0].start, aligned[1].start - 8.0)
        self.assertLessEqual(aligned[0].end, aligned[1].start)

    def test_detect_dialogue_non_overlap(self):
        aligned = [LyricLine(start=1.0, end=2.0, text="歌詞", words=[])]
        segments = [
            {"start": 1.2, "end": 1.9, "text": "歌詞", "words": []},
            {"start": 2.2, "end": 3.0, "text": "旁白", "words": []},
        ]
        dialogue = detect_dialogue(aligned, segments)
        self.assertEqual(len(dialogue), 1)
        self.assertEqual(dialogue[0]["text"], "旁白")

    def test_build_word_level_lines_from_segments_uses_segment_words(self):
        segments = [
            {
                "start": 0.5,
                "end": 1.5,
                "text": "你好",
                "words": [
                    {"text": "你", "start": 0.5, "end": 1.0},
                    {"text": "好", "start": 1.0, "end": 1.5},
                ],
            }
        ]
        lines = build_word_level_lines_from_segments(segments)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "你好")
        self.assertEqual(len(lines[0].words), 2)

    def test_should_fallback_to_transcript_sync_when_match_ratio_low(self):
        lyrics_lines = ["一", "二", "三", "四"]
        self.assertTrue(should_fallback_to_transcript_sync(lyrics_lines, matched_lines=1))
        self.assertFalse(should_fallback_to_transcript_sync(lyrics_lines, matched_lines=3))

    def test_should_not_fallback_to_low_quality_transcript(self):
        lyrics_lines = ["一", "二", "三", "四"]
        segments = [
            {"start": 0.0, "end": 53.31, "text": "作詞 作曲 編曲 混音 母帶 母帶 母帶 母帶 母帶 母帶", "words": []},
            {"start": 54.0, "end": 102.0, "text": "母帶 母帶 母帶 母帶 母帶 母帶 母帶 母帶", "words": []},
            {"start": 103.0, "end": 130.0, "text": "母帶 母帶 母帶 母帶 母帶", "words": []},
        ]
        self.assertFalse(should_fallback_to_transcript_sync(lyrics_lines, matched_lines=1, segments=segments))

    def test_build_word_level_lines_from_segments_skips_low_quality_transcript(self):
        segments = [
            {"start": 0.0, "end": 50.0, "text": "母帶 母帶 母帶 母帶 母帶 母帶 母帶 母帶", "words": []},
            {"start": 51.0, "end": 52.0, "text": "你好", "words": [{"text": "你", "start": 51.0, "end": 51.5}, {"text": "好", "start": 51.5, "end": 52.0}]},
        ]
        lines = build_word_level_lines_from_segments(segments)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "你好")

    def test_align_gt_lyrics_strict_routes_unreliable_lines_to_dialogue(self):
        transform, _ = build_text_transform("zh")
        lines = ["第一句", "第二句", "錯誤句"]
        segments = [
            {"start": 1.0, "end": 2.0, "text": "第一句", "words": []},
            {"start": 3.0, "end": 4.0, "text": "第二句", "words": []},
        ]
        aligned, matched, filtered = align_gt_lyrics_strict(lines, segments, transform)
        self.assertEqual(matched, 2)
        self.assertEqual([line.text for line in aligned], ["第一句", "第二句"])
        self.assertEqual(filtered[0]["reason"], "not_found")

    def test_build_text_transform_only_converts_chinese_lines(self):
        transform, enabled = build_text_transform("zh")
        self.assertTrue(enabled)
        self.assertIn(transform("发光"), {"发光", "發光"})
        self.assertEqual(transform("hello world"), "hello world")
        self.assertEqual(transform("東京ラブストーリー"), "東京ラブストーリー")


if __name__ == '__main__':
    unittest.main()
