from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from core import load_settings
from core.web import create_app, get_local_ip, register_socket_handlers


system_log_queue = queue.Queue()
node_process: subprocess.Popen | None = None
node_log_handle = None
node_port = 3000
node_url = ""


class GUIWriter:
    def __init__(self):
        self.null_file = open(os.devnull, "w", encoding="utf-8")

    def write(self, data):
        if data and data.strip():
            system_log_queue.put(data.strip())

    def flush(self):
        return None

    def isatty(self):
        return False

    def fileno(self):
        return self.null_file.fileno()


if getattr(sys, "frozen", False):
    sys_writer = GUIWriter()
    sys.stdout = sys_writer
    sys.stderr = sys_writer


settings = load_settings()
ROOT_DIR = settings.base_dir
APP_DIR = ROOT_DIR / "app"
os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(ROOT_DIR)
if settings.ffmpeg_dir.exists():
    os.environ["PATH"] += os.pathsep + str(settings.ffmpeg_dir)

settings.songs_dir.mkdir(parents=True, exist_ok=True)
settings.temp_base_dir.mkdir(parents=True, exist_ok=True)

app, socketio, settings = create_app(settings)
register_socket_handlers(socketio, settings=settings, log_cb=print)
LOCAL_IP = get_local_ip()


def configured_node_port() -> int:
    configured = os.getenv("KTV_NODE_PORT")
    if configured:
        try:
            port = int(configured)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass

    config_path = Path(os.getenv("KTV_CONFIG_PATH", ROOT_DIR / "config.yaml"))
    if not config_path.is_absolute():
        config_path = ROOT_DIR / config_path
    try:
        import yaml

        values = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        port = int(values.get("services", {}).get("node", {}).get("port", 3000))
        return port if 1 <= port <= 65535 else 3000
    except (OSError, TypeError, ValueError):
        return 3000


node_port = configured_node_port()
node_url = f"http://127.0.0.1:{node_port}"


def processing_token_warning() -> str:
    flask_token = os.getenv("KTV_JOB_API_TOKEN", "")
    node_token = os.getenv("KTV_PROCESSING_API_TOKEN", "")
    if (
        len(flask_token) < 32
        or flask_token.startswith("replace-")
        or flask_token != node_token
    ):
        return "背景下載尚未設定：請在 .env 將兩個 KTV processing token 設成相同的隨機長字串。"
    if not os.getenv("SPOTIFY_CLIENT_ID") or not os.getenv("SPOTIFY_CLIENT_SECRET"):
        return "Spotify API 憑證尚未設定，歌曲 metadata 比對與處理可能無法完成。"
    return ""


