"""Unified entry point for the complete ``others`` analyzer stack.

The original ``others`` directory is intentionally modular. This file is a
single user-facing program that wires those modules together:

* fixed-file lyric alignment (default)
* Whisper / Parakeet transcription
* WhisperX, torchaudio CTC, and Qwen alignment backends
* CJK tokenization/readings and vocal-region detection
* optional full stem-separation pipeline
* JSON and KTV-LRC output

Default demo:

    .venv-whisperx\\Scripts\\python.exe tests\\unified_nightingale.py

The default inputs are ``tests\\vocals.wav`` and ``tests\\lyric.txt``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

songname = "方大同 - 愛愛愛"
ROOT = Path(__file__).resolve().parents[1]
OTHERS = ROOT / "others"
TESTS = Path(__file__).resolve().parent
DEFAULT_AUDIO = ROOT / "ktv_songs" / (songname + ".vocals.wav")
DEFAULT_LYRICS = ROOT / "lyrics" / songname
DEFAULT_OUTPUT = TESTS / "unified_result.json"
LRC_OUTPUT = ROOT / "ktv_songs" / (songname + ".lrc")

def install_runtime_compat() -> None:
    """Install only the optional services missing from this repository.

    The production analyzer normally receives ``gpu`` from its host
    application. The checked-in ``others`` modules should also be runnable as
    a standalone demo, so this adapter supplies the same small API.
    """
    if "gpu" not in sys.modules:
        gpu = ModuleType("gpu")

        @contextmanager
        def gpu_model(_name: str):
            held: list[object] = []
            try:
                yield held
            finally:
                held.clear()

        def hard_free_gpu(*_args, **_kwargs) -> None:
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                if hasattr(torch, "mps") and torch.mps.is_available():
                    torch.mps.empty_cache()
            except (ImportError, RuntimeError):
                pass

        gpu.gpu_model = gpu_model
        gpu.hard_free_gpu = hard_free_gpu
        gpu.end_of_song_cleanup = hard_free_gpu
        gpu.log_vram = lambda _label: None
        gpu.reset_peak_stats = lambda: None
        gpu.vram_snapshot = lambda: {}
        sys.modules["gpu"] = gpu

    os.environ["PATH"] = str(ROOT) + os.pathsep + os.environ.get("PATH", "")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(OTHERS))


def read_lyrics(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return [line for line in lines if line]


def write_demo_lrc(result: dict, output: Path) -> None:
    """Write a compact KTV-LRC view of the unified result."""
    lrc_path = output.with_suffix(".lrc")
    with lrc_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"@format ktv-lrc\n@version 1\n\n@config songname {songname}, zh-tw\n\n@lyric_synced word\n\n")
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
                handle.write(
                    f"${float(start):.3f} {float(end):.3f} "
                    f"{word.get('word', word.get('text', ''))}\n"
                )
            handle.write("\n")


def align_fixed_files(
    audio_path: Path,
    lyrics_path: Path,
    output_path: Path,
    backend: str,
    language: str | None,
    model: str,
) -> dict:
    """Run the complete lyrics-first path from ``analyze``/``pipeline``."""
    import whisperx

    from align import align_lyrics
    from whisper_compat import align_device_for, detect_device, set_align_backend

    lines = read_lyrics(lyrics_path)
    if not lines:
        raise ValueError(f"No lyric lines found in {lyrics_path}")

    with tempfile.TemporaryDirectory(prefix="unified-nightingale-") as temp:
        lyrics_json = Path(temp) / "lyrics.json"
        lyrics_json.write_text(
            json.dumps({"lines": lines}, ensure_ascii=False),
            encoding="utf-8",
        )
        device = detect_device()
        set_align_backend(backend)
        print(f"[unified] align backend={backend}, device={device}")
        result = align_lyrics(
            str(lyrics_json),
            str(audio_path),
            align_device_for(device),
            model_name=model,
            language_override=language,
        )

    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_demo_lrc(result, output_path)
    del whisperx
    return result


def transcribe_file(
    audio_path: Path,
    output_path: Path,
    backend: str,
    engine: str,
    language: str | None,
    model: str,
) -> dict:
    """Run the generated-transcript path from ``transcribe.py``."""
    from transcribe import transcribe_vocals
    from whisper_compat import detect_device, set_align_backend

    device = detect_device()
    set_align_backend(backend)
    result = transcribe_vocals(
        str(audio_path),
        str(audio_path),
        device,
        model_name=model,
        engine=engine,
        language_override=language,
    )
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_demo_lrc(result, output_path)
    return result


def run_full_pipeline(
    audio_path: Path,
    output_dir: Path,
    backend: str,
    engine: str,
    lyrics_path: Path | None,
    language: str | None,
    model: str,
) -> None:
    """Expose the stem-separation/cache path when host dependencies exist."""
    from pipeline import run_pipeline
    from whisper_compat import detect_device, set_align_backend

    import hashlib

    digest = hashlib.blake2b(audio_path.read_bytes(), digest_size=16).hexdigest()
    set_align_backend(backend)
    run_pipeline(
        str(audio_path),
        str(output_dir),
        digest,
        detect_device(),
        model_name=model,
        engine=engine,
        lyrics_path=str(lyrics_path) if lyrics_path else None,
        language_override=language,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("align", "transcribe", "pipeline"), default="align")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--lyrics", type=Path, default=DEFAULT_LYRICS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output-dir", type=Path, default=TESTS / "unified_cache")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--engine", choices=("whisper", "parakeet"), default="whisper")
    parser.add_argument(
        "--backend",
        choices=("whisperx", "ctc", "qwen"),
        default="ctc",
        help="Alignment backend. Unsupported/failing Qwen falls back to wav2vec2.",
    )
    args = parser.parse_args()

    install_runtime_compat()
    audio_path = args.audio.resolve()
    if not audio_path.is_file():
        raise FileNotFoundError(audio_path)

    if args.mode == "align":
        lyrics_path = args.lyrics.resolve()
        if not lyrics_path.is_file():
            raise FileNotFoundError(lyrics_path)
        result = align_fixed_files(
            audio_path, lyrics_path, args.output.resolve(),
            args.backend, args.language, args.model,
        )
    elif args.mode == "transcribe":
        result = transcribe_file(
            audio_path, args.output.resolve(),
            args.backend, args.engine, args.language, args.model,
        )
    else:
        lyrics_path = args.lyrics.resolve() if args.lyrics.is_file() else None
        run_full_pipeline(
            audio_path, args.output_dir.resolve(),
            args.backend, args.engine, lyrics_path, args.language, args.model,
        )
        result = {}

    if result:
        print(
            f"[unified] complete: language={result.get('language')}, "
            f"segments={len(result.get('segments', []))}, output={args.output.resolve()}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
