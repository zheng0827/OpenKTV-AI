from __future__ import annotations

from pathlib import Path
from typing import Any

from . import cjk
from .audio import detect_vocal_region, highpass_filter, normalize_rms
from .gpu import gpu_model
from .language_detection import detect_language_multiwindow
from .qwen_alignment import QwenUnsupportedError, is_supported as qwen_supported, qwen_align_with_cpu_fallback
from .runtime import align_device_for
from .whisper_alignment import align_with_backend


def clean_text(text: str) -> str:
    import re
    import unicodedata

    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


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
        for item in aligned.get("words", []):
            text = clean_text(item.get("word", item.get("text", "")))
            start = item.get("start")
            end = item.get("end")
            if text and start is not None and end is not None and float(end) >= float(start):
                aligned_words.append({"text": text, "word": text, "start": float(start), "end": float(end)})
        if not aligned_words:
            for item in aligned.get("chars", []):
                text = clean_text(item.get("char", item.get("text", "")))
                start = item.get("start")
                end = item.get("end")
                if text and start is not None and end is not None and float(end) >= float(start):
                    aligned_words.append({"text": text, "word": text, "start": float(start), "end": float(end)})
        if aligned_words:
            updated["words"] = aligned_words
            updated["start"] = min(item["start"] for item in aligned_words)
            updated["end"] = max(item["end"] for item in aligned_words)
        merged.append(updated)
    return merged


def _whisper_asr_options() -> dict[str, Any]:
    return {
        "beam_size": 5,
        "initial_prompt": (
            "Everything before and including GO is INSTRUCTIONS. DON'T INCLUDE IN TRANSCRIPT. "
            "Song Lyrics transcript. Split lines with punctuation. "
            "No annotations or descriptions. GO"
        ),
        "temperatures": [0],
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 10,
        "log_prob_threshold": -10.0,
        "no_speech_threshold": 0.0,
        "repetition_penalty": 1,
        "no_repeat_ngram_size": 0,
        "suppress_blank": False,
    }


def _looks_like_hallucination(segment: dict[str, Any]) -> bool:
    text = clean_text(str(segment.get("text", "")))
    if not text:
        return True
    duration = float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
    if duration <= 0:
        return True
    words = text.split()
    if len(words) >= 6 and len(set(words)) == 1:
        return True
    if float(segment.get("no_speech_prob", 0.0) or 0.0) >= 0.95:
        return True
    return False