class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OpenKTV-AI 服務啟動器")
        self.geometry("560x520")
        self.minsize(500, 420)
        self.configure(bg="#f4f4f9")
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        tk.Label(
            self,
            text="🎤 OpenKTV-AI",
            font=("Microsoft JhengHei", 20, "bold"),
            fg="#4CAF50",
            bg="#f4f4f9",
        ).pack(pady=(14, 4))
        self.status = tk.Label(
            self,
            text="正在啟動 Flask 與 Node.js…",
            font=("Microsoft JhengHei", 11, "bold"),
            fg="#455a64",
            bg="#f4f4f9",
        )
        self.status.pack(pady=4)

        info_frame = tk.Frame(self, bg="white", bd=1, relief="solid")
        info_frame.pack(fill="x", padx=20, pady=8)
        self.create_clickable_link(info_frame, "🖥️ 新版 KTV 介面", f"{node_url}/", "blue")
        self.create_clickable_link(info_frame, "📺 播放器", f"{node_url}/player", "blue")
        self.create_clickable_link(info_frame, "📱 手機遙控", f"{node_url}/remote", "#d32f2f")

        stat_frame = tk.Frame(self, bg="#f4f4f9")
        stat_frame.pack(fill="x", padx=20, pady=4)
        self.lbl_count = tk.Label(
            stat_frame, text="總歌曲數: 載入中...", font=("Microsoft JhengHei", 11), bg="#f4f4f9"
        )
        self.lbl_count.pack(anchor="w")
        self.lbl_size = tk.Label(
            stat_frame, text="佔用空間: 載入中...", font=("Microsoft JhengHei", 11), bg="#f4f4f9"
        )
        self.lbl_size.pack(anchor="w", pady=4)
        tk.Label(
            stat_frame,
            text=f"Node 日誌：{ROOT_DIR / 'logs' / 'node.log'}",
            font=("Microsoft JhengHei", 9),
            fg="#666",
            bg="#f4f4f9",
            wraplength=510,
            justify="left",
        ).pack(anchor="w", pady=3)

        self.log_txt = tk.Text(
            self, height=10, state="disabled", bg="#222", fg="#0f0", font=("Consolas", 9)
        )
        self.log_txt.pack(fill="both", expand=True, padx=20, pady=10)
        self.update_stats()
        self.check_log_queue()
        self.start_services()

    def create_clickable_link(self, parent, text_prefix, url, color):
        frame = tk.Frame(parent, bg="white")
        frame.pack(pady=4, anchor="w", padx=10)
        tk.Label(
            frame, text=f"{text_prefix}: ", font=("Microsoft JhengHei", 10), bg="white"
        ).pack(side="left")
        link = tk.Label(
            frame, text=url, font=("Consolas", 10, "underline"), fg=color, bg="white", cursor="hand2"
        )
        link.pack(side="left")
        link.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target))

    def log_message(self, message: str):
        print(message)
        system_log_queue.put(str(message))

    def start_services(self):
        node_binary = shutil.which("node")
        if not node_binary:
            self.fail_startup("找不到 Node.js。請先安裝 Node.js 18.17 以上版本，再重新執行。")
            return
        if not (APP_DIR / "node_modules").is_dir():
            self.fail_startup(
                "Node.js 套件尚未安裝。請在專案目錄執行 `npm --prefix app ci`，再重新啟動。"
            )
            return

        def run_flask():
            try:
                self.log_message(f"啟動 Flask API：http://127.0.0.1:{settings.port}")
                socketio.run(
                    app,
                    host=settings.host,
                    port=settings.port,
                    debug=False,
                    allow_unsafe_werkzeug=True,
                )
            except Exception:
                self.log_message("Flask 啟動失敗：\n" + traceback.format_exc())

        threading.Thread(target=run_flask, daemon=True).start()

        global node_process, node_log_handle
        try:
            log_dir = ROOT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            node_log_handle = (log_dir / "node.log").open("a", encoding="utf-8")
            node_env = os.environ.copy()
            node_env["KTV_NODE_PORT"] = str(node_port)
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            node_process = subprocess.Popen(
                [node_binary, str(APP_DIR / "server.js")],
                cwd=ROOT_DIR,
                env=node_env,
                stdout=node_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            self.log_message(f"啟動 Node.js 播放服務：{node_url}")
        except OSError as error:
            self.fail_startup(f"無法啟動 Node.js：{error}")
            return

        threading.Thread(target=self.wait_for_node, daemon=True).start()
        self.after(1000, self.check_node_status)

    def wait_for_node(self):
        deadline = time.monotonic() + 45
        health_url = f"{node_url}/health"
        while time.monotonic() < deadline:
            if node_process is None or node_process.poll() is not None:
                self.after(
                    0,
                    lambda: self.fail_startup(
                        f"Node.js 播放服務意外結束。請查看 {ROOT_DIR / 'logs' / 'node.log'}"
                    ),
                )
                return
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    if response.status == 200:
                        self.after(0, self.node_ready)
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.5)
        self.after(
            0,
            lambda: self.fail_startup(
                f"等待 Node.js 服務逾時，請確認埠號 {node_port} 未被其他程式占用，並查看 Node 日誌。"
            ),
        )

    def node_ready(self):
        self.status.config(text=f"服務已啟動 · 新版介面：{node_url}", fg="#2e7d32")
        warning = processing_token_warning()
        if warning:
            self.log_message("設定提醒：" + warning)
        webbrowser.open(node_url)

    def check_node_status(self):
        if node_process is not None and node_process.poll() is not None:
            self.status.config(text="Node.js 播放服務已停止，請查看 Node 日誌。", fg="#c62828")
        self.after(2000, self.check_node_status)

    def fail_startup(self, message: str):
        self.status.config(text=message, fg="#c62828", wraplength=520, justify="center")
        self.log_message(message)
        messagebox.showerror("OpenKTV-AI 啟動失敗", message)

    def update_stats(self):
        try:
            songs = [f for f in os.listdir(settings.songs_dir) if f.lower().endswith(".mp4")]
            size_mb = sum(os.path.getsize(settings.songs_dir / f) for f in songs) / (1024 * 1024)
            self.lbl_count.config(text=f"🎵 總歌曲數: {len(songs)} 首")
            self.lbl_size.config(text=f"💾 佔用空間: {size_mb:.2f} MB")
        except OSError as error:
            self.lbl_count.config(text=f"無法讀取曲庫：{error}")
        self.after(5000, self.update_stats)

    def check_log_queue(self):
        while not system_log_queue.empty():
            try:
                message = system_log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_txt.config(state="normal")
            self.log_txt.insert("end", message + "\n")
            self.log_txt.see("end")
            self.log_txt.config(state="disabled")
        self.after(100, self.check_log_queue)

    def close_app(self):
        global node_process, node_log_handle
        if node_process is not None and node_process.poll() is None:
            node_process.terminate()
            try:
                node_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                node_process.kill()
        if node_log_handle is not None:
            node_log_handle.close()
            node_log_handle = None
        self.destroy()


if __name__ == "__main__":
    ServerApp().mainloop()
