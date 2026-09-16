from __future__ import annotations

from pathlib import Path
from typing import Any

from .gpu import gpu_model
from .language_detection import detect_language_multiwindow
from .qwen_alignment import QwenUnsupportedError, is_supported as qwen_supported, qwen_align_with_cpu_fallback
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
        for item in aligned.get("chars", []):
            text = clean_text(item.get("char", item.get("text", "")))
            start = item.get("start")
            end = item.get("end")
            if text and start is not None and end is not None and float(end) >= float(start):
                aligned_words.append({"text": text, "start": float(start), "end": float(end)})
        if not aligned_words:
            for item in aligned.get("words", []):
                text = clean_text(item.get("word", item.get("text", "")))
                start = item.get("start")
                end = item.get("end")
                if text and start is not None and end is not None and float(end) >= float(start):
                    aligned_words.append({"text": text, "start": float(start), "end": float(end)})
        if aligned_words:
            updated["words"] = aligned_words
            updated["start"] = min(item["start"] for item in aligned_words)
            updated["end"] = max(item["end"] for item in aligned_words)
        merged.append(updated)
    return merged


def transcribe_segments(
    vocals_wav: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str | None,
    alignment_backend: str,
) -> tuple[list[dict[str, Any]], str]:
    import whisperx  # pylint: disable=import-outside-toplevel

    audio = whisperx.load_audio(str(vocals_wav))
    with gpu_model(f"whisperx:{model_name}:{device}") as held:
        model = whisperx.load_model(model_name, device=device, compute_type=compute_type, task="transcribe")
        held.append(model)
        detected_language = language or detect_language_multiwindow(model, audio)
        result = model.transcribe(audio, batch_size=8, task="transcribe", language=detected_language, chunk_size=30)

    base_segments = []
    for seg in result.get("segments", []):
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
            device,
            backend=fallback_backend,
            allow_ctc_fallback_to_whisperx=True,
        )
    return _merge_alignment(base_segments, aligned_payload.get("segments", [])), detected_language
