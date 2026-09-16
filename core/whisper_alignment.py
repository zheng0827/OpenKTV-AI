from __future__ import annotations

from .ctc_alignment import ctc_align
from .gpu import gpu_model, log_vram
from .runtime import is_oom


def _run_whisperx(raw_segments, audio, language: str, device: str, model_name=None):
    import whisperx  # pylint: disable=import-outside-toplevel

    key_suffix = model_name or language
    with gpu_model(f"wav2vec2:{key_suffix}:{device}") as held:
        align_model, metadata = whisperx.load_align_model(
            language_code=language,
            device=device,
            model_name=model_name,
        )
        held.append(align_model)
        return whisperx.align(
            raw_segments,
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=True,
        )


def _run_ctc(raw_segments, audio, language: str, device: str, model_name=None):
    import whisperx  # pylint: disable=import-outside-toplevel

    key_suffix = model_name or language
    with gpu_model(f"wav2vec2:{key_suffix}:{device}") as held:
        align_model, metadata = whisperx.load_align_model(
            language_code=language,
            device=device,
            model_name=model_name,
        )
        held.append(align_model)
        return ctc_align(raw_segments, align_model, metadata, audio, device)


def align_with_backend(
    raw_segments,
    audio,
    language: str,
    device: str,
    backend: str = "whisperx",
    pre_align_cleanup=None,
    model_name=None,
    allow_ctc_fallback_to_whisperx: bool = True,
):
    runner = _run_ctc if backend == "ctc" else _run_whisperx
    try:
        return runner(raw_segments, audio, language, device, model_name=model_name)
    except Exception as error:
        if backend == "ctc" and allow_ctc_fallback_to_whisperx and not is_oom(error):
            return _run_whisperx(raw_segments, audio, language, device, model_name=model_name)
        if not is_oom(error):
            raise
        log_vram("oom:align_attempt1")

    if pre_align_cleanup:
        try:
            pre_align_cleanup()
        except Exception:
            pass
        try:
            return runner(raw_segments, audio, language, device, model_name=model_name)
        except Exception as retry_error:
            if backend == "ctc" and allow_ctc_fallback_to_whisperx and not is_oom(retry_error):
                return _run_whisperx(raw_segments, audio, language, device, model_name=model_name)
            if not is_oom(retry_error):
                raise
            log_vram("oom:align_attempt2")
    return runner(raw_segments, audio, language, "cpu", model_name=model_name)
