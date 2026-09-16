from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from .gpu import gpu_model
from .runtime import detect_device

SAMPLE_RATE = 16000
QWEN_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B-hf"
SUPPORTED_LANGS = frozenset({"en", "zh", "yue", "fr", "de", "it", "ja", "ko", "pt", "ru", "es"})
MAX_SEGMENT_SECONDS = 285.0
SLICE_PAD_SECONDS = 0.2
INFERENCE_BATCH = 8
_MIN_SAMPLES = 400


class QwenUnsupportedError(Exception):
    pass


def is_supported(language: str) -> bool:
    return language in SUPPORTED_LANGS


def _pick_dtype(device: str):
    import torch  # pylint: disable=import-outside-toplevel

    return torch.bfloat16 if device == "cuda" else torch.float32


def qwen_align(raw_segments, audio, language: str, device: str) -> dict:
    import numpy as np  # pylint: disable=import-outside-toplevel
    import torch  # pylint: disable=import-outside-toplevel

    if language not in SUPPORTED_LANGS:
        raise QwenUnsupportedError(f"language '{language}' not supported by qwen aligner")

    audio_np = audio.detach().cpu().numpy() if torch.is_tensor(audio) else np.asarray(audio)
    audio_np = np.ascontiguousarray(audio_np, dtype=np.float32)
    total_seconds = len(audio_np) / SAMPLE_RATE

    prepared: list[dict | None] = []
    for seg in raw_segments:
        text = (seg.get("text") or "").strip()
        if not text:
            prepared.append(None)
            continue
        start = max(0.0, float(seg.get("start", 0.0)) - SLICE_PAD_SECONDS)
        end = min(total_seconds, float(seg.get("end", total_seconds)) + SLICE_PAD_SECONDS)
        if end <= start:
            prepared.append(None)
            continue
        if end - start > MAX_SEGMENT_SECONDS:
            raise QwenUnsupportedError(f"segment spans {end - start:.0f}s, exceeds qwen aligner cap {MAX_SEGMENT_SECONDS:.0f}s")
        arr = audio_np[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
        if len(arr) < _MIN_SAMPLES:
            prepared.append(None)
            continue
        prepared.append({"arr": arr, "text": text, "offset": start})

    todo = [index for index, item in enumerate(prepared) if item is not None]
    results: list[list | None] = [None] * len(prepared)
    if todo:
        with gpu_model(f"qwen-aligner:{device}") as held:
            from transformers import AutoModelForTokenClassification, AutoProcessor  # pylint: disable=import-outside-toplevel

            processor = AutoProcessor.from_pretrained(QWEN_MODEL_ID)
            model = AutoModelForTokenClassification.from_pretrained(QWEN_MODEL_ID, dtype=_pick_dtype(device))
            model = model.to(device)
            model.eval()
            held.append(model)
            ts_token_id = model.config.timestamp_token_id
            for start_index in range(0, len(todo), INFERENCE_BATCH):
                batch_indexes = todo[start_index:start_index + INFERENCE_BATCH]
                audios = [prepared[index]["arr"] for index in batch_indexes]
                texts = [prepared[index]["text"] for index in batch_indexes]
                inputs, word_lists = processor.prepare_forced_aligner_inputs(audio=audios, transcript=texts, language=language)
                inputs = inputs.to(model.device, model.dtype)
                with torch.inference_mode():
                    outputs = model(**inputs)
                decoded = processor.decode_forced_alignment(
                    logits=outputs.logits,
                    input_ids=inputs["input_ids"],
                    word_lists=word_lists,
                    timestamp_token_id=ts_token_id,
                )
                for local, batch_index in enumerate(batch_indexes):
                    results[batch_index] = decoded[local]

    aligned_segments = []
    word_segments: list[dict] = []
    for index, seg in enumerate(raw_segments):
        out_seg = {"text": seg.get("text", ""), "start": seg.get("start"), "end": seg.get("end"), "words": []}
        prep = prepared[index]
        tokens = results[index]
        if prep is not None and tokens:
            offset = prep["offset"]
            words = []
            for tok in tokens:
                start = round(float(tok["start_time"]) + offset, 3)
                end = round(float(tok["end_time"]) + offset, 3)
                words.append({"word": tok["text"], "start": start, "end": max(start, end)})
            if words:
                out_seg["start"] = words[0]["start"]
                out_seg["end"] = words[-1]["end"]
                out_seg["words"] = words
                word_segments.extend(words)
        aligned_segments.append(out_seg)
    return {"segments": aligned_segments, "word_segments": word_segments}


def qwen_align_with_cpu_fallback(raw_segments, audio, language: str, pre_align_cleanup=None) -> dict:
    device = detect_device()
    try:
        return qwen_align(raw_segments, audio, language, device)
    except Exception as error:
        if device == "cpu" or "out of memory" not in str(error).lower():
            raise
        if pre_align_cleanup:
            try:
                pre_align_cleanup()
            except Exception:
                pass
        return qwen_align(raw_segments, audio, language, "cpu")
