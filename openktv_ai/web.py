from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from typing import Callable

from flask import Blueprint, Flask, current_app, render_template, send_from_directory
from flask_socketio import SocketIO, emit

from .config import AppSettings, load_settings
from .processing import KTVProcessor


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _create_blueprint() -> Blueprint:
    bp = Blueprint("web", __name__)

    @bp.route("/player")
    def page_player():
        return render_template("player.html")

    @bp.route("/remote")
    def page_remote():
        return render_template("remote.html")

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
        songs = [file_name for file_name in os.listdir(settings.songs_dir) if file_name.lower().endswith(".mp4")]
        return json.dumps(songs)

    return bp


def create_app(settings: AppSettings | None = None) -> tuple[Flask, SocketIO, AppSettings]:
    app_settings = settings or load_settings()
    app = Flask(__name__, template_folder=str(app_settings.templates_dir))
    app.config.from_mapping(
        SECRET_KEY=app_settings.secret_key,
        APP_SETTINGS=app_settings,
    )
    app.register_blueprint(_create_blueprint())

    socketio = SocketIO(app, cors_allowed_origins="*")
    return app, socketio, app_settings


def register_socket_handlers(socketio: SocketIO, settings: AppSettings, log_cb: Callable[[str], None]):
    state = {"is_processing": False, "playlist_queue": []}

    def broadcast_log(message: str):
        log_cb(message)
        socketio.emit("admin_log", {"msg": message})

    def enqueue_song(filename: str):
        state["playlist_queue"].append(filename)
        emit("update_queue", state["playlist_queue"], broadcast=True)
        if len(state["playlist_queue"]) == 1:
            emit("play_video", {"filename": filename, "title": filename}, broadcast=True)

    @socketio.on("add_to_queue")
    def handle_add_queue(data):
        filename = data["filename"]
        enqueue_song(filename)

    @socketio.on("request_play")
    def handle_request_play(data):
        filename = data["filename"]
        enqueue_song(filename)

    @socketio.on("song_ended")
    def handle_song_ended():
        if state["playlist_queue"]:
            state["playlist_queue"].pop(0)
            emit("update_queue", state["playlist_queue"], broadcast=True)
            if state["playlist_queue"]:
                next_song = state["playlist_queue"][0]
                emit("play_video", {"filename": next_song, "title": next_song}, broadcast=True)
            else:
                emit("stop_video", broadcast=True)

    @socketio.on("control")
    def handle_control(action):
        if action == "cut":
            handle_song_ended()
        else:
            emit("command", action, broadcast=True)

    @socketio.on("control_effect")
    def handle_effect(data):
        emit("apply_effect", data, broadcast=True)

    @socketio.on("change_track")
    def handle_track(mode):
        emit("set_audio", mode, broadcast=True)

    @socketio.on("start_download")
    def handle_start_download(data):
        if state["is_processing"]:
            broadcast_log("⚠️ 系統正在處理其他歌曲，請稍候。")
            return

        url = data.get("url")
        title = data.get("title")
        options = {
            "stems": data.get("stems"),
            "mix_mode": data.get("mix_mode"),
            "device": data.get("device"),
        }

        def run_process():
            state["is_processing"] = True
            socketio.emit("task_status", {"status": "busy"})

            processor = KTVProcessor(settings=settings, log_cb=broadcast_log)
            success = processor.process_song(url, title, options=options)
            if success:
                socketio.emit("refresh_list")

            state["is_processing"] = False
            socketio.emit("task_status", {"status": "idle"})

        broadcast_log("=== 開始新任務 ===")
        threading.Thread(target=run_process, daemon=True).start()

    @socketio.on("update_ytdlp")
    def handle_update_ytdlp():
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
