import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
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
    uvr_model_dir: Path
    uvr_model: str
    separator_backend: str
    separator_stems: int
    device_preference: str
    pseudo_delay_ms: int
    pseudo_reflection_gain: float
    pseudo_reverb_room: float
    pseudo_reverb_damping: float
    pseudo_backing_gain: float
    whisper_model: str
    whisper_language: str
    whisper_compute_type: str
    alignment_backend: str
    intro_skip_lead_seconds: float
    download_retry_count: int
    library_index_path: Path
    retry_delay_seconds: float
    lyrics_alignment_backends: tuple[str, ...]
    alignment_parallelism: int
    dereverb_enabled: bool
    dereverb_model: str
    spotify_match_threshold: float
    musixmatch_api_key: str


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
    load_dotenv(dotenv_path=root / ".env", override=False)
    config_values = {}
    config_path = Path(os.getenv("KTV_CONFIG_PATH", root / "config.yaml"))
    if config_path.is_file():
        try:
            import yaml

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            config_values = loaded if isinstance(loaded, dict) else {}
        except Exception as error:
            raise RuntimeError(f"無法載入設定檔 {config_path}: {error}") from error

    download_cfg = config_values.get("download", {})
    retry_cfg = download_cfg.get("background_jobs", {})
    audio_cfg = config_values.get("audio_processing", {})
    metadata_cfg = config_values.get("metadata", {})
    paths_cfg = config_values.get("paths", {})
    services_cfg = config_values.get("services", {})
    flask_cfg = services_cfg.get("flask", {})
    separator_cfg = audio_cfg.get("separator", {})
    alignment_cfg = audio_cfg.get("alignment", {})
    dereverb_cfg = audio_cfg.get("dereverb", {})
    models_cfg = config_values.get("models", {})

    def configured_path(env_name: str, config_value, default: Path) -> Path:
        value = os.getenv(env_name, str(config_value) if config_value else str(default))
        configured = Path(value)
        return configured if configured.is_absolute() else root / configured

    stems = os.getenv("KTV_SEPARATOR_STEMS", str(separator_cfg.get("stems", "4"))).strip()
    separator_stems = 4 if stems == "4" else 2

    return AppSettings(
        base_dir=root,
        templates_dir=configured_path("KTV_TEMPLATES_DIR", None, root / "templates"),
        songs_dir=configured_path("KTV_SONGS_DIR", paths_cfg.get("songs_directory"), root / "ktv_songs"),
        temp_base_dir=configured_path("KTV_TEMP_DIR", paths_cfg.get("temporary_directory"), root / "temp_processing"),
        ffmpeg_dir=configured_path("KTV_FFMPEG_DIR", paths_cfg.get("ffmpeg_directory"), root / "ffmpeg" / "bin"),
        yt_dlp_path=configured_path("KTV_YTDLP_PATH", None, root / "yt-dlp.exe"),
        host=os.getenv("KTV_HOST", flask_cfg.get("host", "0.0.0.0")),
        port=_to_int(os.getenv("KTV_PORT"), _to_int(str(flask_cfg.get("port", 5000)), 5000)),
        secret_key=os.getenv("KTV_SECRET_KEY") or secrets.token_urlsafe(32),
        demucs_cache_dir=Path(os.getenv("KTV_DEMUCS_CACHE_DIR", root / "model_cache" / "demucs")),
        demucs_model=os.getenv("KTV_DEMUCS_MODEL", separator_cfg.get("demucs_model", "htdemucs_ft")),
        uvr_model_dir=Path(os.getenv("KTV_UVR_MODEL_DIR", root / "model_cache" / "uvr")),
        uvr_model=os.getenv("KTV_UVR_MODEL", separator_cfg.get("uvr_model", "UVR-MDX-NET-Voc_FT.onnx")),
        separator_backend=os.getenv("KTV_SEPARATOR_BACKEND", separator_cfg.get("backend", "demucs")).strip().lower(),
        separator_stems=separator_stems,
        device_preference=os.getenv("KTV_DEVICE", "auto").strip().lower(),
        pseudo_delay_ms=_to_int(os.getenv("KTV_PSEUDO_DELAY_MS"), 12),
        pseudo_reflection_gain=_to_float(os.getenv("KTV_PSEUDO_REFLECTION_GAIN"), 0.12),
        pseudo_reverb_room=_to_float(os.getenv("KTV_PSEUDO_REVERB_ROOM"), 0.45),
        pseudo_reverb_damping=_to_float(os.getenv("KTV_PSEUDO_REVERB_DAMPING"), 0.35),
        pseudo_backing_gain=_to_float(os.getenv("KTV_PSEUDO_BACKING_GAIN"), 0.9),
        whisper_model=os.getenv("KTV_WHISPER_MODEL", models_cfg.get("whisper", {}).get("model", "large-v3")),
        whisper_language=os.getenv("KTV_WHISPER_LANGUAGE", "").strip().lower(),
        whisper_compute_type=os.getenv("KTV_WHISPER_COMPUTE_TYPE", models_cfg.get("whisper", {}).get("compute_type", "auto")),
        alignment_backend=os.getenv("KTV_ALIGNMENT_BACKEND", alignment_cfg.get("primary_backend", "ctc")).strip().lower(),
        intro_skip_lead_seconds=_to_float(os.getenv("KTV_INTRO_SKIP_LEAD_SECONDS"), 5.0),
        download_retry_count=_to_int(
            os.getenv("KTV_DOWNLOAD_RETRY_COUNT"),
            _to_int(str(retry_cfg.get("retry_count", download_cfg.get("retry_count", 2))), 2),
        ),
        library_index_path=configured_path(
            "KTV_LIBRARY_INDEX_PATH",
            paths_cfg.get("library_csv"),
            root / "ktv_songs" / "library.csv",
        ),
        retry_delay_seconds=_to_float(
            os.getenv("KTV_DOWNLOAD_RETRY_DELAY_SECONDS"),
            _to_float(str(retry_cfg.get("retry_delay_seconds", download_cfg.get("retry_delay_seconds", 2))), 2.0),
        ),
        lyrics_alignment_backends=tuple(alignment_cfg.get("backends", ["ctc", "whisperx", "qwen"])),
        alignment_parallelism=_to_int(
            os.getenv("KTV_ALIGNMENT_PARALLELISM"),
            _to_int(str(alignment_cfg.get("max_parallel_backends", 1)), 1),
        ),
        dereverb_enabled=bool(dereverb_cfg.get("enabled", True)),
        dereverb_model=str(dereverb_cfg.get("model_name", "UVR-DeEcho-DeReverb.pth")),
        spotify_match_threshold=_to_float(
            os.getenv("KTV_SPOTIFY_MATCH_THRESHOLD"),
            _to_float(str(metadata_cfg.get("spotify", {}).get("minimum_match_score", 0.72)), 0.72),
        ),
        musixmatch_api_key=os.getenv("MUSIXMATCH_API_KEY", ""),
    )
