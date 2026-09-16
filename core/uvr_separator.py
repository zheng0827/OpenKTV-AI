from __future__ import annotations

import logging
from pathlib import Path


CANONICAL_OUTPUTS = {
    "Vocals": "uvr_vocals.wav",
    "Instrumental": "uvr_instrumental.wav",
}


def _find_output(output_dir: Path, explicit_names: tuple[str, ...], aliases: tuple[str, ...]) -> Path | None:
    for explicit_name in explicit_names:
        candidate = output_dir / explicit_name
        if candidate.exists():
            return candidate
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        lower = path.name.lower()
        if any(alias in lower for alias in aliases):
            return path
    return None


def separate_with_uvr(input_path: Path, output_dir: Path, model_dir: Path, model_name: str, log_cb=print) -> tuple[Path, Path]:
    try:
        from audio_separator.separator import Separator  # pylint: disable=import-outside-toplevel
    except Exception as error:
        raise RuntimeError(f"無法載入 UVR 依賴 audio-separator: {error}") from error

    model_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_cb(f"UVR 分離中 (model={model_name})...")
    separator = Separator(
        log_level=logging.WARNING,
        model_file_dir=str(model_dir),
        output_dir=str(output_dir),
        output_format="WAV",
        use_soundfile=True,
    )
    separator.load_model(model_name)
    separator.separate(
        str(input_path),
        custom_output_names={"Vocals": "uvr_vocals", "Instrumental": "uvr_instrumental"},
    )

    vocals = _find_output(output_dir, ("uvr_vocals", CANONICAL_OUTPUTS["Vocals"]), ("vocals",))
    instrumental = _find_output(output_dir, ("uvr_instrumental", CANONICAL_OUTPUTS["Instrumental"]), ("instrumental", "karaoke", "no_vocals", "inst"))
    if vocals is None or instrumental is None:
        raise FileNotFoundError("UVR output missing vocals or instrumental stem")
    return vocals, instrumental
