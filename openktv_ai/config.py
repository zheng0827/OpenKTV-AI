import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - fallback for minimal runtime
    def load_dotenv(*_args, **_kwargs):
        return False


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
    demucs_cache_dir: Path
    demucs_model: str
    separator_stems: int
    device_preference: str
    mix_mode: str
    pseudo_delay_ms: int
    pseudo_reflection_gain: float
    pseudo_reverb_room: float
    pseudo_reverb_damping: float
    pseudo_backing_gain: float
    intro_skip_lead_seconds: float
    download_retry_count: int
    library_index_path: Path


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
    # Local values are convenient for development. Existing system environment
    # variables still take precedence for production deployments.
    load_dotenv(dotenv_path=root / ".env", override=False)

    stems = os.getenv("KTV_SEPARATOR_STEMS", "4").strip()
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
        demucs_cache_dir=Path(os.getenv("KTV_DEMUCS_CACHE_DIR", root / "model_cache" / "demucs")),
        demucs_model=os.getenv("KTV_DEMUCS_MODEL", "htdemucs_ft"),
        separator_stems=separator_stems,
        device_preference=os.getenv("KTV_DEVICE", "auto").strip().lower(),
        mix_mode=os.getenv("KTV_MIX_MODE", "pseudo-spatial").strip().lower(),
        pseudo_delay_ms=_to_int(os.getenv("KTV_PSEUDO_DELAY_MS"), 12),
        pseudo_reflection_gain=_to_float(os.getenv("KTV_PSEUDO_REFLECTION_GAIN"), 0.12),
        pseudo_reverb_room=_to_float(os.getenv("KTV_PSEUDO_REVERB_ROOM"), 0.45),
        pseudo_reverb_damping=_to_float(os.getenv("KTV_PSEUDO_REVERB_DAMPING"), 0.35),
        pseudo_backing_gain=_to_float(os.getenv("KTV_PSEUDO_BACKING_GAIN"), 0.9),
        intro_skip_lead_seconds=_to_float(os.getenv("KTV_INTRO_SKIP_LEAD_SECONDS"), 5.0),
        download_retry_count=_to_int(os.getenv("KTV_DOWNLOAD_RETRY_COUNT"), 2),
        library_index_path=Path(os.getenv("KTV_LIBRARY_INDEX_PATH", root / "ktv_songs" / "library_index.csv")),
    )
