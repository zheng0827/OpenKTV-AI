from __future__ import annotations

import json
import re
import subprocess
import tempfile
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
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    dist = prev[-1]
    return 1.0 - dist / max(len(a), len(b))


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


def transcribe_segments(
    vocals_wav: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str | None,
    lyrics_prompt: str | None = None,
    asr_backend: str = "auto",
) -> tuple[list[dict[str, Any]], str]:
    backend = (asr_backend or "auto").strip().lower()
    if backend in {"auto", "whisperx"}:
        try:
            import whisperx  # pylint: disable=import-outside-toplevel
            print("ewwwwwwwwwwwww")
            model = whisperx.load_model(model_name, device=device, compute_type=compute_type, language=language)
            result = model.transcribe(str(vocals_wav), batch_size=8, language=language)
            out = []
            for seg in result.get("segments", []):
                seg_start = seg.get("start")
                seg_end = seg.get("end")
                if seg_start is None or seg_end is None:
                    continue
                words = []
                for word in seg.get("words", []) or []:
                    word_start = word.get("start")
                    word_end = word.get("end")
                    token = clean_text(str(word.get("word", word.get("text", ""))))
                    if word_start is None or word_end is None or not token:
                        continue
                    words.append({"text": token, "start": float(word_start), "end": float(word_end)})
                out.append({"start": float(seg_start), "end": float(seg_end), "text": clean_text(str(seg.get("text", ""))), "words": words})
            if out:
                detected = str(result.get("language") or language or "zh")
                return out, detected
        except Exception:
            if backend == "whisperx":
                raise

    from faster_whisper import WhisperModel  # pylint: disable=import-outside-toplevel

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(vocals_wav),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        temperature=0.0,
        condition_on_previous_text=True,
        initial_prompt=lyrics_prompt or None,
    )

    out = []
    for seg in segments:
        words = []
        for word in seg.words or []:
            if word.start is None or word.end is None:
                continue
            words.append({"text": clean_text(word.word), "start": float(word.start), "end": float(word.end)})
        out.append({"start": float(seg.start), "end": float(seg.end), "text": clean_text(seg.text), "words": words})
    return out, getattr(info, "language", language or "zh")


