from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .runtime import resolve_device


def configure_demucs_cache(cache_dir: Path) -> Path:
    resolved_dir = cache_dir.expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TORCH_HOME"] = str(resolved_dir)
    os.environ.setdefault("HF_HOME", str(resolved_dir / "huggingface"))
    try:
        import torch  # pylint: disable=import-outside-toplevel

        torch.hub.set_dir(str(resolved_dir))
    except Exception:
        pass
    return resolved_dir


def demucs_checkpoint_dir(cache_dir: Path | None = None) -> Path:
    if cache_dir is not None:
        configure_demucs_cache(cache_dir)
    import torch  # pylint: disable=import-outside-toplevel

    return Path(torch.hub.get_dir()) / "checkpoints"


def _huggingface_demucs_weights_ready(model_name: str, cache_dir: Path | None) -> bool:
    hf_home = (
        cache_dir.expanduser().resolve() / "huggingface"
        if cache_dir is not None
        else Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
    )
    for manifest in (hf_home / "hub").glob(f"models--adefossez--*/snapshots/*/{model_name}.yaml"):
        try:
            signatures: list[str] = []
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("models:"):
                    _key, value = line.split(":", 1)
                    signatures = list(ast.literal_eval(value.strip()))
                    break
        except (OSError, SyntaxError, ValueError):
            continue
        if signatures and all((manifest.parent / f"{signature}.safetensors").is_file() for signature in signatures):
            return True
    return False


def _demucs_required_cache_files(model_name: str) -> list[str]:
    import demucs.pretrained as pretrained  # pylint: disable=import-outside-toplevel

    files_map = pretrained._parse_remote_files(pretrained.REMOTE_ROOT / "files.txt")  # pylint: disable=protected-access
    if model_name in files_map:
        return [files_map[model_name].rsplit("/", 1)[-1]]

    bag_file = pretrained.REMOTE_ROOT / f"{model_name}.yaml"
    if not bag_file.exists():
        raise FileNotFoundError(f"找不到 Demucs 模型設定: {model_name}")

    signatures: list[str] = []
    for line in bag_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("models:"):
            _key, value = line.split(":", 1)
            signatures = list(ast.literal_eval(value.strip()))
            break
    if not signatures:
        raise RuntimeError(f"無法解析 Demucs 權重清單: {model_name}")
    return [files_map[sig].rsplit("/", 1)[-1] for sig in signatures]


def demucs_weights_ready(model_name: str, cache_dir: Path | None = None) -> bool:
    if _huggingface_demucs_weights_ready(model_name, cache_dir):
        return True
    try:
        checkpoint_dir = demucs_checkpoint_dir(cache_dir)
    except Exception:
        return False
    required = _demucs_required_cache_files(model_name)
    return all((checkpoint_dir / filename).exists() for filename in required)


def ensure_demucs_weights(model_name: str, log_cb=print, cache_dir: Path | None = None) -> None:
    if cache_dir is not None:
        cache_dir = configure_demucs_cache(cache_dir)
        log_cb(f"Demucs weights cache: {demucs_checkpoint_dir()}")
    log_cb(f"🔍 啟動前檢查 Demucs 權重: {model_name}")
    if demucs_weights_ready(model_name, cache_dir=cache_dir):
        log_cb("✅ Demucs 權重已存在，略過下載。")
        return
    log_cb("⬇️ Demucs 權重不存在，開始自動下載...")
    try:
        import demucs.pretrained as pretrained  # pylint: disable=import-outside-toplevel

        pretrained.get_model(model_name)
    except Exception as error:
        raise RuntimeError(f"Demucs 權重下載失敗: {error}") from error
    if not demucs_weights_ready(model_name, cache_dir=cache_dir):
        raise RuntimeError("Demucs 權重下載完成，但快取檔檢查失敗。")
    log_cb("✅ Demucs 權重下載完成。")


def build_demucs_command(input_path: Path, output_dir: Path, model_name: str, stems: int, device: str) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "demucs.separate",
        "-n",
        model_name,
        "-d",
        device,
        "-o",
        str(output_dir),
    ]
    if stems == 2:
        command.extend(["--two-stems", "vocals"])
    command.append(str(input_path))
    return command


def _run_command(command: list[str]) -> None:
    subprocess.run(
        command,
        shell=False,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def prepare_accompaniment(stems_dir: Path, output_path: Path, stems: int) -> None:
    if stems == 2:
        no_vocals = stems_dir / "no_vocals.wav"
        if not no_vocals.exists():
            raise FileNotFoundError("Demucs output missing no_vocals.wav")
        shutil.copyfile(no_vocals, output_path)
        return

    drums = stems_dir / "drums.wav"
    bass = stems_dir / "bass.wav"
    other = stems_dir / "other.wav"
    missing = [p.name for p in [drums, bass, other] if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Demucs output missing stems: {', '.join(missing)}")

    _run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(drums),
            "-i",
            str(bass),
            "-i",
            str(other),
            "-filter_complex",
            "[0:a][1:a][2:a]amix=inputs=3:normalize=0[a]",
            "-map",
            "[a]",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def separate_with_demucs(input_path: Path, output_dir: Path, model_name: str, stems: int, device_preference: str, log_cb=print) -> tuple[Path, Path]:
    demucs_out_root = output_dir / "demucs_out"
    demucs_out_root.mkdir(parents=True, exist_ok=True)

    device = resolve_device(device_preference)
    if device == "mps":
        log_cb("⚠️ Demucs 目前改用 CPU，Apple Silicon 仍可在 WhisperX/Qwen 對齊階段使用 MPS。")
        device = "cpu"
    if device_preference == "cuda" and device != "cuda":
        log_cb("⚠️ 已要求 CUDA，但目前不可用，已自動改用 CPU。")
    log_cb(f"Demucs device: {device}")
    log_cb(f"Demucs 分離中 (model={model_name}, stems={stems})...")
    _run_command(build_demucs_command(input_path, demucs_out_root, model_name, stems, device))

    stem_folder = demucs_out_root / model_name / input_path.stem
    vocals = stem_folder / "vocals.wav"
    if not vocals.exists():
        raise FileNotFoundError("Demucs output missing vocals.wav")

    accompaniment = output_dir / "demucs_accompaniment.wav"
    prepare_accompaniment(stem_folder, accompaniment, stems)
    return vocals, accompaniment
