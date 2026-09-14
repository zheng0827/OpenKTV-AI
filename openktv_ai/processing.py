from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import AppSettings


def configure_demucs_cache(cache_dir: Path) -> Path:
    """Configure the Torch Hub and Hugging Face directories used by Demucs."""
    resolved_dir = cache_dir.expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    # Demucs downloads checkpoints through torch.hub. Setting both forms keeps
    # the directory stable even if torch was imported earlier.
    os.environ["TORCH_HOME"] = str(resolved_dir)
    # Some Demucs builds resolve safetensors through huggingface_hub instead.
    # Keep that cache alongside the Torch checkpoints unless the user has
    # deliberately configured a shared Hugging Face cache already.
    os.environ.setdefault("HF_HOME", str(resolved_dir / "huggingface"))

    try:
        import torch  # pylint: disable=import-outside-toplevel

        torch.hub.set_dir(str(resolved_dir))
    except Exception:
        # The model download below will surface a useful dependency error.
        pass

    return resolved_dir


def demucs_checkpoint_dir(cache_dir: Path | None = None) -> Path:
    if cache_dir is not None:
        configure_demucs_cache(cache_dir)

    import torch  # pylint: disable=import-outside-toplevel

    return Path(torch.hub.get_dir()) / "checkpoints"


def _huggingface_demucs_weights_ready(model_name: str, cache_dir: Path | None) -> bool:
    """Check the safetensors layout used by recent Demucs releases."""
    hf_home = (
        cache_dir.expanduser().resolve() / "huggingface"
        if cache_dir is not None
        else Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
    )
    snapshots_root = hf_home / "hub"

    # The repository name varies between Demucs releases, so discover a
    # snapshot by its model manifest rather than hard-coding a repository.
    for manifest in snapshots_root.glob(f"models--adefossez--*/snapshots/*/{model_name}.yaml"):
        try:
            signatures: list[str] = []
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("models:"):
                    _, value = line.split(":", 1)
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
    urls: list[str] = []

    if model_name in files_map:
        urls = [files_map[model_name]]
    else:
        bag_file = pretrained.REMOTE_ROOT / f"{model_name}.yaml"
        if not bag_file.exists():
            raise FileNotFoundError(f"找不到 Demucs 模型設定: {model_name}")

        signatures: list[str] = []
        for line in bag_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("models:"):
                _, value = line.split(":", 1)
                signatures = list(ast.literal_eval(value.strip()))
                break

        if not signatures:
            raise RuntimeError(f"無法解析 Demucs 權重清單: {model_name}")
        urls = [files_map[sig] for sig in signatures]

    return [url.rsplit("/", 1)[-1] for url in urls]


def demucs_weights_ready(model_name: str, cache_dir: Path | None = None) -> bool:
    # Demucs 4.0 originally used Torch Hub (.th files), while newer builds
    # obtain safetensors from Hugging Face. Support both cache formats.
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
    except Exception as error:  # pylint: disable=broad-except
        raise RuntimeError(f"Demucs 權重下載失敗: {error}") from error

    if not demucs_weights_ready(model_name, cache_dir=cache_dir):
        raise RuntimeError("Demucs 權重下載完成，但快取檔檢查失敗。")
    log_cb("✅ Demucs 權重下載完成。")


def _is_cuda_available() -> bool:
    try:
        import torch  # pylint: disable=import-outside-toplevel

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_device(preference: str) -> str:
    preferred = (preference or "auto").lower()
    cuda_available = _is_cuda_available()

    if preferred == "cpu":
        return "cpu"
    if preferred == "cuda":
        return "cuda" if cuda_available else "cpu"
    return "cuda" if cuda_available else "cpu"


def build_demucs_command(
    input_path: Path,
    output_dir: Path,
    model_name: str,
    stems: int,
    device: str,
) -> list[str]:
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


def build_mix_filter(_mode: str, settings: AppSettings) -> str:
    return (
        # Preserve separated vocals as-is and keep them centered.
        "[1:a]pan=mono|c0=0.5*FL+0.5*FR[vocal_mono];"
        "[vocal_mono]pan=stereo|c0=c0|c1=c0[vocals];"
        # Spatial processing is applied only to accompaniment/backing track.
        + build_pseudo_accompaniment_filter("[2:a]", settings, "acc")
        + "[vocals][acc]amix=inputs=2:normalize=0[a]"
    )