def resolve_alignment_python(path: str | None) -> str | None:
    if path:
        candidate = Path(path).expanduser()
        if candidate.exists():
            return str(candidate.resolve())
        return path

    candidates = [
        Path(".venv-whisperx") / "Scripts" / "python.exe",
        Path("venv-whisperx") / "Scripts" / "python.exe",
        Path(".venv-whisperx") / "bin" / "python",
        Path("venv-whisperx") / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return None


def _merge_alignment(base_segments: list[dict[str, Any]], aligned_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = []
    aligned_index = 0
    for base in base_segments:
        updated = dict(base)
        if not clean_text(str(base.get("text", ""))):
            merged.append(updated)
            continue
        if aligned_index >= len(aligned_segments):
            merged.append(updated)
            continue
        aligned = aligned_segments[aligned_index]
        aligned_index += 1
        aligned_words = []
        for item in aligned.get("chars", []):
            text = clean_text(item.get("char", item.get("text", "")))
            start = item.get("start")
            end = item.get("end")
            if text and start is not None and end is not None and float(end) >= float(start):
                aligned_words.append({"text": text, "start": float(start), "end": float(end)})
        if not aligned_words:
            for item in aligned.get("words", []):
                text = clean_text(item.get("word", ""))
                start = item.get("start")
                end = item.get("end")
                if text and start is not None and end is not None and float(end) >= float(start):
                    aligned_words.append({"text": text, "start": float(start), "end": float(end)})
        if aligned_words:
            updated["words"] = aligned_words
            updated["start"] = min(x["start"] for x in aligned_words)
            updated["end"] = max(x["end"] for x in aligned_words)
        merged.append(updated)
    return merged


def apply_forced_alignment(
    wav_path: Path,
    base_segments: list[dict[str, Any]],
    language: str,
    device: str,
    alignment_python: str | None = None,
) -> list[dict[str, Any]]:
    if not base_segments:
        return base_segments

    payload = {
        "wav": str(wav_path),
        "segments": base_segments,
        "language": language,
        "device": "cpu" if device == "cpu" else "cuda",
    }
    alignment_python = resolve_alignment_python(alignment_python)

    if alignment_python:
        with tempfile.TemporaryDirectory() as tmp:
            input_json = Path(tmp) / "align-input.json"
            output_json = Path(tmp) / "align-output.json"
            input_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            command = [
                alignment_python,
                "-m",
                "openktv_ai.lyrics_pipeline.alignment_worker",
                "--alignment-input",
                str(input_json),
                "--alignment-output",
                str(output_json),
            ]
            try:
                completed = subprocess.run(command, check=False, capture_output=True, text=True)
                if completed.returncode == 0 and output_json.exists():
                    try:
                        aligned_segments = json.loads(output_json.read_text(encoding="utf-8"))
                        return _merge_alignment(base_segments, aligned_segments)
                    except Exception:
                        pass
            except Exception:
                pass

    try:
        import whisperx  # pylint: disable=import-outside-toplevel

        align_model, metadata = whisperx.load_align_model(language_code=language, device=payload["device"])
        aligned = whisperx.align(
            base_segments,
            align_model,
            metadata,
            whisperx.load_audio(str(wav_path)),
            payload["device"],
            return_char_alignments=True,
        )
        return _merge_alignment(base_segments, aligned.get("segments", []))
    except Exception:
        return base_segments


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
            best_score = score
            best_idx = idx
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


def _build_line(text: str, start: float, end: float) -> LyricLine:
    safe_end = max(start + 0.001, end)
    return LyricLine(
        start=float(start),
        end=float(safe_end),
        text=clean_text(text),
        words=interpolate_words(text, float(start), float(safe_end)),
    )


def _append_interpolated_lines(out: list[LyricLine], texts: list[str], start: float, end: float) -> None:
    if not texts:
        return
    safe_end = max(start + 0.001 * len(texts), end)
    span = safe_end - start
    for index, text in enumerate(texts):
        line_start = start + span * index / len(texts)
        line_end = start + span * (index + 1) / len(texts)
        out.append(_build_line(text, line_start, line_end))


def _estimate_prefix_start(anchor_start: float, line_count: int) -> float:
    """Place unmatched prefix lines shortly before the first reliable anchor."""
    if line_count <= 0:
        return max(0.0, anchor_start)
    lead_seconds = min(8.0, max(0.8, line_count * 2.5))
    return max(0.0, anchor_start - lead_seconds)


def _is_low_quality_transcript_segment(text: str, start: float, end: float) -> bool:
    tokens = tokenize(text)
    if not tokens:
        return True
    duration = max(0.0, end - start)
    if duration >= 25.0 and len(tokens) <= 5:
        return True
    if duration >= 45.0 and len(tokens) <= 12:
        return True

    unique_ratio = len(set(tokens)) / len(tokens)
    if len(tokens) >= 12 and unique_ratio < 0.3:
        return True

    max_repeat = 1
    repeat = 1
    previous = tokens[0]
    for token in tokens[1:]:
        if token == previous:
            repeat += 1
            max_repeat = max(max_repeat, repeat)
        else:
            previous = token
            repeat = 1
    return max_repeat >= 8


def _segment_is_usable_for_fallback(segment: dict[str, Any]) -> bool:
    text = clean_text(str(segment.get("text", "")))
    start = segment.get("start")
    end = segment.get("end")
    if not text or start is None or end is None:
        return False
    start = float(start)
    end = float(end)
    if end <= start:
        return False
    return not _is_low_quality_transcript_segment(text, start, end)


def _transcript_is_usable_for_fallback(segments: list[dict[str, Any]]) -> bool:
    if not segments:
        return False
    usable = sum(1 for segment in segments if _segment_is_usable_for_fallback(segment))
    return usable >= 3 and usable / len(segments) >= 0.6


def interpolate_words(text: str, start: float, end: float) -> list[dict[str, Any]]:
    pieces = tokenize(text)
    if not pieces:
        return []
    duration = max(0.001, end - start)
    out = []
    for i, token in enumerate(pieces):
        s = start + duration * i / len(pieces)
        e = start + duration * (i + 1) / len(pieces)
        out.append({"text": token, "start": s, "end": e})
    return out


def align_lyrics(lyrics_lines: list[str], segments: list[dict[str, Any]]) -> list[LyricLine]:
    aligned_lines, _ = _align_lyrics_with_match_count(lyrics_lines, segments)
    return aligned_lines


def _align_lyrics_with_match_count(lyrics_lines: list[str], segments: list[dict[str, Any]]) -> tuple[list[LyricLine], int]:
    out: list[LyricLine] = []
    search_idx = 0
    pending_lines: list[str] = []
    previous_end: float | None = None
    has_anchor = False
    matched_count = 0
    for lyric in lyrics_lines:
        idx, score = find_segment(lyric, segments, search_idx)
        if idx is None:
            pending_lines.append(lyric)
            continue
        segment = segments[idx]
        if not _is_reliable_match(lyric, segment.get("text", ""), score):
            pending_lines.append(lyric)
            continue

        if pending_lines:
            anchor_start = float(segment["start"])
            pending_start = previous_end if has_anchor and previous_end is not None else _estimate_prefix_start(anchor_start, len(pending_lines))
            _append_interpolated_lines(out, pending_lines, pending_start, anchor_start)
            pending_lines = []

        words = segment.get("words", [])
        tokens = tokenize(lyric)
        if len(tokens) == len(words) and tokens:
            matched_words = [{"text": token, "start": word["start"], "end": word["end"]} for token, word in zip(tokens, words)]
        else:
            matched_words = interpolate_words(lyric, segment["start"], segment["end"])
        out.append(LyricLine(start=float(segment["start"]), end=float(segment["end"]), text=clean_text(lyric), words=matched_words))
        search_idx = idx + 1
        previous_end = float(segment["end"])
        has_anchor = True
        matched_count += 1

    if pending_lines:
        if has_anchor and previous_end is not None:
            tail_start = previous_end
            tail_end = float(segments[-1]["end"]) if segments else previous_end + max(1.0, len(pending_lines))
        elif segments:
            first_start = float(segments[0]["start"])
            tail_start = _estimate_prefix_start(first_start, len(pending_lines))
            tail_end = max(
                first_start,
                float(segments[-1]["end"]),
                tail_start + 0.001 * len(pending_lines),
            )
        else:
            tail_start = 0.0
            tail_end = max(1.0, len(pending_lines))
        _append_interpolated_lines(out, pending_lines, tail_start, tail_end)
    return out, matched_count


def align_gt_lyrics_strict(
    lyrics_lines: list[str],
    segments: list[dict[str, Any]],
    text_transform,
) -> tuple[list[LyricLine], int, list[dict[str, Any]]]:
    out: list[LyricLine] = []
    filtered_dialogue: list[dict[str, Any]] = []
    search_idx = 0
    matched_count = 0
    cursor = 0.0

    for lyric in lyrics_lines:
        idx, score = find_segment(lyric, segments, search_idx)
        if idx is None:
            filtered_dialogue.append(
                {
                    "source": "lrclib",
                    "reason": "not_found",
                    "text": text_transform(lyric),
                    "start": cursor,
                    "end": cursor + 0.001,
                }
            )
            continue

        segment = segments[idx]
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if idx < search_idx:
            filtered_dialogue.append(
                {
                    "source": "lrclib",
                    "reason": "out_of_order",
                    "text": text_transform(lyric),
                    "start": seg_start,
                    "end": seg_end,
                }
            )
            continue

        if not _is_reliable_match(lyric, segment.get("text", ""), score):
            filtered_dialogue.append(
                {
                    "source": "lrclib",
                    "reason": "low_similarity",
                    "text": text_transform(lyric),
                    "start": seg_start,
                    "end": seg_end,
                    "score": round(float(score), 4),
                }
            )
            cursor = max(cursor, seg_end)
            continue

        words = segment.get("words", [])
        tokens = tokenize(lyric)
        if len(tokens) == len(words) and tokens:
            matched_words = [{"text": text_transform(token), "start": word["start"], "end": word["end"]} for token, word in zip(tokens, words)]
        else:
            matched_words = interpolate_words(text_transform(lyric), seg_start, seg_end)
        out.append(
            LyricLine(
                start=seg_start,
                end=seg_end,
                text=text_transform(lyric),
                words=matched_words,
            )
        )
        search_idx = idx + 1
        matched_count += 1
        cursor = max(cursor, seg_end)
    return out, matched_count, filtered_dialogue


def build_word_level_lines_from_segments(segments: list[dict[str, Any]], text_transform=None) -> list[LyricLine]:
    text_transform = text_transform or clean_text
    lines: list[LyricLine] = []
    for segment in segments:
        if not _segment_is_usable_for_fallback(segment):
            continue
        text = clean_text(str(segment.get("text", "")))
        start = float(segment["start"])
        end = float(segment["end"])
        words = []
        for word in segment.get("words", []):
            token = clean_text(str(word.get("text", word.get("word", ""))))
            token_start = word.get("start")
            token_end = word.get("end")
            if token and token_start is not None and token_end is not None and float(token_end) >= float(token_start):
                words.append({"text": token, "start": float(token_start), "end": float(token_end)})
        if not words or len(tokenize(text)) != len(words):
            words = interpolate_words(text, start, end)
        mapped_words = [{**word, "text": text_transform(str(word.get("text", "")))} for word in words]
        lines.append(LyricLine(start=start, end=end, text=text_transform(text), words=mapped_words))
    return lines


def should_fallback_to_transcript_sync(
    lyrics_lines: list[str],
    matched_lines: int,
    segments: list[dict[str, Any]] | None = None,
) -> bool:
    if not lyrics_lines:
        return False
    ratio = matched_lines / len(lyrics_lines)
    if ratio >= MIN_LYRIC_MATCH_RATIO or len(lyrics_lines) < 3:
        return False
    if segments is None:
        return True
    return _transcript_is_usable_for_fallback(segments)


def detect_dialogue(aligned_lines: list[LyricLine], segments: list[dict[str, Any]], text_transform=None) -> list[dict[str, Any]]:
    text_transform = text_transform or clean_text
    windows = [(line.start, line.end) for line in aligned_lines]
    dialogue = []
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        overlap = any(max(start, ls) < min(end, le) for ls, le in windows)
        if overlap:
            continue
        text = text_transform(clean_text(segment.get("text", "")))
        if text:
            dialogue.append({"start": start, "end": end, "text": text})
    return dialogue


def merge_display_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge punctuation and contraction fragments into displayable words.

    ASR tokenization often emits ``you'`` + ``d`` or punctuation as separate
    words. Those are useful to the aligner but produce visibly distracting
    karaoke flashes, so the display token keeps the combined timing window.
    """
    merged: list[dict[str, Any]] = []
    for source in words:
        text = clean_text(str(source.get("text", "")))
        start = source.get("start")
        end = source.get("end")
        if not text or start is None or end is None:
            continue
        item = {**source, "text": text, "start": float(start), "end": float(end)}
        punctuation_only = not re.search(r"[0-9A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
        contraction_fragment = bool(merged) and (
            merged[-1]["text"].endswith(("'", "\u2019")) or text.startswith(("\u2019", "'"))
        )
        if merged and (punctuation_only or contraction_fragment):
            previous = merged[-1]
            previous["text"] += text
            previous["end"] = max(previous["end"], item["end"])
            continue
        merged.append(item)
    return merged


def refill_dialogue_to_backing(vocals_wav: Path, backing_wav: Path, dialogue: list[dict[str, Any]], output_wav: Path) -> None:
    if not dialogue:
        output_wav.write_bytes(backing_wav.read_bytes())
        return

    segs = [d for d in dialogue if d["end"] > d["start"]]
    if not segs:
        output_wav.write_bytes(backing_wav.read_bytes())
        return

    split_count = len(segs)
    labels = [f"d{i}" for i in range(split_count)]
    parts = [f"[0:a]asplit={split_count}" + "".join(f"[{label}src]" for label in labels) + ";"]
    for idx, seg in enumerate(segs):
        delay = int(max(0.0, seg["start"]) * 1000)
        parts.append(f"[{labels[idx]}src]atrim=start={seg['start']:.3f}:end={seg['end']:.3f},asetpts=PTS-STARTPTS,adelay={delay}|{delay}[{labels[idx]}];")
    mix_inputs = "".join(f"[{label}]" for label in labels)
    parts.append(f"[1:a]{mix_inputs}amix=inputs={split_count + 1}:normalize=0[out]")

    command = [
        "ffmpeg", "-y", "-i", str(vocals_wav), "-i", str(backing_wav),
        "-filter_complex", "".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", str(output_wav),
    ]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def run_lyrics_alignment_pipeline(
    vocals_wav: Path,
    backing_wav: Path,
    output_lrc: Path,
    output_refilled_backing_wav: Path,
    song_name: str,
    singer: str,
    lyrics_text: str,
    whisper_model: str,
    whisper_device: str,
    whisper_compute_type: str,
    whisper_language: str | None,
    alignment_python: str | None,
    output_dialogue_audit_json: Path | None = None,
    asr_backend: str = "auto",
) -> tuple[list[LyricLine], list[dict[str, Any]]]:
    lyrics_lines = parse_lyrics_text(lyrics_text)
    print("\n".join(lyrics_lines))
    segments, detected_language = transcribe_segments(
        vocals_wav=vocals_wav,
        model_name=whisper_model,
        device=whisper_device,
        compute_type=whisper_compute_type,
        language=whisper_language,
        lyrics_prompt="\n".join(lyrics_lines),
        asr_backend=asr_backend,
    )

    text_transform, _chinese_detected = build_text_transform(detected_language or whisper_language or "")
    aligned_segments = apply_forced_alignment(
        wav_path=vocals_wav,
        base_segments=segments,
        language=detected_language or whisper_language or "zh",
        device=whisper_device,
        alignment_python=alignment_python,
    )
    # LRCLIB is the source of lyric text. ASR is used only to locate those lines;
    # never replace trusted lyrics with unverified transcript text.
    aligned_lines = [
        LyricLine(
            start=line.start,
            end=line.end,
            text=text_transform(line.text),
            words=[{**word, "text": text_transform(str(word["text"]))} for word in line.words],
        )
        for line in align_lyrics(lyrics_lines, aligned_segments)
    ]
    dialogue = detect_dialogue(aligned_lines, aligned_segments, text_transform=text_transform)
    dialogue.sort(key=lambda item: (float(item.get("start", 0.0)), float(item.get("end", 0.0))))
    refill_dialogue_to_backing(vocals_wav, backing_wav, dialogue, output_refilled_backing_wav)
    if output_dialogue_audit_json is not None:
        output_dialogue_audit_json.write_text(
            json.dumps(
                {
                    "detected_language": detected_language,
                    "chinese_conversion_enabled": _chinese_detected,
                    "filtered_lyrics_dialogue": [],
                    "dialogue_total": dialogue,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    longest_end = max([line.end for line in aligned_lines], default=0.0)
    write_ktv_lrc(output_lrc, text_transform(song_name), text_transform(singer), longest_end, aligned_lines, dialogue)
    return aligned_lines, dialogue
