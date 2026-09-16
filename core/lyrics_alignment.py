from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LRC_INLINE_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
SYMBOL_NOISE_LINE_RE = re.compile(r"^[^0-9A-Za-z\u3400-\u9fff]+$")
MIN_MATCH_SCORE = 0.3
MIN_LYRIC_MATCH_RATIO = 0.35
_OPENCC = None


@dataclass
class LyricLine:
    start: float
    end: float
    text: str
    words: list[dict[str, Any]]


def clean_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _opencc_convert(text: str) -> str:
    global _OPENCC
    try:
        from opencc import OpenCC  # pylint: disable=import-outside-toplevel

        if _OPENCC is None:
            _OPENCC = OpenCC("s2twp")
        return _OPENCC.convert(text)
    except Exception:
        return text


def _is_han(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff"


def _has_mixed_non_chinese(text: str) -> bool:
    return bool(re.search(r"[A-Za-z\u3040-\u30ff\uac00-\ud7af]", text))


def _looks_chinese_line(text: str) -> bool:
    return any(_is_han(ch) for ch in text) and not _has_mixed_non_chinese(text)


def _convert_simplified_chinese_only(text: str) -> str:
    converted = _opencc_convert(text)
    if converted == text:
        return text
    if len(converted) != len(text):
        return converted
    out = []
    for source, target in zip(text, converted):
        if source == target:
            out.append(source)
        elif _is_han(source) and _is_han(target):
            out.append(target)
        else:
            out.append(source)
    return "".join(out)


def build_text_transform(language: str) -> tuple[Callable[[str], str], bool]:
    lang = (language or "").lower()
    enable = lang.startswith("zh") or lang in {"cmn", "yue", "wuu"}

    def transform(text: str) -> str:
        value = clean_text(text)
        if not enable or not value:
            return value
        if not _looks_chinese_line(value):
            return value
        return _convert_simplified_chinese_only(value)

    return transform, enable


def tokenize(text: str) -> list[str]:
    text = clean_text(text)
    out: list[str] = []
    buffer = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if buffer:
                out.append(buffer)
                buffer = ""
            out.append(ch)
        elif ch.isalnum() or ch in {"'", "-"}:
            buffer += ch
        else:
            if buffer:
                out.append(buffer)
                buffer = ""
    if buffer:
        out.append(buffer)
    return out


def normalize_match(text: str) -> str:
    value = _convert_simplified_chinese_only(clean_text(text)).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[^\w\u3400-\u9fff]", "", value)
    return value


def similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for index, ca in enumerate(a, 1):
        cur = [index]
        for inner, cb in enumerate(b, 1):
            cur.append(min(cur[inner - 1] + 1, prev[inner] + 1, prev[inner - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


def parse_lyrics_text(lyrics_text: str) -> list[str]:
    lines: list[str] = []
    section = ""
    for raw in (lyrics_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("@"):
            if line.startswith("@dialogue"):
                section = "dialogue"
            elif line.startswith("@lyric "):
                section = "lyric"
            continue
        if section == "dialogue" or line.startswith("$"):
            continue
        if line.startswith(("%", "&")):
            if section != "lyric":
                continue
            line = re.sub(r"^[%&]\s*\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s*", "", line)
        line = LRC_INLINE_TIMESTAMP_RE.sub("", line).strip()
        if not line:
            continue
        line = clean_text(line)
        if line and not SYMBOL_NOISE_LINE_RE.fullmatch(line):
            lines.append(line)
    return lines


def interpolate_words(text: str, start: float, end: float) -> list[dict[str, Any]]:
    pieces = tokenize(text)
    if not pieces:
        return []
    duration = max(0.001, end - start)
    return [
        {"text": token, "start": start + duration * index / len(pieces), "end": start + duration * (index + 1) / len(pieces)}
        for index, token in enumerate(pieces)
    ]


def find_segment(lyric_line: str, segments: list[dict[str, Any]], start_index: int) -> tuple[int | None, float]:
    target = normalize_match(lyric_line)
    best_idx = None
    best_score = -1.0
    for idx in range(start_index, min(len(segments), start_index + 20)):
        candidate = normalize_match(segments[idx].get("text", ""))
        if not candidate:
            continue
        score = similarity(target, candidate)
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx, best_score


def _is_reliable_match(lyric_line: str, segment_text: str, score: float) -> bool:
    if score >= MIN_MATCH_SCORE:
        return True
    target = normalize_match(lyric_line)
    candidate = normalize_match(segment_text)
    if not target or not candidate:
        return False
    shorter = min(len(target), len(candidate))
    return shorter >= 3 and (target in candidate or candidate in target)


def align_gt_lyrics_strict(lyrics_lines: list[str], segments: list[dict[str, Any]], text_transform) -> tuple[list[LyricLine], int, list[dict[str, Any]]]:
    out: list[LyricLine] = []
    filtered_dialogue: list[dict[str, Any]] = []
    search_idx = 0
    matched_count = 0
    cursor = 0.0
    for lyric in lyrics_lines:
        idx, score = find_segment(lyric, segments, search_idx)
        if idx is None:
            filtered_dialogue.append({"source": "lrclib", "reason": "not_found", "text": text_transform(lyric), "start": cursor, "end": cursor + 0.001})
            continue
        segment = segments[idx]
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if not _is_reliable_match(lyric, segment.get("text", ""), score):
            filtered_dialogue.append({"source": "lrclib", "reason": "low_similarity", "text": text_transform(lyric), "start": seg_start, "end": seg_end, "score": round(float(score), 4)})
            cursor = max(cursor, seg_end)
            continue
        words = segment.get("words", [])
        tokens = tokenize(lyric)
        if len(tokens) == len(words) and tokens:
            matched_words = [{"text": text_transform(token), "start": word["start"], "end": word["end"]} for token, word in zip(tokens, words)]
        else:
            matched_words = interpolate_words(text_transform(lyric), seg_start, seg_end)
        out.append(LyricLine(start=seg_start, end=seg_end, text=text_transform(lyric), words=matched_words))
        search_idx = idx + 1
        matched_count += 1
        cursor = max(cursor, seg_end)
    return out, matched_count, filtered_dialogue


def build_word_level_lines_from_segments(segments: list[dict[str, Any]], text_transform=None) -> list[LyricLine]:
    text_transform = text_transform or clean_text
    lines: list[LyricLine] = []
    for segment in segments:
        text = clean_text(str(segment.get("text", "")))
        start = segment.get("start")
        end = segment.get("end")
        if not text or start is None or end is None or float(end) <= float(start):
            continue
        words = []
        for word in segment.get("words", []):
            token = clean_text(str(word.get("text", word.get("word", ""))))
            token_start = word.get("start")
            token_end = word.get("end")
            if token and token_start is not None and token_end is not None and float(token_end) >= float(token_start):
                words.append({"text": token, "start": float(token_start), "end": float(token_end)})
        if not words or len(tokenize(text)) != len(words):
            words = interpolate_words(text, float(start), float(end))
        lines.append(LyricLine(start=float(start), end=float(end), text=text_transform(text), words=[{**word, "text": text_transform(str(word["text"]))} for word in words]))
    return lines


def should_fallback_to_transcript_sync(lyrics_lines: list[str], matched_lines: int, segments: list[dict[str, Any]] | None = None) -> bool:
    if not lyrics_lines:
        return False
    ratio = matched_lines / len(lyrics_lines)
    if ratio >= MIN_LYRIC_MATCH_RATIO or len(lyrics_lines) < 3:
        return False
    return bool(segments)


def detect_dialogue(aligned_lines: list[LyricLine], segments: list[dict[str, Any]], text_transform=None) -> list[dict[str, Any]]:
    text_transform = text_transform or clean_text
    windows = [(line.start, line.end) for line in aligned_lines]
    dialogue = []
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if any(max(start, line_start) < min(end, line_end) for line_start, line_end in windows):
            continue
        text = text_transform(clean_text(segment.get("text", "")))
        if text:
            dialogue.append({"start": start, "end": end, "text": text})
    return dialogue


def merge_display_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source in words:
        text = clean_text(str(source.get("text", "")))
        start = source.get("start")
        end = source.get("end")
        if not text or start is None or end is None:
            continue
        item = {**source, "text": text, "start": float(start), "end": float(end)}
        punctuation_only = not re.search(r"[0-9A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
        contraction_fragment = bool(merged) and (merged[-1]["text"].endswith(("'", "\u2019")) or text.startswith(("\u2019", "'")))
        if merged and (punctuation_only or contraction_fragment):
            merged[-1]["text"] += text
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            continue
        merged.append(item)
    return merged


def write_ktv_lrc(output: Path, song_name: str, singer: str, duration_seconds: float, aligned_lines: list[LyricLine], dialogue: list[dict[str, Any]]) -> None:
    with output.open("w", encoding="utf-8", newline="\n") as file:
        file.write("@format ktv-lrc\n@version 1\n\n@id 000000\n\n")
        file.write(f"@song_name {clean_text(song_name)}, zh-tw\n")
        file.write(f"@singer {clean_text(singer or 'Unknown')}, zh-tw\n")
        file.write(f"@duration {int(max(0, duration_seconds))}\n\n")
        file.write("@lyrics zh-tw\n@lyric_synced word\n\n@lyric zh-tw\n")
        for line in aligned_lines:
            file.write(f"%{line.start:.3f} {line.end:.3f} {line.text}\n")
            for word in merge_display_words(line.words):
                file.write(f"${word['start']:.3f} {word['end']:.3f} {clean_text(str(word['text']))}\n")
            file.write("\n")
        file.write("@dialogue\n")
        for item in dialogue:
            file.write(f"&{item['start']:.3f} {item['end']:.3f} {item['text']}\n")


def run_alignment_workflow(
    lyrics_text: str,
    transcript_segments: list[dict[str, Any]],
    detected_language: str,
    output_lrc: Path,
    output_dialogue_audit_json: Path | None,
    song_name: str,
    singer: str,
) -> tuple[list[LyricLine], list[dict[str, Any]]]:
    lyrics_lines = parse_lyrics_text(lyrics_text)
    text_transform, chinese_detected = build_text_transform(detected_language)
    aligned_lines, matched_lines, filtered_lyrics_dialogue = align_gt_lyrics_strict(lyrics_lines, transcript_segments, text_transform)
    if should_fallback_to_transcript_sync(lyrics_lines, matched_lines, transcript_segments):
        aligned_lines = build_word_level_lines_from_segments(transcript_segments, text_transform=text_transform)
    dialogue = detect_dialogue(aligned_lines, transcript_segments, text_transform=text_transform)
    dialogue.sort(key=lambda item: (float(item.get("start", 0.0)), float(item.get("end", 0.0))))
    if output_dialogue_audit_json is not None:
        output_dialogue_audit_json.write_text(
            json.dumps({
                "detected_language": detected_language,
                "chinese_conversion_enabled": chinese_detected,
                "filtered_lyrics_dialogue": filtered_lyrics_dialogue,
                "dialogue_total": dialogue,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    longest_end = max([line.end for line in aligned_lines], default=0.0)
    write_ktv_lrc(output_lrc, text_transform(song_name), text_transform(singer), longest_end, aligned_lines, dialogue)
    return aligned_lines, dialogue
