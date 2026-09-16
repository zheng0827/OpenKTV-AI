from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import AppSettings


def build_pseudo_accompaniment_filter(input_stream: str, settings: AppSettings, output_label: str) -> str:
    delay_right = settings.pseudo_delay_ms
    delay_left = max(2, delay_right // 2)
    return (
        f"{input_stream}aformat=channel_layouts=stereo,asplit=2[acc_dry][acc_ref];"
        f"[acc_ref]adelay={delay_left}|{delay_right},"
        f"volume={settings.pseudo_reflection_gain}[acc_er];"
        "[acc_dry][acc_er]amix=inputs=2:normalize=0,"
        "highpass=f=55,"
        "equalizer=f=180:t=q:w=0.8:g=-1.2,"
        "equalizer=f=3200:t=q:w=1.0:g=0.7,"
        f"volume={settings.pseudo_backing_gain},"
        f"alimiter=limit=0.95[{output_label}];"
    )


def build_mix_filter(_mode: str, settings: AppSettings) -> str:
    return (
        "[1:a]anull[lyrics];"
        "[2:a]anull[dialogue];"
        + build_pseudo_accompaniment_filter("[3:a]", settings, "acc")
        + "[lyrics][dialogue][acc]amix=inputs=3:normalize=0,volume=0.5[a]"
    )


def build_instrumental_filter(settings: AppSettings) -> str:
    return (
        build_pseudo_accompaniment_filter("[0:a]", settings, "processed_acc")
        + "[processed_acc]volume=0.5[mastered_acc];"
        + "[mastered_acc][1:a]amix=inputs=2:normalize=0[out]"
    )


def _run_command(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            shell=False,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        message = detail or "ffmpeg command failed"
        raise RuntimeError(message) from error


def extract_timed_vocals(source_wav: Path, segments: list[dict], output_wav: Path) -> None:
    valid = [segment for segment in segments if float(segment.get("end", 0.0)) > float(segment.get("start", 0.0))]
    if not valid:
        _run_command([
            "ffmpeg", "-y", "-i", str(source_wav), "-filter_complex", "[0:a]volume=0[out]", "-map", "[out]", "-c:a", "pcm_s16le", str(output_wav),
        ])
        return

    labels = [f"clip{index}" for index in range(len(valid))]
    parts = ["[0:a]volume=0[base];"]
    for index, segment in enumerate(valid):
        delay = int(max(0.0, float(segment["start"])) * 1000)
        parts.append(
            f"[0:a]atrim=start={float(segment['start']):.3f}:end={float(segment['end']):.3f},"
            f"asetpts=PTS-STARTPTS,adelay={delay}|{delay}[{labels[index]}];"
        )
    inputs = "".join(f"[{label}]" for label in labels)
    parts.append(f"[base]{inputs}amix=inputs={len(valid) + 1}:normalize=0[out]")
    _run_command([
        "ffmpeg", "-y", "-i", str(source_wav), "-filter_complex", "".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", str(output_wav),
    ])


def export_instrumental_track(accompaniment: Path, dialogue_vocals: Path, output_path: Path, settings: AppSettings) -> None:
    _run_command([
        "ffmpeg", "-y", "-i", str(accompaniment), "-i", str(dialogue_vocals),
        "-filter_complex", build_instrumental_filter(settings),
        "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(output_path),
    ])


def mix_video_audio(temp_input: Path, lyrics_vocals: Path, dialogue_vocals: Path, accompaniment: Path, temp_output: Path, settings: AppSettings) -> None:
    _run_command([
        "ffmpeg", "-y",
        "-i", str(temp_input),
        "-i", str(lyrics_vocals),
        "-i", str(dialogue_vocals),
        "-i", str(accompaniment),
        "-filter_complex", build_mix_filter(settings.mix_mode, settings),
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac",
        str(temp_output),
    ])
