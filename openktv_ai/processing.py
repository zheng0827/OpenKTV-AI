from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import AppSettings


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


def build_mix_filter(mode: str, settings: AppSettings) -> str:
    selected = (mode or "pseudo-spatial").lower()
    if selected == "legacy":
        return (
            "[0:a]pan=mono|c0=0.5*FL+0.5*FR[L];"
            "[2:a]pan=mono|c0=0.5*FL+0.5*FR[R];"
            "[L][R]join=inputs=2:channel_layout=stereo[a]"
        )

    if selected == "stereo-balance":
        l_orig = settings.stereo_balance_left_original
        r_orig = settings.stereo_balance_right_original
        l_acc = 1.0 - l_orig
        r_acc = 1.0 - r_orig
        return (
            "[0:a]pan=mono|c0=0.5*FL+0.5*FR[orig];"
            "[2:a]pan=mono|c0=0.5*FL+0.5*FR[acc];"
            "[orig][acc]amerge=inputs=2[base];"
            f"[base]pan=stereo|c0={l_orig}*c0+{l_acc}*c1|c1={r_orig}*c0+{r_acc}*c1[a]"
        )

    delay_right = settings.pseudo_delay_ms
    delay_left = max(2, delay_right // 2)
    return (
        "[0:a]pan=mono|c0=0.5*FL+0.5*FR,highpass=f=80,"
        f"volume={settings.pseudo_original_gain}[orig];"
        "[2:a]aformat=channel_layouts=stereo,asplit=2[acc_dry][acc_ref];"
        f"[acc_ref]adelay={delay_left}|{delay_right},"
        f"volume={settings.pseudo_reflection_gain}[acc_er];"
        "[acc_dry][acc_er]amix=inputs=2:normalize=0,"
        "pan=mono|c0=0.5*FL+0.5*FR,"
        "equalizer=f=220:t=q:w=1.0:g=-0.8,"
        "equalizer=f=4300:t=q:w=1.0:g=0.8,"
        f"volume={settings.pseudo_accompaniment_gain}[acc];"
        "[orig][acc]amerge=inputs=2[blend];"
        f"[blend]pan=stereo|c0={settings.pseudo_left_original}*c0+{settings.pseudo_left_accompaniment}*c1"
        f"|c1={settings.pseudo_right_original}*c0+{settings.pseudo_right_accompaniment}*c1,"
        "alimiter=limit=0.95[a]"
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
        selected_mode = mix_mode if mix_mode in {"legacy", "stereo-balance", "pseudo-spatial"} else self.settings.mix_mode
        filter_complex = build_mix_filter(selected_mode, self.settings)
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
        self.log(f"步驟 3/4: 混音策略 {selected_mode}...")
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

            self._mix_audio(temp_input, vocals, accompaniment, temp_output, mix_mode)

            self.log(f"步驟 4/4: 儲存為 {safe_title}.mp4")
            final = self.settings.songs_dir / f"{safe_title}.mp4"
            if final.exists():
                final = self.settings.songs_dir / f"{safe_title}_{job_id}.mp4"

            shutil.move(str(temp_output), str(final))
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
