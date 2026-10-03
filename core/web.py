from __future__ import annotations

import json
import hmac
import os
import time
import re
import socket
import subprocess
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from flask import Blueprint, Flask, current_app, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit

from .config import AppSettings, load_settings
from .library import find_intro_skip_seconds, update_library_index
from .unified_nightingale import KTVProcessor

CONTROL_ROLES = {"remote", "queue", "admin", "combo"}
BACKGROUND_JOBS: dict[str, dict] = {}
BACKGROUND_JOBS_LOCK = threading.Lock()


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _is_playlist_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return "list" in query or "/playlist" in parsed.path


def _validate_youtube_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只允許 http/https 連結")
    host = (parsed.netloc or "").lower()
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
    if host not in allowed_hosts:
        raise ValueError("僅支援 YouTube 連結")
    return url.strip()


def _extract_title_artist(raw_title: str) -> tuple[str, str]:
    parsed = _parse_title_model(raw_title)
    return parsed["song"], parsed["singer"]


def _normalize_yt_title(raw_title: str) -> str:
    text = (raw_title or "").strip()
    noise_patterns = [
        r"\[[^\]]*(official|lyrics|mv|music\s*video|karaoke|中字|歌詞)[^\]]*\]",
        r"\([^\)]*(official|lyrics|mv|music\s*video|karaoke|中字|歌詞)[^\)]*\)",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -|｜_")
    return text


def _parse_title_model(raw_title: str, uploader: str = "") -> dict[str, str]:
    normalized = _normalize_yt_title(raw_title)
    separators = [" - ", " – ", " — ", " | ", " ｜ ", " / "]
    for separator in separators:
        if separator in normalized:
            left, right = [part.strip() for part in normalized.split(separator, 1)]
            if left and right:
                return {"title": normalized, "song": right, "singer": left}

    if uploader.strip():
        return {"title": normalized or raw_title.strip(), "song": normalized or raw_title.strip(), "singer": uploader.strip()}
    return {"title": normalized or raw_title.strip(), "song": normalized or raw_title.strip(), "singer": ""}


def _extract_youtube_title(url: str) -> dict[str, str]:
    safe_url = _validate_youtube_url(url)
    try:
        from yt_dlp import YoutubeDL  # pylint: disable=import-outside-toplevel
    except Exception as error:
        raise RuntimeError(f"無法載入 yt-dlp 模組: {error}") from error

    with YoutubeDL({"quiet": True, "skip_download": True, "extract_flat": True}) as ydl:
        payload = ydl.extract_info(safe_url, download=False)
    title = (payload or {}).get("title") or ""
    uploader = (payload or {}).get("uploader") or ""
    parsed = _parse_title_model(title, uploader=uploader)
    parsed["raw_title"] = title
    parsed["uploader"] = uploader
    return parsed


def _playlist_entries(url: str, settings: AppSettings) -> list[dict]:
    safe_url = _validate_youtube_url(url)
    try:
        from yt_dlp import YoutubeDL  # pylint: disable=import-outside-toplevel
    except Exception as error:
        raise RuntimeError(f"無法載入 yt-dlp 模組: {error}") from error

    with YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True}) as ydl:
        payload = ydl.extract_info(safe_url, download=False)
    entries = []
    for item in payload.get("entries", []) or []:
        video_id = item.get("id")
        if not video_id:
            continue
        entries.append(
            {
                "id": video_id,
                "title": (item.get("title") or video_id).strip(),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "singer": (item.get("uploader") or "").strip(),
            }
        )
    return entries