def build_pseudo_accompaniment_filter(input_stream: str, settings: AppSettings, output_label: str) -> str:
    """Return the shared pseudo-spatial processing used in both audio modes."""
    delay_right = settings.pseudo_delay_ms
    delay_left = max(2, delay_right // 2)
    return (
        f"{input_stream}aformat=channel_layouts=stereo,asplit=2[acc_dry][acc_ref];"
        f"[acc_ref]adelay={delay_left}|{delay_right},"
        f"volume={settings.pseudo_reflection_gain}[acc_er];"
        f"[acc_dry][acc_er]amix=inputs=2:normalize=0,volume={settings.pseudo_backing_gain},"
        f"aecho=0.6:0.4:{max(40, delay_right * 3)}:{settings.pseudo_reverb_room},"
        f"aecho=0.6:0.3:{max(70, delay_right * 5)}:{settings.pseudo_reverb_damping}"
        f"[{output_label}];"
    )


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


def _prepare_accompaniment(stems_dir: Path, output_path: Path, stems: int) -> None:
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

    cmd = [
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
    _run_command(cmd)


def _export_instrumental_track(
    accompaniment: Path,
    output_path: Path,
    mix_mode: str,
    settings: AppSettings,
) -> None:
    """Create the browser-playable companion track used by the player UI."""
    command = ["ffmpeg", "-y", "-i", str(accompaniment)]
    processed_filter = build_pseudo_accompaniment_filter("[0:a]", settings, "processed_acc")
    command.extend(
        [
            "-filter_complex",
            processed_filter,
            "-map",
            "[processed_acc]",
        ]
    )
    command.extend(["-c:a", "aac", "-b:a", "192k", str(output_path)])
    _run_command(command)


class KTVProcessor:
    def __init__(self, settings: AppSettings, log_cb):
        self.settings = settings
        self.log = log_cb

    def sanitize_filename(self, name: str) -> str:
        return "".join([c for c in name if c not in r'\\/:*?"<>|'])

    def _separate_audio(self, input_path: Path, job_temp_dir: Path, stems: int, mix_mode: str, device_pref: str) -> tuple[Path, Path]:
        demucs_out_root = job_temp_dir / "demucs_out"
        demucs_out_root.mkdir(parents=True, exist_ok=True)

        device = resolve_device(device_pref)
        if device_pref == "cuda" and device != "cuda":
            self.log("⚠️ 已要求 CUDA，但目前不可用，已自動改用 CPU。")
        self.log(f"Demucs device: {device}")

        command = build_demucs_command(
            input_path=input_path,
            output_dir=demucs_out_root,
            model_name=self.settings.demucs_model,
            stems=stems,
            device=device,
        )
        self.log(f"Demucs 分離中 (model={self.settings.demucs_model}, stems={stems}, mode={mix_mode})...")
        _run_command(command)

        stem_folder = demucs_out_root / self.settings.demucs_model / input_path.stem
        vocals = stem_folder / "vocals.wav"
        if not vocals.exists():
            raise FileNotFoundError("Demucs output missing vocals.wav")

        accompaniment = job_temp_dir / "accompaniment.wav"
        _prepare_accompaniment(stem_folder, accompaniment, stems)
        return vocals, accompaniment

    def _mix_audio(self, temp_input: Path, vocals: Path, accompaniment: Path, temp_output: Path, mix_mode: str) -> None:
        filter_complex = build_mix_filter(mix_mode, self.settings)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(temp_input),
            "-i",
            str(vocals),
            "-i",
            str(accompaniment),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(temp_output),
        ]
        self.log("步驟 3/4: 混音策略 pseudo-spatial...")
        _run_command(cmd)

    def process_song(self, url: str, manual_title: str, options: dict | None = None) -> bool:
        options = options or {}
        stems = 4 if str(options.get("stems", self.settings.separator_stems)) == "4" else 2
        mix_mode = str(options.get("mix_mode", self.settings.mix_mode)).lower()
        device_pref = str(options.get("device", self.settings.device_preference)).lower()

        job_temp_dir: Path | None = None
        try:
            safe_title = self.sanitize_filename(manual_title)
            self.log(f"目標歌曲：{safe_title}")

            job_id = str(int(time.time()))
            job_temp_dir = self.settings.temp_base_dir / job_id
            job_temp_dir.mkdir(parents=True, exist_ok=True)

            temp_input = job_temp_dir / "input.mp4"
            temp_output = job_temp_dir / "output.mp4"
            temp_instrumental = job_temp_dir / "instrumental.m4a"
            temp_vocals = job_temp_dir / "vocals.wav"

            self.log("步驟 1/4: 下載影片...")
            cmd_dl = [
                "yt-dlp",
                "--ffmpeg-location",
                str(self.settings.ffmpeg_dir),
                "--force-overwrites",
                "--no-playlist",
                "-f",
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
                "-o",
                str(temp_input),
                url,
            ]
            _run_command(cmd_dl)

            self.log("步驟 2/4: AI 分離 (Demucs)...")
            vocals, accompaniment = self._separate_audio(temp_input, job_temp_dir, stems, mix_mode, device_pref)
            shutil.copyfile(vocals, temp_vocals)

            self._mix_audio(temp_input, vocals, accompaniment, temp_output, mix_mode)
            _export_instrumental_track(accompaniment, temp_instrumental, mix_mode, self.settings)

            self.log(f"步驟 4/4: 儲存為 {safe_title}.mp4")
            final = self.settings.songs_dir / f"{safe_title}.mp4"
            if final.exists():
                final = self.settings.songs_dir / f"{safe_title}_{job_id}.mp4"
            final_instrumental = final.with_name(f"{final.stem}.instrumental.m4a")
            final_vocals = final.with_name(f"{final.stem}.vocals.wav")

            shutil.move(str(temp_output), str(final))
            shutil.move(str(temp_instrumental), str(final_instrumental))
            shutil.move(str(temp_vocals), str(final_vocals))
            self.log("✅ 製作完成！已自動同步至歌單。")
            return True

        except subprocess.CalledProcessError as error:
            self.log(f"❌ 執行失敗 (Code {error.returncode})")
            return False
        except Exception as error:  # pylint: disable=broad-except
            self.log(f"❌ 錯誤: {error}")
            return False
        finally:
            if job_temp_dir and job_temp_dir.exists():
                shutil.rmtree(job_temp_dir, ignore_errors=True)
