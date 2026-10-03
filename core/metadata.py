from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


NOISE_RE = re.compile(
    r"\s*[\[(（【](?:official(?:\s+(?:music\s+)?video)?|lyrics?|mv|music\s*video|"
    r"karaoke|中字|歌詞|高音質|4k)[^\])）】]*[\])）】]",
    re.IGNORECASE,
)


def parse_youtube_title(raw_title: str, uploader: str = "") -> dict[str, str]:
    clean_title = re.sub(r"\s+", " ", NOISE_RE.sub("", raw_title or "")).strip(" -|｜_")
    for separator in (" - ", " – ", " — ", " | ", " ｜ ", " / "):
        if separator in clean_title:
            artist, title = (part.strip() for part in clean_title.split(separator, 1))
            if artist and title:
                return {"title": title, "artist": artist, "youtube_title": raw_title or ""}
    return {
        "title": clean_title or (raw_title or "").strip(),
        "artist": (uploader or "").strip(),
        "youtube_title": raw_title or "",
    }


def ai_parse_youtube_title(raw_title: str, uploader: str = "") -> dict[str, str]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or not raw_title:
        return {}
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent"
    )
    payload = {
        "contents": [{
            "parts": [{
                "text": (
                    "從 YouTube 音樂影片標題推測正式歌曲名稱與歌手，回傳 JSON，"
                    "僅包含 title 與 artist 兩個字串欄位；不確定時使用空字串。"
                    f"\n標題：{raw_title}\n上傳者：{uploader}"
                )
            }]
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
            result = json.loads(response.read().decode("utf-8"))
        response_text = result["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(response_text)
        if not isinstance(parsed, dict):
            return {}
        title = str(parsed.get("title") or "").strip()
        artist = str(parsed.get("artist") or "").strip()
        if not title:
            return {}
        return {"title": title, "artist": artist, "youtube_title": raw_title}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
        return {}


def extract_youtube_metadata(url: str) -> dict[str, str]:
    parsed_url = urllib.parse.urlparse(url.strip())
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
    if parsed_url.scheme not in {"http", "https"} or parsed_url.hostname not in allowed_hosts:
        raise ValueError("僅允許 http/https YouTube 影片網址")
    from yt_dlp import YoutubeDL

    with YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("title", "")
    uploader = info.get("artist") or info.get("uploader") or ""
    return ai_parse_youtube_title(title, uploader) or parse_youtube_title(title, uploader)


def spotify_track_metadata(title: str, artist: str) -> dict[str, str]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not title or not client_id or not client_secret:
        return {}

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    token_request = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_request, timeout=12) as response:  # nosec B310
            token = json.loads(response.read().decode("utf-8")).get("access_token")
        if not token:
            return {}
        query = f"track:{title}" + (f" artist:{artist}" if artist else "")
        url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
            {"q": query, "type": "track", "limit": 5}
        )
        search_request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(search_request, timeout=12) as response:  # nosec B310
            tracks = json.loads(response.read().decode("utf-8")).get("tracks", {}).get("items", [])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return {}

    if not tracks:
        return {}
    preferred = tracks[0]
    expected_artist = artist.casefold().strip()
    if expected_artist:
        preferred = next(
            (track for track in tracks if any(a.get("name", "").casefold() == expected_artist for a in track.get("artists", []))),
            preferred,
        )
    artists = preferred.get("artists") or []
    album = preferred.get("album") or {}
    release_date = str(album.get("release_date") or "")
    genre = ""
    artist_id = artists[0].get("id") if artists else None
    if artist_id:
        artist_url = f"https://api.spotify.com/v1/artists/{urllib.parse.quote(artist_id)}"
        artist_request = urllib.request.Request(artist_url, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(artist_request, timeout=12) as response:  # nosec B310
                genre = ", ".join(json.loads(response.read().decode("utf-8")).get("genres", []))
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
    return {
        "title": preferred.get("name") or title,
        "artist": ", ".join(a.get("name", "") for a in artists if a.get("name")),
        "album": album.get("name", ""),
        "release_year": release_date[:4],
        "duration_seconds": str(round((preferred.get("duration_ms") or 0) / 1000)),
        "genre": genre,
    }