def _create_blueprint() -> Blueprint:
    bp = Blueprint("web", __name__)

    @bp.route("/health")
    def health():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        try:
            import resource
            memory_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if os.name != "darwin" else 1)
        except Exception:
            memory_bytes = 0
        return json.dumps({
            "status": "ok",
            "library_songs": sum(1 for _ in settings.songs_dir.glob("*.mp4")) if settings.songs_dir.exists() else 0,
            "background_jobs": len(BACKGROUND_JOBS),
            "process_cpu_seconds": time.process_time(),
            "peak_memory_bytes": memory_bytes,
        })

    @bp.route("/player")
    def page_player():
        return render_template("player.html")

    @bp.route("/lyrics-editor")
    def page_lyrics_editor():
        return render_template("lyrics_editor.html")

    @bp.route("/remote")
    def page_remote():
        return render_template("remote.html")

    @bp.route("/queue")
    def page_queue():
        return render_template("queue.html")

    @bp.route("/admin")
    def page_admin():
        return render_template("admin.html")

    @bp.route("/combo")
    def page_combo():
        return render_template("combo.html")

    @bp.route("/")
    def page_index():
        return render_template("remote.html")

    @bp.route("/songs/<path:filename>")
    def serve_song(filename: str):
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        return send_from_directory(settings.songs_dir, filename)

    @bp.route("/api/list")
    def get_song_list():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        songs = [
            file_name
            for file_name in os.listdir(settings.songs_dir)
            if file_name.lower().endswith(".mp4")
        ]
        return json.dumps(sorted(songs))

    @bp.route("/api/playlist_preview")
    def playlist_preview():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        url = request.args.get("url", "").strip()
        if not _is_playlist_url(url):
            return json.dumps({"ok": False, "error": "不是 playlist 連結"}), 400
        try:
            entries = _playlist_entries(url, settings)
            return json.dumps({"ok": True, "entries": entries})
        except ValueError:
            return json.dumps({"ok": False, "error": "playlist 參數無效"}), 400
        except Exception:
            return json.dumps({"ok": False, "error": "playlist 讀取失敗"}), 500

    @bp.route("/api/resolve_yt_title")
    def resolve_yt_title():
        url = request.args.get("url", "").strip()
        if not url:
            return json.dumps({"ok": False, "error": "缺少連結"}), 400
        try:
            data = _extract_youtube_title(url)
            return json.dumps({"ok": True, **data})
        except ValueError:
            return json.dumps({"ok": False, "error": "YouTube 連結格式不正確"}), 400
        except Exception:
            return json.dumps({"ok": False, "error": "無法解析 YouTube 標題"}), 500

    @bp.route("/api/jobs", methods=["POST"])
    def create_processing_job():
        expected_token = os.getenv("KTV_JOB_API_TOKEN", "")
        supplied_token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if len(expected_token) < 32 or expected_token.startswith("replace-") or not hmac.compare_digest(supplied_token, expected_token):
            return json.dumps({"error": "unauthorized"}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return json.dumps({"error": "invalid_json"}), 400
        for field in ("url", "title", "singer", "lyrics_text"):
            if field in payload and not isinstance(payload[field], str):
                return json.dumps({"error": f"invalid_{field}"}), 400
        try:
            url = _validate_youtube_url(payload.get("url", ""))
        except ValueError:
            return json.dumps({"error": "invalid_youtube_url"}), 400
        title = (payload.get("title") or "").strip()[:300]
        singer = (payload.get("singer") or "").strip()[:200]
        lyrics = (payload.get("lyrics_text") or "")[:100_000]
        raw_options = payload.get("options") or {}
        if not isinstance(raw_options, dict):
            return json.dumps({"error": "invalid_options"}), 400
        allowed_options = {"stems", "device", "separator_backend", "alignment_backend"}
        if set(raw_options) - allowed_options:
            return json.dumps({"error": "unknown_options"}), 400
        options = {key: value for key, value in raw_options.items() if key in allowed_options}
        if options.get("stems") is not None and str(options["stems"]) not in {"2", "4"}:
            return json.dumps({"error": "invalid_stems"}), 400
        if options.get("device") is not None and str(options["device"]).lower() not in {"auto", "cuda", "cpu", "mps"}:
            return json.dumps({"error": "invalid_device"}), 400
        if options.get("separator_backend") is not None and str(options["separator_backend"]).lower() not in {"demucs", "uvr", "hybrid"}:
            return json.dumps({"error": "invalid_separator_backend"}), 400
        if options.get("alignment_backend") is not None and str(options["alignment_backend"]).lower() not in {"ctc", "whisperx", "qwen"}:
            return json.dumps({"error": "invalid_alignment_backend"}), 400
        job_slots: threading.BoundedSemaphore = current_app.config["JOB_SLOTS"]
        if not job_slots.acquire(blocking=False):
            return json.dumps({"error": "processing_capacity_reached"}), 429
        job_id = os.urandom(16).hex()
        job = {"job_id": job_id, "status": "queued", "progress": 0, "logs": [], "error": ""}
        with BACKGROUND_JOBS_LOCK:
            BACKGROUND_JOBS[job_id] = job
            for old_id in list(BACKGROUND_JOBS)[:-500]:
                BACKGROUND_JOBS.pop(old_id, None)

        settings: AppSettings = current_app.config["APP_SETTINGS"]

        def log_job(message: str):
            with BACKGROUND_JOBS_LOCK:
                job["logs"].append(str(message)[:2000])
                job["logs"] = job["logs"][-100:]

        def run_job():
            try:
                job["status"] = "running"
                processor = KTVProcessor(settings=settings, log_cb=log_job)
                attempts = max(0, settings.download_retry_count) + 1
                for attempt in range(1, attempts + 1):
                    job["attempt"] = attempt
                    job["progress"] = min(95, int((attempt - 1) * 95 / attempts))
                    success = processor.process_song(
                        url,
                        title,
                        {**options, "singer": singer, "lyrics_text": lyrics},
                    )
                    if success:
                        job["status"] = "complete"
                        job["progress"] = 100
                        return
                    last_message = job["logs"][-1] if job["logs"] else ""
                    if "Spotify 曲目比對狀態為 needs_review" in last_message:
                        job["status"] = "needs_review"
                        job["error"] = last_message
                        return
                    if "Spotify API credentials are not configured" in last_message:
                        job["status"] = "failed"
                        job["error"] = last_message
                        return
                    if attempt < attempts:
                        time.sleep(max(0.0, settings.retry_delay_seconds))
                job["status"] = "failed"
                job["error"] = (job["logs"][-1] if job["logs"] else "processing failed")
            except Exception as error:
                job["status"] = "failed"
                job["error"] = str(error)[:500]
            finally:
                job_slots.release()

        threading.Thread(target=run_job, daemon=True).start()
        return json.dumps({"job_id": job_id, "status": job["status"]}), 202

    @bp.route("/api/jobs/<job_id>")
    def get_processing_job(job_id: str):
        expected_token = os.getenv("KTV_JOB_API_TOKEN", "")
        supplied_token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if len(expected_token) < 32 or expected_token.startswith("replace-") or not hmac.compare_digest(supplied_token, expected_token):
            return json.dumps({"error": "unauthorized"}), 401
        with BACKGROUND_JOBS_LOCK:
            job = BACKGROUND_JOBS.get(job_id)
            if job is None:
                return json.dumps({"error": "job_not_found"}), 404
            return json.dumps({**job, "logs": list(job.get("logs", []))}, ensure_ascii=False)

    return bp


def create_app(settings: AppSettings | None = None) -> tuple[Flask, SocketIO, AppSettings]:
    app_settings = settings or load_settings()
    app = Flask(__name__, template_folder=str(app_settings.templates_dir))
    app.config.from_mapping(SECRET_KEY=app_settings.secret_key, APP_SETTINGS=app_settings)
    app.config["JOB_SLOTS"] = threading.BoundedSemaphore(app_settings.max_concurrent_song_jobs)
    app.register_blueprint(_create_blueprint())

    cors_origins = [origin.strip() for origin in os.getenv("KTV_CORS_ORIGINS", "").split(",") if origin.strip()]
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    socketio = SocketIO(app, cors_allowed_origins=cors_origins)
    return app, socketio, app_settings


def register_socket_handlers(socketio: SocketIO, settings: AppSettings, log_cb: Callable[[str], None]):
    state = {
        "is_processing": False,
        "client_roles": {},
        "playlist_queue": [],
        "played_queue": [],
        "next_queue_id": 1,
        "current_song": None,
        "is_playing": False,
        "position": 0.0,
        "started_at": None,
        "audio_mode": "original",
        "effects": {"volume": 1.0, "pitch": 0},
        "intro_skip_to": None,
        "intro_skip_hide_after": None,
        "intro_skip_used": False,
    }

    def current_position() -> float:
        if state["is_playing"] and state["started_at"] is not None:
            return state["position"] + max(0.0, time.time() - state["started_at"])
        return state["position"]

    def role_for_sid(sid: str) -> str:
        return state["client_roles"].get(sid, "player")

    def can_control() -> bool:
        return role_for_sid(request.sid) in CONTROL_ROLES

    def update_intro_skip(song_filename: str | None):
        if not song_filename:
            state["intro_skip_to"] = None
            state["intro_skip_hide_after"] = None
            state["intro_skip_used"] = False
            return
        skip_to, hide_after = find_intro_skip_seconds(
            settings.songs_dir,
            song_filename,
            lead_seconds=settings.intro_skip_lead_seconds,
        )
        state["intro_skip_to"] = skip_to
        state["intro_skip_hide_after"] = hide_after
        state["intro_skip_used"] = False

    def queue_payload() -> list[dict]:
        items = []
        for item in state["playlist_queue"]:
            current = state["current_song"] and item["queue_id"] == state["current_song"]["queue_id"]
            item_state = "playing" if current else "queued"
            items.append({
                "queue_id": item["queue_id"],
                "filename": item["filename"],
                "title": item["title"],
                "state": item_state,
            })

        for item in state["played_queue"][-30:]:
            items.append(
                {
                    "queue_id": item["queue_id"],
                    "filename": item["filename"],
                    "title": item["title"],
                    "state": "sung",
                }
            )
        return items

    def playback_snapshot() -> dict:
        current = state["current_song"]
        filename = current["filename"] if current else None
        instrumental_filename = None
        if filename:
            candidate = f"{Path(filename).stem}.instrumental.m4a"
            if (settings.songs_dir / candidate).is_file():
                instrumental_filename = candidate
        return {
            "filename": filename,
            "title": (current["title"] if current else ""),
            "instrumental_filename": instrumental_filename,
            "is_playing": state["is_playing"],
            "position": current_position(),
            "audio_mode": state["audio_mode"],
            "effects": state["effects"],
            "server_time": time.time(),
            "intro_skip_to": state["intro_skip_to"],
            "intro_skip_hide_after": state["intro_skip_hide_after"],
            "intro_skip_used": state["intro_skip_used"],
            "queue": queue_payload(),
        }

    def emit_all_state():
        payload = playback_snapshot()
        socketio.emit("playback_snapshot", payload)
        socketio.emit("playback_state", payload)
        socketio.emit("update_queue", [item["filename"] for item in state["playlist_queue"]])
        socketio.emit("update_queue_state", queue_payload())

    def playback_sync_loop():
        while True:
            socketio.sleep(1)
            if state["current_song"]:
                emit_all_state()

    socketio.start_background_task(playback_sync_loop)

    def broadcast_log(message: str):
        log_cb(message)
        socketio.emit("admin_log", {"msg": message})

    def enqueue_song(filename: str, title: str | None = None, insert_next: bool = False) -> dict:
        entry = {
            "queue_id": state["next_queue_id"],
            "filename": filename,
            "title": title or filename,
        }
        state["next_queue_id"] += 1

        if insert_next and state["current_song"] and state["playlist_queue"]:
            state["playlist_queue"].insert(1, entry)
        else:
            state["playlist_queue"].append(entry)

        if state["current_song"] is None and state["playlist_queue"]:
            start_current_song(state["playlist_queue"][0])
        emit_all_state()
        return entry

    def start_current_song(entry: dict):
        state["current_song"] = entry
        state["is_playing"] = True
        state["position"] = 0.0
        state["started_at"] = time.time()
        update_intro_skip(entry["filename"])

    def advance_song():
        if state["playlist_queue"]:
            finished = state["playlist_queue"].pop(0)
            state["played_queue"].append(finished)
        if state["playlist_queue"]:
            start_current_song(state["playlist_queue"][0])
        else:
            state["current_song"] = None
            state["is_playing"] = False
            state["position"] = 0.0
            state["started_at"] = None
            update_intro_skip(None)

    def apply_seek(position: float):
        state["position"] = max(0.0, float(position))
        state["started_at"] = time.time() if state["is_playing"] else None

    @socketio.on("connect")
    def handle_connect(auth=None):
        auth = auth if isinstance(auth, dict) else {}
        role = str(auth.get("role") or request.args.get("role", "player")).lower().strip()
        expected_token = os.getenv("KTV_LEGACY_CONTROL_TOKEN", "")
        supplied_token = str(auth.get("token") or "")
        authenticated = bool(expected_token) and hmac.compare_digest(supplied_token, expected_token)
        if role in CONTROL_ROLES and not authenticated:
            role = "player"
        state["client_roles"][request.sid] = role if role in (CONTROL_ROLES | {"player"}) else "player"
        emit("playback_snapshot", playback_snapshot())
        emit("playback_state", playback_snapshot())
        emit("update_queue_state", queue_payload())
        emit("apply_effect", state["effects"])

    @socketio.on("disconnect")
    def handle_disconnect():
        state["client_roles"].pop(request.sid, None)

    @socketio.on("add_to_queue")
    def handle_add_queue(data):
        if not can_control():
            return
        enqueue_song(data["filename"], data.get("title"))

    @socketio.on("queue_insert_next")
    def handle_insert_next(data):
        if not can_control():
            return
        enqueue_song(data["filename"], data.get("title"), insert_next=True)

    @socketio.on("queue_remove")
    def handle_queue_remove(data):
        if not can_control():
            return
        try:
            queue_id = int(data.get("queue_id", -1))
        except (TypeError, ValueError):
            return
        removed_current = state["current_song"] and state["current_song"]["queue_id"] == queue_id
        state["playlist_queue"] = [item for item in state["playlist_queue"] if item["queue_id"] != queue_id]
        if removed_current:
            if state["playlist_queue"]:
                start_current_song(state["playlist_queue"][0])
            else:
                state["current_song"] = None
                state["is_playing"] = False
                state["position"] = 0.0
                state["started_at"] = None
                update_intro_skip(None)
        emit_all_state()

    @socketio.on("request_play")
    def handle_request_play(data):
        if not can_control():
            return
        enqueue_song(data["filename"], data.get("title"), insert_next=True)

    @socketio.on("song_ended")
    def handle_song_ended(data=None):
        if not state["current_song"]:
            return
        if isinstance(data, dict) and data.get("filename") != state["current_song"]["filename"]:
            return
        advance_song()
        emit_all_state()

    @socketio.on("control")
    def handle_control(action):
        if not can_control():
            return
        if action == "cut":
            advance_song()
        elif action == "pause" and state["current_song"]:
            state["position"] = current_position()
            state["is_playing"] = not state["is_playing"]
            state["started_at"] = time.time() if state["is_playing"] else None
        elif action == "play" and state["current_song"]:
            state["is_playing"] = True
            state["started_at"] = time.time()
        elif action == "stop":
            state["is_playing"] = False
            state["position"] = 0.0
            state["started_at"] = None
        emit_all_state()

    @socketio.on("seek")
    def handle_seek(data):
        if not can_control() or not state["current_song"]:
            return
        try:
            position = float(data.get("position", 0.0))
        except (TypeError, ValueError):
            return
        apply_seek(position)
        emit_all_state()

    @socketio.on("skip_intro")
    def handle_skip_intro():
        if not can_control() or not state["current_song"]:
            return
        skip_to = state["intro_skip_to"]
        hide_after = state["intro_skip_hide_after"]
        if skip_to is None or hide_after is None:
            return
        if current_position() >= hide_after:
            return
        apply_seek(skip_to)
        state["intro_skip_used"] = True
        emit_all_state()

    @socketio.on("control_effect")
    def handle_effect(data):
        if not can_control():
            return
        state["effects"].update({key: value for key, value in data.items() if key in {"volume", "pitch"}})
        socketio.emit("apply_effect", state["effects"])
        emit_all_state()

    @socketio.on("change_track")
    def handle_track(mode):
        if not can_control():
            return
        if mode in {"original", "instrumental"}:
            state["audio_mode"] = mode
            socketio.emit("set_audio", mode)
            emit_all_state()

    @socketio.on("start_download")
    def handle_start_download(data):
        if state["is_processing"]:
            broadcast_log("⚠️ 系統正在處理其他歌曲，請稍候。")
            return
        if not can_control():
            return

        url = (data.get("url") or "").strip()
        try:
            url = _validate_youtube_url(url)
        except Exception as error:
            broadcast_log(f"❌ 無效連結: {error}")
            return
        manual_title = (data.get("title") or "").strip()
        options = {
            "stems": data.get("stems"),
            "device": data.get("device"),
            "separator_backend": data.get("separator_backend"),
            "alignment_backend": data.get("alignment_backend"),
        }
        manual_singer = (data.get("singer") or "").strip()
        manual_lyrics = (data.get("lyrics_text") or "").strip()

        try:
            if _is_playlist_url(url):
                selected_entries = data.get("playlist_entries") or _playlist_entries(url, settings)
                tasks = []
                for item in selected_entries:
                    item_url = item.get("url") or f"https://www.youtube.com/watch?v={item.get('id')}"
                    tasks.append(
                        {
                            "url": _validate_youtube_url(item_url),
                            "title": item.get("title") or item.get("id") or manual_title,
                            "singer": (item.get("singer") or "").strip(),
                            "lyrics_text": "",
                        }
                    )
            else:
                tasks = [{"url": url, "title": manual_title, "singer": manual_singer, "lyrics_text": manual_lyrics}]
        except Exception as error:
            broadcast_log(f"❌ playlist 讀取失敗: {error}")
            return

        state["is_processing"] = True

        def run_process():
            try:
                socketio.emit("task_status", {"status": "busy"})
                processor = KTVProcessor(settings=settings, log_cb=broadcast_log)
                total = len(tasks)
                succeeded = 0
                failed_items = []

                for index, task in enumerate(tasks, start=1):
                    title = task["title"]
                    _song_name, singer = _extract_title_artist(title)
                    provided_singer = (task.get("singer") or singer or "").strip()
                    provided_lyrics = (task.get("lyrics_text") or "").strip()
                    socketio.emit(
                        "task_progress",
                        {
                            "phase": "processing",
                            "current": index,
                            "total": total,
                            "title": title,
                            "attempt": 1,
                        },
                    )
                    success = False
                    for attempt in range(settings.download_retry_count + 1):
                        socketio.emit(
                            "task_progress",
                            {
                                "phase": "processing",
                                "current": index,
                                "total": total,
                                "title": title,
                                "attempt": attempt + 1,
                            },
                        )
                        success = processor.process_song(
                            task["url"],
                            title,
                            options={
                                **options,
                                "singer": provided_singer,
                                "lyrics_text": provided_lyrics,
                            },
                        )
                        if success:
                            break
                        time.sleep(max(0.0, settings.retry_delay_seconds))

                    if success:
                        succeeded += 1
                    else:
                        failed_items.append(title)

                update_library_index(settings.songs_dir, settings.library_index_path)
                if succeeded:
                    socketio.emit("refresh_list")

                socketio.emit(
                    "task_progress",
                    {
                        "phase": "done",
                        "total": total,
                        "success": succeeded,
                        "failed": failed_items,
                    },
                )
            finally:
                state["is_processing"] = False
                socketio.emit("task_status", {"status": "idle"})

        broadcast_log("=== 開始新任務 ===")
        threading.Thread(target=run_process, daemon=True).start()

    @socketio.on("update_ytdlp")
    def handle_update_ytdlp():
        if not can_control():
            return

        def run_update():
            socketio.emit("task_status", {"status": "busy"})
            broadcast_log("開始更新 yt-dlp 核心...")
            try:
                command = ["yt-dlp", "-U"]
                if settings.yt_dlp_path.exists():
                    command = [str(settings.yt_dlp_path), "-U"]
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.stdout:
                    broadcast_log(result.stdout)
                if result.stderr:
                    broadcast_log(result.stderr)
                broadcast_log("✅ yt-dlp 更新程序結束。")
            except Exception as error:
                broadcast_log(f"❌ 更新失敗: {error}")
            finally:
                socketio.emit("task_status", {"status": "idle"})

        threading.Thread(target=run_update, daemon=True).start()
