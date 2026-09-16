from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]|^[%$&](\d+(?:\.\d+)?)")
LRC_INLINE_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
SYMBOL_NOISE_LINE_RE = re.compile(r"^[^0-9A-Za-z\u3400-\u9fff]+$")
REPEATED_SYMBOL_RE = re.compile(r"([^\w\s\u3400-\u9fff])\1{2,}")


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
                "vocals": (
                    f"{stem}.lyrics.vocals.wav"
                    if (songs_dir / f"{stem}.lyrics.vocals.wav").is_file()
                    else (f"{stem}.vocals.wav" if (songs_dir / f"{stem}.vocals.wav").is_file() else "")
                ),
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

    for item in payload:
        if not isinstance(item, dict):
            continue
        cleaned = sanitize_lrclib_lyrics(item.get("plainLyrics"))
        if cleaned:
            return cleaned
    return None


def sanitize_lrclib_lyrics(lyrics: str | None) -> str | None:
    if not lyrics:
        return None

    cleaned_lines: list[str] = []
    for raw_line in lyrics.splitlines():
        line = _strip_weird_unicode(raw_line).strip()
        if not line:
            continue
        line = LRC_INLINE_TIMESTAMP_RE.sub("", line).strip()
        line = REPEATED_SYMBOL_RE.sub(r"\1\1", line).strip()
        if not line or SYMBOL_NOISE_LINE_RE.fullmatch(line):
            continue
        cleaned_lines.append(line)

    if not cleaned_lines:
        return None
    return "\n".join(cleaned_lines)


def _strip_weird_unicode(text: str) -> str:
    allowed = []
    for char in text:
        category = unicodedata.category(char)
        if char in {"\n", "\r", "\t"}:
            allowed.append(char)
            continue
        if category in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            continue
        if char in {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}:
            continue
        allowed.append(char)
    return "".join(allowed)


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
