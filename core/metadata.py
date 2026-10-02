from __future__ import annotations

import base64
import difflib
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

_NOISE_RE = re.compile(
    r"\b(official|music video|lyrics?|mv|karaoke|cover|live|伴奏|翻唱|cover版|現場版)\b",
    re.IGNORECASE,
)
_FILE_INVALID_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_NON_ORIGINAL_TERMS = ("cover", "karaoke", "instrumental", "live", "tribute", "翻唱", "伴奏", "現場")


def normalize_youtube_title(raw_title: str) -> str:
    value = unicodedata.normalize("NFKC", raw_title or "").strip()
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)|（[^）]*）", lambda match: " " if _NOISE_RE.search(match.group()) else match.group(), value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -|｜_")


def parse_youtube_title(raw_title: str, uploader: str = "") -> dict[str, str]:
    title = normalize_youtube_title(raw_title)
    for separator in (" - ", " – ", " — ", " | ", " ｜ ", " / "):
        if separator in title:
            artist, track = (part.strip() for part in title.split(separator, 1))
            if artist and track:
                return {"title": title, "song": track, "artist": artist}
    return {"title": title, "song": title, "artist": (uploader or "").strip()}


def safe_filename(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = _FILE_INVALID_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or "untitled"


def _search_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = _NOISE_RE.sub(" ", value)
    return " ".join(re.findall(r"[\w\u3400-\u9fff]+", value))


def _similarity(left: str, right: str) -> float:
    a, b = _search_key(left), _search_key(right)
    if not a or not b:
        return 0.0
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    left_tokens, right_tokens = set(a.split()), set(b.split())
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return max(sequence, overlap)


def _spotify_request(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Spotify API returned an invalid response")
    return payload


def _spotify_access_token() -> str:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("Spotify API credentials are not configured")
    authorization = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = _spotify_request(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=b"grant_type=client_credentials",
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Spotify API did not return an access token")
    return token


def match_spotify_track(song: str, artist: str, minimum_score: float = 0.72) -> dict:
    token = _spotify_access_token()
    query = " ".join(part for part in (f'track:"{song}"' if song else "", f'artist:"{artist}"' if artist else "") if part)
    url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode({"q": query or song, "type": "track", "limit": 10})
    authorization = {"Authorization": "Bearer " + token}
    payload = _spotify_request(url, headers=authorization)
    tracks = payload.get("tracks", {}).get("items", [])
    candidates = []
    for track in tracks:
        track_name = track.get("name", "")
        artists = track.get("artists", [])
        artist_names = [entry.get("name", "") for entry in artists]
        title_score = _similarity(song, track_name)
        artist_score = max((_similarity(artist, name) for name in artist_names), default=0.0) if artist else 0.55
        score = title_score * 0.68 + artist_score * 0.32
        combined = f"{track_name} {' '.join(artist_names)}".casefold()
        if any(term in combined for term in _NON_ORIGINAL_TERMS):
            score -= 0.35
        candidates.append((score, track))
    if not candidates:
        return {"status": "not_found", "confidence": 0.0, "track": None}

    candidates.sort(key=lambda item: item[0], reverse=True)
    score, track = candidates[0]
    artist_names = [entry.get("name", "") for entry in track.get("artists", [])]
    album = track.get("album") or {}
    artist_genres = []
    first_artist_id = next((entry.get("id") for entry in track.get("artists", []) if entry.get("id")), None)
    if first_artist_id:
        try:
            artist_payload = _spotify_request(
                f"https://api.spotify.com/v1/artists/{urllib.parse.quote(first_artist_id)}",
                headers=authorization,
            )
            artist_genres = artist_payload.get("genres") or []
        except Exception:
            pass
    return {
        "status": "matched" if score >= minimum_score else "needs_review",
        "confidence": round(max(0.0, min(1.0, score)), 4),
        "track": {
            "id": track.get("id", ""),
            "url": track.get("external_urls", {}).get("spotify", ""),
            "song_name": track.get("name", ""),
            "artist_name": ", ".join(artist_names),
            "artist_id": first_artist_id or "",
            "album": album.get("name", ""),
            "release_year": (album.get("release_date") or "")[:4],
            "duration_seconds": round((track.get("duration_ms") or 0) / 1000, 3),
            "genre": ", ".join(artist_genres),
        },
    }


def extract_youtube_metadata(url: str) -> dict:
    from yt_dlp import YoutubeDL

    with YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp did not return video metadata")
    parsed = parse_youtube_title(info.get("title", ""), info.get("uploader", ""))
    return {
        **parsed,
        "raw_title": info.get("title", ""),
        "video_id": info.get("id", ""),
        "duration_seconds": info.get("duration") or 0,
        "uploader": info.get("uploader", ""),
        "url": info.get("webpage_url") or url,
    }


def fetch_lyrics(song: str, artist: str, album: str = "", duration_seconds: float = 0.0, musixmatch_api_key: str = "") -> dict:
    params = {"track_name": song, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration_seconds:
        params["duration"] = str(int(duration_seconds))
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            item = json.loads(response.read().decode("utf-8"))
        if isinstance(item, dict):
            timed = item.get("syncedLyrics") or ""
            plain = item.get("plainLyrics") or ""
            if timed or plain:
                return {"source": "lrclib", "synced": timed, "plain": plain}
    except Exception:
        pass
    if musixmatch_api_key:
        try:
            search_url = "https://api.musixmatch.com/ws/1.1/track.search?" + urllib.parse.urlencode(
                {"q_track": song, "q_artist": artist, "page_size": 5, "apikey": musixmatch_api_key, "format": "json"}
            )
            search = _spotify_request(search_url)
            matches = search.get("message", {}).get("body", {}).get("track_list", [])
            track_id = next(
                (row.get("track", {}).get("track_id") for row in matches if row.get("track", {}).get("has_lyrics")),
                None,
            )
            if track_id:
                lyric_url = "https://api.musixmatch.com/ws/1.1/track.lyrics.get?" + urllib.parse.urlencode(
                    {"track_id": track_id, "apikey": musixmatch_api_key, "format": "json"}
                )
                lyrics_payload = _spotify_request(lyric_url)
                text = lyrics_payload.get("message", {}).get("body", {}).get("lyrics", {}).get("lyrics_body", "")
                if text:
                    return {"source": "musixmatch", "synced": "", "plain": text}
        except Exception:
            pass
    return {"source": "", "synced": "", "plain": ""}
