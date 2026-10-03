from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]|^[%$&](\d+(?:\.\d+)?)")
LRC_INLINE_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
SYMBOL_NOISE_LINE_RE = re.compile(r"^[^0-9A-Za-z\u3400-\u9fff]+$")
REPEATED_SYMBOL_RE = re.compile(r"([^\w\s\u3400-\u9fff])\1{2,}")
CATALOG_FIELDS = [
    "id", "artist", "title", "album", "release_year", "duration_seconds",
    "artist_gender", "genre", "lyrics", "separator_model", "separator_mode",
    "alignment_model", "video_filename", "instrumental_filename", "lyrics_filename",
    "source_url", "youtube_title", "vocals_filename", "dereverbed_vocals_filename",
    "alignment_results_filename", "processing_status", "created_at",
    "language", "char_count", "pinyin_abbr", "zhuyin_abbr",
]


def stable_song_id(artist: str, title: str, source_url: str = "") -> str:
    identity = "|".join((artist.strip().casefold(), title.strip().casefold()))
    if not artist.strip() or not title.strip():
        identity = source_url.strip()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def plain_lyrics_from_lrc(lrc_path: Path) -> str:
    if not lrc_path.is_file():
        return ""
    contents = lrc_path.read_text(encoding="utf-8", errors="ignore")
    is_ktv_lrc = "@format ktv-lrc" in contents
    lines = []
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("@"):
            continue
        if is_ktv_lrc and not line.startswith("%"):
            continue
        line = re.sub(r"^%\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+", "", line)
        line = re.sub(r"^\$\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+", "", line)
        line = LRC_INLINE_TIMESTAMP_RE.sub("", line)
        line = re.sub(r"\[(?:ar|al|ti|by|re|ve|offset):[^\]]*\]", "", line, flags=re.IGNORECASE)
        line = _strip_weird_unicode(line).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def _read_catalog(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({field: row.get(field, "") or "" for field in CATALOG_FIELDS} for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def upsert_catalog_entry(index_path: Path, entry: dict) -> None:
    rows = _read_catalog(index_path)
    row = {field: str(entry.get(field, "") or "") for field in CATALOG_FIELDS}
    if not row["id"]:
        row["id"] = stable_song_id(row["artist"], row["title"], row["source_url"])
    if not row["created_at"]:
        row["created_at"] = datetime.now(timezone.utc).isoformat()

    existing_index = next(
        (
            index for index, existing in enumerate(rows)
            if existing.get("id") == row["id"]
            or (
                row["artist"] and row["title"]
                and existing.get("artist", "").strip().casefold() == row["artist"].strip().casefold()
                and existing.get("title", "").strip().casefold() == row["title"].strip().casefold()
            )
        ),
        None,
    )
    if existing_index is None:
        rows.append(row)
    else:
        existing = rows[existing_index]
        row["created_at"] = existing.get("created_at") or row["created_at"]
        rows[existing_index] = {**existing, **{key: value or existing.get(key, "") for key, value in row.items()}}
    _write_catalog(index_path, rows)


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
    song_name = Path(song_filename).name
    song_stem = song_name[:-4] if song_name.lower().endswith(".mp4") else song_name
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
    previous_rows = _read_catalog(index_path)
    existing_by_video = {row.get("video_filename", ""): row for row in previous_rows}
    rows = []
    seen_ids = set()
    timestamp = int(time.time())
    for mp4_file in sorted(songs_dir.glob("*.mp4")):
        stem = mp4_file.stem
        previous = existing_by_video.get(mp4_file.name, {})
        lrc_name = f"{stem}.lrc" if (songs_dir / f"{stem}.lrc").is_file() else ""
        entry = {
            **previous,
            "id": previous.get("id") or stable_song_id(previous.get("artist", ""), previous.get("title", ""), previous.get("source_url", mp4_file.name)),
            "title": previous.get("title") or stem,
            "video_filename": mp4_file.name,
            "instrumental_filename": f"{stem}.instrumental.m4a" if (songs_dir / f"{stem}.instrumental.m4a").is_file() else "",
            "lyrics_filename": lrc_name,
            "vocals_filename": f"{stem}.vocals.wav" if (songs_dir / f"{stem}.vocals.wav").is_file() else "",
            "dereverbed_vocals_filename": f"{stem}.vocals.dereverbed.wav" if (songs_dir / f"{stem}.vocals.dereverbed.wav").is_file() else "",
            "lyrics": plain_lyrics_from_lrc(songs_dir / lrc_name) if lrc_name else "",
            "processing_status": "complete",
            "created_at": previous.get("created_at") or str(timestamp),
        }
        if entry["id"] in seen_ids:
            continue
        seen_ids.add(entry["id"])
        rows.append({field: entry.get(field, "") for field in CATALOG_FIELDS})
    _write_catalog(index_path, rows)


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
