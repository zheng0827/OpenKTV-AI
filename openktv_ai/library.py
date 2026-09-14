from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]|^[%$&](\d+(?:\.\d+)?)")


def parse_first_lyric_time(lrc_path: Path) -> float | None:
    if not lrc_path.is_file():
        return None
    for raw_line in lrc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = TIMESTAMP_RE.search(line)
        if not match:
            continue
        if match.group(4):
            return float(match.group(4))
        minutes = int(match.group(1) or 0)
        seconds = int(match.group(2) or 0)
        millis = int((match.group(3) or "0").ljust(3, "0"))
        return minutes * 60 + seconds + millis / 1000.0
    return None


def find_intro_skip_seconds(songs_dir: Path, song_filename: str, lead_seconds: float) -> tuple[float | None, float | None]:
    song_stem = Path(song_filename).stem
    lrc_candidates = [
        songs_dir / f"{song_stem}.ktv.lrc",
        songs_dir / f"{song_stem}.lrc",
    ]
    first_line_time = None
    for candidate in lrc_candidates:
        first_line_time = parse_first_lyric_time(candidate)
        if first_line_time is not None:
            break

    if first_line_time is None:
        return None, None

    return max(0.0, first_line_time - max(0.0, lead_seconds)), first_line_time


def update_library_index(songs_dir: Path, index_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    timestamp = int(time.time())
    for mp4_file in sorted(songs_dir.glob("*.mp4")):
        stem = mp4_file.stem
        rows.append(
            {
                "song": mp4_file.name,
                "instrumental": f"{stem}.instrumental.m4a" if (songs_dir / f"{stem}.instrumental.m4a").is_file() else "",
                "vocals": f"{stem}.vocals.wav" if (songs_dir / f"{stem}.vocals.wav").is_file() else "",
                "lyrics_lrc": f"{stem}.lrc" if (songs_dir / f"{stem}.lrc").is_file() else "",
                "indexed_at": timestamp,
            }
        )

    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["song", "instrumental", "vocals", "lyrics_lrc", "indexed_at"])
        writer.writeheader()
        writer.writerows(rows)


def fetch_lrclib_lyrics(song_name: str, singer: str) -> str | None:
    if not song_name:
        return None
    query = urllib.parse.urlencode({"track_name": song_name, "artist_name": singer or ""})
    url = f"https://lrclib.net/api/search?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, list) or not payload:
        return None

    item = payload[0] if isinstance(payload[0], dict) else None
    if not item:
        return None
    return item.get("syncedLyrics") or item.get("plainLyrics")


def write_ktv_lrc_template(output_path: Path, song_name: str, singer: str, lyrics: str | None) -> None:
    lines = [line.strip() for line in (lyrics or "").splitlines() if line.strip()]
    output = [
        "@format ktv-lrc",
        "@version 1",
        "",
        "@id 000000",
        "",
        f"@song_name {song_name}, zh-tw",
        f"@singer {singer or 'Unknown'}, zh-tw",
        "@duration 0",
        "",
        "@lyrics zh-tw",
        "@lyric_synced line",
        "",
        "@lyric zh-tw",
    ]

    cursor = 0.0
    if lines:
        for line in lines:
            end = cursor + 2.0
            output.append(f"%{cursor:.3f} {end:.3f} {line}")
            cursor = end
    else:
        output.append("%0.000 0.000 (暫無歌詞)")

    output.extend(["", "@dialogue"])
    output_path.write_text("\n".join(output) + "\n", encoding="utf-8")
