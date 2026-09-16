from __future__ import annotations

import os
import subprocess
from pathlib import Path


def download_youtube_video(url: str, output_path: Path, ffmpeg_dir: Path) -> None:
    command = [
        "yt-dlp",
        "--ffmpeg-location",
        str(ffmpeg_dir),
        "--force-overwrites",
        "--no-playlist",
        "-f",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "-o",
        str(output_path),
        url,
    ]
    subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