def _filter_hallucinations(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = [segment for segment in segments if not _looks_like_hallucination(segment)]
    removed = len(segments) - len(kept)
    if removed:
        print(f"[core:transcribe] removed {removed} hallucinated/invalid segments", flush=True)
    return kept


def _interpolate_missing_words(text: str, start: float, end: float, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = text.split() if " " in text else list(text)
    if not tokens:
        return words
    duration = max(0.001, end - start)
    timed = [word for word in words if word.get("start") is not None and word.get("end") is not None]
    if len(timed) == len(tokens):
        return words
    return [
        {
            "text": token,
            "word": token,
            "start": round(start + duration * index / len(tokens), 3),
            "end": round(start + duration * (index + 1) / len(tokens), 3),
            "estimated": True,
        }
        for index, token in enumerate(tokens)
    ]


def _retokenize_cjk(
    original_segments: list[dict[str, Any]],
    aligned_segments: list[dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for original, aligned in zip(original_segments, aligned_segments):
        text = clean_text(str(original.get("text", "")))
        token_pairs = cjk.tokenize_for_alignment(text, language)
        if not token_pairs:
            continue
        chars = [
            {
                "char": item.get("char", item.get("word", "")),
                "start": item.get("start"),
                "end": item.get("end"),
                "score": item.get("score"),
            }
            for item in aligned.get("chars", [])
        ]
        if not chars:
            chars = aligned.get("words", [])
        surfaces = [surface for surface, _ in token_pairs]
        lengths = [len(reading) for _, reading in token_pairs]
        entries = cjk.attribute_chars_to_tokens(
            surfaces,
            chars,
            fallback_start=aligned.get("start", original.get("start")),
            fallback_end=aligned.get("end", original.get("end")),
            cleaned_lengths=lengths,
        )
        entries = cjk.merge_punct(entries)
        valid = [entry for entry in entries if entry.get("start") is not None and entry.get("end") is not None]
        if not valid:
            continue
        cjk.attach_reading(valid, language)
        output.append({
            "text": text,
            "start": float(valid[0]["start"]),
            "end": float(valid[-1]["end"]),
            "words": [
                {
                    "text": item["word"],
                    "word": item["word"],
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    **({"reading": item["reading"]} if item.get("reading") else {}),
                }
                for item in valid
            ],
        })
    return output


def transcribe_segments(
    vocals_wav: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str | None,
    alignment_backend: str,
) -> tuple[list[dict[str, Any]], str]:
    import whisperx  # pylint: disable=import-outside-toplevel

    transcription_device = align_device_for(device)
    full_audio = whisperx.load_audio(str(vocals_wav))
    vocal_start, vocal_end = detect_vocal_region(full_audio)
    start_sample = int(vocal_start * 16000)
    end_sample = int(vocal_end * 16000)
    audio = full_audio[start_sample:end_sample]
    audio = normalize_rms(highpass_filter(audio))
    with gpu_model(f"whisperx:{model_name}:{transcription_device}") as held:
        model = whisperx.load_model(
            model_name,
            device=transcription_device,
            compute_type=compute_type,
            task="transcribe",
            language=language,
            asr_options=_whisper_asr_options(),
        )
        held.append(model)
        detected_language = language or detect_language_multiwindow(model, audio)
        result = model.transcribe(
            audio,
            batch_size=16,
            task="transcribe",
            language=detected_language,
            chunk_size=30
        )
    
    raw_segments = _filter_hallucinations(result.get("segments", []))
    base_segments = []
    for seg in raw_segments:
        seg_start = seg.get("start")
        seg_end = seg.get("end")
        if seg_start is None or seg_end is None:
            continue
        base_segments.append({
            "start": float(seg_start),
            "end": float(seg_end),
            "text": clean_text(str(seg.get("text", ""))),
            "words": [],
        })

    if not base_segments:
        return [], detected_language

    aligned_payload = None
    if alignment_backend == "qwen" and qwen_supported(detected_language):
        try:
            aligned_payload = qwen_align_with_cpu_fallback(base_segments, audio, detected_language)
        except QwenUnsupportedError:
            print(f"[core:align] qwen backend unsupported for language={detected_language}, fallback to wav2vec2", flush=True)
            aligned_payload = None
        except Exception as error:
            print(f"[core:align] qwen backend failed: {error}", flush=True)
            aligned_payload = None
    if aligned_payload is None:
        fallback_backend = "ctc" if alignment_backend in {"ctc", "qwen"} else "whisperx"
        aligned_payload = align_with_backend(
            base_segments,
            audio,
            detected_language,
            transcription_device,
            backend=fallback_backend,
            allow_ctc_fallback_to_whisperx=True,
        )
    aligned_segments = aligned_payload.get("segments", [])
    if cjk.is_cjk(detected_language):
        merged = _retokenize_cjk(base_segments, aligned_segments, detected_language)
    else:
        merged = _merge_alignment(base_segments, aligned_segments)
        for segment in merged:
            segment["words"] = _interpolate_missing_words(
                segment["text"],
                segment["start"],
                segment["end"],
                segment.get("words", []),
            )
    for segment in merged:
        segment["start"] = round(float(segment["start"]) + vocal_start, 3)
        segment["end"] = round(float(segment["end"]) + vocal_start, 3)
        for word in segment.get("words", []):
            word["start"] = round(float(word["start"]) + vocal_start, 3)
            word["end"] = round(float(word["end"]) + vocal_start, 3)
    return merged, detected_language
