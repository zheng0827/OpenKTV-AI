from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LRC_INLINE_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
SYMBOL_NOISE_LINE_RE = re.compile(r"^[^0-9A-Za-z\u3400-\u9fff]+$")
MIN_MATCH_SCORE = 0.72


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


def traditional(text: str) -> str:
    try:
        from opencc import OpenCC  # pylint: disable=import-outside-toplevel

        return OpenCC("s2twp").convert(text)
    except Exception:
        return text


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
    value = traditional(text).lower()
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
    for raw in (lyrics_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("@") or line.startswith(("%", "$", "&")):
            continue
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
) -> tuple[list[dict[str, Any]], str]:
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


def _merge_alignment(base_segments: list[dict[str, Any]], aligned_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = []
    for base, aligned in zip(base_segments, aligned_segments):
        updated = dict(base)
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
    payload = {
        "wav": str(wav_path),
        "segments": base_segments,
        "language": language,
        "device": "cpu" if device == "cpu" else "cuda",
    }

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
        pass

    if not alignment_python:
        return base_segments

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
        subprocess.run(command, check=False, capture_output=True, text=True)
        if not output_json.exists():
            return base_segments
        try:
            aligned_segments = json.loads(output_json.read_text(encoding="utf-8"))
        except Exception:
            return base_segments
        return _merge_alignment(base_segments, aligned_segments)


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
        text=traditional(text),
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
    out: list[LyricLine] = []
    search_idx = 0
    pending_lines: list[str] = []
    previous_end: float | None = None
    has_anchor = False
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
        out.append(LyricLine(start=float(segment["start"]), end=float(segment["end"]), text=traditional(lyric), words=matched_words))
        search_idx = idx + 1
        previous_end = float(segment["end"])
        has_anchor = True

    if pending_lines:
        if has_anchor and previous_end is not None:
            tail_start = previous_end
            tail_end = float(segments[-1]["end"]) if segments else previous_end + max(1.0, len(pending_lines))
        elif segments:
            first_start = float(segments[0]["start"])
            tail_start = _estimate_prefix_start(first_start, len(pending_lines))
            tail_end = max(first_start, tail_start + 0.001 * len(pending_lines))
        else:
            tail_start = 0.0
            tail_end = max(1.0, len(pending_lines))
        _append_interpolated_lines(out, pending_lines, tail_start, tail_end)
    return out


def detect_dialogue(aligned_lines: list[LyricLine], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = [(line.start, line.end) for line in aligned_lines]
    dialogue = []
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        overlap = any(max(start, ls) < min(end, le) for ls, le in windows)
        if overlap:
            continue
        text = traditional(clean_text(segment.get("text", "")))
        if text:
            dialogue.append({"start": start, "end": end, "text": text})
    return dialogue


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
        file.write(f"@song_name {traditional(song_name)}, zh-tw\n")
        file.write(f"@singer {traditional(singer or 'Unknown')}, zh-tw\n")
        file.write(f"@duration {int(max(0, duration_seconds))}\n\n")
        file.write("@lyrics zh-tw\n@lyric_synced word\n\n@lyric zh-tw\n")

        for line in aligned_lines:
            file.write(f"%{line.start:.3f} {line.end:.3f} {line.text}\n")
            for word in line.words:
                file.write(f"${word['start']:.3f} {word['end']:.3f} {traditional(word['text'])}\n")
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
) -> tuple[list[LyricLine], list[dict[str, Any]]]:
    lyrics_lines = parse_lyrics_text(lyrics_text)
    if not lyrics_lines:
        lyrics_lines = [song_name]

    segments, detected_language = transcribe_segments(
        vocals_wav=vocals_wav,
        model_name=whisper_model,
        device=whisper_device,
        compute_type=whisper_compute_type,
        language=whisper_language,
    )
    aligned_segments = apply_forced_alignment(
        wav_path=vocals_wav,
        base_segments=segments,
        language=detected_language or whisper_language or "zh",
        device=whisper_device,
        alignment_python=alignment_python,
    )
    aligned_lines = align_lyrics(lyrics_lines, aligned_segments)
    dialogue = detect_dialogue(aligned_lines, aligned_segments)
    refill_dialogue_to_backing(vocals_wav, backing_wav, dialogue, output_refilled_backing_wav)

    longest_end = max([line.end for line in aligned_lines], default=0.0)
    write_ktv_lrc(output_lrc, song_name, singer, longest_end, aligned_lines, dialogue)
    return aligned_lines, dialogue
