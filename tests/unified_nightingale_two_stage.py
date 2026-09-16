"""Two-stage lyrics-first alignment demo.

Stage 1 locates each supplied lyric line with ASR sentence segments.
Stage 2 forced-aligns the supplied text again, but only inside the sentence
window found in stage 1.  The original ``unified_nightingale.py`` is kept
unchanged for comparison.

Example:
    .venv-whisperx\\Scripts\\python.exe tests\\unified_nightingale_two_stage.py ^
        --audio "ktv_songs\\方大同 - Love Song.vocals.wav" ^
        --lyrics "lyrics\\方大同 - Love Song" ^
        --output tests\\love_song_two_stage.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
OTHERS = ROOT / "others"


def install_runtime_compat() -> None:
    """Provide the small GPU adapter required by the standalone ``others`` modules."""
    if "gpu" not in sys.modules:
        gpu = ModuleType("gpu")

        @contextmanager
        def gpu_model(_name: str):
            yield []

        def hard_free_gpu(*_args, **_kwargs) -> None:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except (ImportError, RuntimeError):
                pass

        gpu.gpu_model = gpu_model
        gpu.hard_free_gpu = hard_free_gpu
        gpu.end_of_song_cleanup = hard_free_gpu
        gpu.log_vram = lambda _label: None
        gpu.reset_peak_stats = lambda: None
        gpu.vram_snapshot = lambda: {}
        sys.modules["gpu"] = gpu

    sys.path.insert(0, str(OTHERS))


def read_lyrics(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return [line for line in lines if line]


def write_demo_lrc(result: dict, output: Path) -> None:
    with output.with_suffix(".lrc").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("@format ktv-lrc\n@version 1\n\n@lyric_synced word\n\n")
        for segment in result.get("segments", []):
            handle.write(
                f"%{float(segment['start']):.3f} {float(segment['end']):.3f} "
                f"{segment.get('text', '')}\n"
            )
            for word in segment.get("words", []):
                start = word.get("start")
                end = word.get("end")
                if start is None or end is None:
                    continue
                text = word.get("word", word.get("text", ""))
                handle.write(f"${float(start):.3f} {float(end):.3f} {text}\n")
            handle.write("\n")


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]", "", text).lower()


def similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (char_left != char_right),
            ))
        previous = current
    return 1.0 - previous[-1] / max(len(left), len(right))


def locate_sentences(lyrics: list[str], asr_segments: list[dict]) -> list[dict]:
    """Match each lyric line to a later contiguous ASR sentence window.

    WhisperX may split one sung lyric line into several ASR segments. Searching
    only one segment makes the first stage fail before forced alignment starts,
    so candidates are built from up to eight consecutive segments.
    """
    located = []
    cursor = 0
    for lyric in lyrics:
        target = normalize(lyric)
        best_window = None
        best_score = 0.0
        search_end = min(cursor + 30, len(asr_segments))
        for start_index in range(cursor, search_end):
            candidate_text = ""
            for end_index in range(start_index, min(start_index + 8, search_end)):
                candidate_text += str(asr_segments[end_index].get("text", ""))
                candidate = normalize(candidate_text)
                score = similarity(target, candidate)
                if target and (target in candidate or candidate in target):
                    score = max(score, 0.75)
                if score > best_score:
                    best_window, best_score = (start_index, end_index), score

        if best_window is None:
            raise ValueError(f"Stage 1 found no ASR candidate for lyric line: {lyric!r}")
        if best_score < 0.15:
            print(
                f"[two-stage] WARNING: low-confidence sentence match "
                f"score={best_score:.3f}: {lyric}",
                flush=True,
            )
        start_index, end_index = best_window
        source_start = asr_segments[start_index]
        source_end = asr_segments[end_index]
        located.append({
            "text": lyric,
            "start": float(source_start["start"]),
            "end": float(source_end["end"]),
            "score": round(best_score, 3),
        })
        cursor = end_index + 1
    return located


def align_two_stage(
    audio_path: Path,
    lyrics_path: Path,
    backend: str,
    language: str,
    model_name: str,
) -> dict:
    import whisperx

    from align import _map_chars_to_lines_cjk, _map_words_to_lines
    from cjk import is_cjk, tokenize_for_alignment, align_model_for
    from whisper_compat import (
        align_device_for,
        compute_type_for,
        detect_device,
        align_with_fallback,
        set_align_backend,
    )

    lyrics = read_lyrics(lyrics_path)
    if not lyrics:
        raise ValueError(f"No lyric lines found in {lyrics_path}")

    device = detect_device()
    align_device = align_device_for(device)
    set_align_backend(backend)
    audio = whisperx.load_audio(str(audio_path))

    print(f"[two-stage] Stage 1: sentence localization ({device})")
    asr_model = whisperx.load_model(
        model_name,
        align_device,
        compute_type=compute_type_for(device),
        language=language,
        task="transcribe",
    )
    asr_result = asr_model.transcribe(
        audio,
        language=language,
        task="transcribe",
        verbose=False,
    )
    asr_segments = [
        item for item in asr_result.get("segments", [])
        if item.get("start") is not None and item.get("end") is not None
    ]
    located = locate_sentences(lyrics, asr_segments)
    print(f"[two-stage] Stage 1 complete: {len(located)} sentence windows")
    del asr_model

    print("[two-stage] Stage 2: word alignment inside sentence windows")
    if is_cjk(language):
        token_pairs = [
            tokenize_for_alignment(item["text"], language)
            for item in located
        ]
        alignment_lines = [
            "".join(alignment_text for _, alignment_text in pairs)
            for pairs in token_pairs
        ]
    else:
        token_pairs = None
        alignment_lines = located
        alignment_lines = [item["text"] for item in located]

    raw_segments = [
        {
            "text": text,
            "start": item["start"],
            "end": item["end"],
        }
        for item, text in zip(located, alignment_lines)
    ]
    aligned = align_with_fallback(
        raw_segments,
        audio,
        language,
        align_device,
        model_name=align_model_for(language),
    )

    if is_cjk(language):
        segments = _map_chars_to_lines_cjk(aligned, lyrics, token_pairs, language)
    else:
        segments = _map_words_to_lines(aligned, lyrics)

    if len(segments) != len(lyrics):
        raise RuntimeError(
            f"Stage 2 returned {len(segments)} lines for {len(lyrics)} lyric lines"
        )
    result = {"language": language, "segments": segments, "source": "two-stage"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--lyrics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--backend", choices=("whisperx", "ctc"), default="ctc")
    args = parser.parse_args()

    install_runtime_compat()
    result = align_two_stage(
        args.audio.resolve(),
        args.lyrics.resolve(),
        args.backend,
        args.language,
        args.model,
    )
    args.output.resolve().write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_demo_lrc(result, args.output.resolve())
    print(
        f"[two-stage] complete: {len(result['segments'])} lines, "
        f"output={args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
