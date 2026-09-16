"""Run the complete lyric forced-alignment pipeline on fixed test files.

The script intentionally uses the six modules in ``others``:

* ``align``: end-to-end orchestration and lyric-line mapping
* ``language``: optional multi-window language detection
* ``cjk``: CJK tokenization and readings
* ``whisper_compat``: backend/device selection and OOM fallback
* ``ctc_align``: torchaudio CTC backend
* ``qwen_align``: optional Qwen backend

Run from the repository root with the WhisperX environment:

    .venv-whisperx\\Scripts\\python.exe tests\\run_alignment_demo.py

The default fixed inputs are ``tests\\vocals.wav`` and ``tests\\lyric.txt``.
Use ``--backend qwen`` to try Qwen3-ForcedAligner instead of CTC.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OTHERS = ROOT / "others"
DEFAULT_AUDIO = Path(__file__).resolve().with_name("Nanahoshi - ツバサ.vocals.wav")
DEFAULT_LYRICS = Path(__file__).resolve().with_name("lyric.txt")
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("alignment_result.json")


def _install_project_compat_modules() -> None:
    """Provide the small project services expected by ``others`` modules."""
    if "gpu" not in sys.modules:
        gpu = types.ModuleType("gpu")

        @contextmanager
        def gpu_model(_name: str):
            held: list[object] = []
            try:
                yield held
            finally:
                held.clear()

        def hard_free_gpu() -> None:
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
        gpu.log_vram = lambda _label: None
        gpu.reset_peak_stats = lambda: None
        gpu.vram_snapshot = lambda: {}
        gpu.gpu_model = gpu_model
        sys.modules["gpu"] = gpu

    if "audio" not in sys.modules:
        audio = types.ModuleType("audio")

        def detect_vocal_region(samples):
            """Return the non-silent region using short-window RMS energy."""
            import numpy as np

            values = np.asarray(samples, dtype=np.float32)
            if values.size == 0:
                return 0.0, 0.0
            window = 16000 // 10
            rms = []
            for start in range(0, len(values), window):
                chunk = values[start : start + window]
                rms.append(float(np.sqrt(np.mean(chunk * chunk))))
            peak = max(rms, default=0.0)
            threshold = max(peak * 0.08, 1e-4)
            active = [i for i, value in enumerate(rms) if value >= threshold]
            if not active:
                return 0.0, len(values) / 16000
            return active[0] * window / 16000, min(
                len(values) / 16000, (active[-1] + 1) * window / 16000
            )

        audio.detect_vocal_region = detect_vocal_region
        sys.modules["audio"] = audio


def _read_lyrics(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return [line for line in lines if line]


def _write_lrc(result: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8") as output:
        output.write("@format ktv-lrc\n@version 1\n\n@lyric_synced word\n\n")
        for segment in result.get("segments", []):
            output.write(
                f"%{segment['start']:.3f} {segment['end']:.3f} "
                f"{segment['text']}\n"
            )
            for word in segment.get("words", []):
                start = word.get("start")
                end = word.get("end")
                if start is None or end is None:
                    continue
                output.write(
                    f"${start:.3f} {end:.3f} {word['word']}"
                    f"{' [' + word['reading'] + ']' if word.get('reading') else ''}\n"
                )
            output.write("\n")


def run(audio_path: Path, lyrics_path: Path, output_path: Path, backend: str) -> dict:
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if not lyrics_path.is_file():
        raise FileNotFoundError(f"Lyric file not found: {lyrics_path}")

    _install_project_compat_modules()
    ffmpeg_dir = ROOT / "ffmpeg"
    ffmpeg_exe = ROOT / "ffmpeg.exe"
    if ffmpeg_exe.is_file():
        os.environ["PATH"] = str(ROOT) + os.pathsep + os.environ.get("PATH", "")
    elif ffmpeg_dir.is_dir():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")
    sys.path.insert(0, str(OTHERS))

    from align import align_lyrics
    from whisper_compat import (
        align_device_for,
        detect_device,
        set_align_backend,
    )

    lines = _read_lyrics(lyrics_path)
    if not lines:
        raise ValueError(f"No lyric lines found in {lyrics_path}")

    # align.py consumes its historical JSON lyric contract; keep lyric.txt as
    # the user-facing input and create the adapter data in memory on disk.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="openktv-align-") as temp_dir:
        lyrics_json = Path(temp_dir) / "lyrics.json"
        lyrics_json.write_text(
            json.dumps({"lines": lines}, ensure_ascii=False),
            encoding="utf-8",
        )

        device = detect_device()
        set_align_backend(backend)
        print(f"[demo] backend={backend}, device={device}")
        result = align_lyrics(
            str(lyrics_json),
            str(audio_path),
            align_device_for(device),
            model_name="large-v3",
            language_override="jp",
        )

    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_lrc(result, output_path.with_suffix(".lrc"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--lyrics", type=Path, default=DEFAULT_LYRICS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--backend",
        choices=("whisperx", "ctc", "qwen"),
        default="ctc",
        help="Alignment backend; ctc is the fast default.",
    )
    args = parser.parse_args()

    result = run(
        args.audio.resolve(),
        args.lyrics.resolve(),
        args.output.resolve(),
        args.backend,
    )
    print(
        f"[demo] complete: {len(result.get('segments', []))} segments, "
        f"language={result.get('language')}"
    )
    print(f"[demo] JSON: {args.output.resolve()}")
    print(f"[demo] LRC: {args.output.resolve().with_suffix('.lrc')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
