import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _to_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AppSettings:
    base_dir: Path
    templates_dir: Path
    songs_dir: Path
    temp_base_dir: Path
    ffmpeg_dir: Path
    yt_dlp_path: Path
    host: str
    port: int
    secret_key: str
    demucs_model: str
    separator_stems: int
    device_preference: str
    mix_mode: str
    stereo_balance_left_original: float
    stereo_balance_right_original: float
    pseudo_delay_ms: int
    pseudo_reflection_gain: float
    pseudo_original_gain: float
    pseudo_accompaniment_gain: float
    pseudo_left_original: float
    pseudo_left_accompaniment: float
    pseudo_right_original: float
    pseudo_right_accompaniment: float


class Config:
    DEBUG = False


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


def resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_settings(base_dir: Path | None = None) -> AppSettings:
    root = base_dir or resolve_base_dir()

    stems = os.getenv("KTV_SEPARATOR_STEMS", "2").strip()
    separator_stems = 4 if stems == "4" else 2

    return AppSettings(
        base_dir=root,
        templates_dir=Path(os.getenv("KTV_TEMPLATES_DIR", root / "templates")),
        songs_dir=Path(os.getenv("KTV_SONGS_DIR", root / "ktv_songs")),
        temp_base_dir=Path(os.getenv("KTV_TEMP_DIR", root / "temp_processing")),
        ffmpeg_dir=Path(os.getenv("KTV_FFMPEG_DIR", root / "ffmpeg" / "bin")),
        yt_dlp_path=Path(os.getenv("KTV_YTDLP_PATH", root / "yt-dlp.exe")),
        host=os.getenv("KTV_HOST", "0.0.0.0"),
        port=_to_int(os.getenv("KTV_PORT"), 5000),
        secret_key=os.getenv("KTV_SECRET_KEY", "ktv_secret"),
        demucs_model=os.getenv("KTV_DEMUCS_MODEL", "htdemucs_ft"),
        separator_stems=separator_stems,
        device_preference=os.getenv("KTV_DEVICE", "auto").strip().lower(),
        mix_mode=os.getenv("KTV_MIX_MODE", "pseudo-spatial").strip().lower(),
        stereo_balance_left_original=_to_float(os.getenv("KTV_BALANCE_LEFT_ORIGINAL"), 0.65),
        stereo_balance_right_original=_to_float(os.getenv("KTV_BALANCE_RIGHT_ORIGINAL"), 0.35),
        pseudo_delay_ms=_to_int(os.getenv("KTV_PSEUDO_DELAY_MS"), 12),
        pseudo_reflection_gain=_to_float(os.getenv("KTV_PSEUDO_REFLECTION_GAIN"), 0.12),
        pseudo_original_gain=_to_float(os.getenv("KTV_PSEUDO_ORIGINAL_GAIN"), 1.0),
        pseudo_accompaniment_gain=_to_float(os.getenv("KTV_PSEUDO_ACCOMPANIMENT_GAIN"), 0.95),
        pseudo_left_original=_to_float(os.getenv("KTV_PSEUDO_LEFT_ORIGINAL"), 0.72),
        pseudo_left_accompaniment=_to_float(os.getenv("KTV_PSEUDO_LEFT_ACCOMPANIMENT"), 0.28),
        pseudo_right_original=_to_float(os.getenv("KTV_PSEUDO_RIGHT_ORIGINAL"), 0.28),
        pseudo_right_accompaniment=_to_float(os.getenv("KTV_PSEUDO_RIGHT_ACCOMPANIMENT"), 0.72),
    )
