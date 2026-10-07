import logging
import multiprocessing
import os
import queue
import shutil
import sys
import subprocess
import threading
import traceback
import webbrowser
import time
from urllib.error import URLError
from urllib.request import urlopen

import tkinter as tk
from tkinter import messagebox

from core import load_settings
from core.demucs_separator import ensure_demucs_weights
from core.web import create_app, get_local_ip, register_socket_handlers


system_log_queue = queue.Queue()


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
os.environ["PATH"] += os.pathsep + str(settings.base_dir)
if settings.ffmpeg_dir.exists():
    os.environ["PATH"] += os.pathsep + str(settings.ffmpeg_dir)

settings.songs_dir.mkdir(parents=True, exist_ok=True)
settings.temp_base_dir.mkdir(parents=True, exist_ok=True)

app, socketio, settings = create_app(settings)
register_socket_handlers(socketio, settings=settings, log_cb=print)

LOCAL_IP = get_local_ip()
node_process = None


def start_node_server():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("找不到 Node.js，請安裝 Node.js 並在 app 資料夾安裝套件。")
    node_process = subprocess.Popen(
        [node, "server.js"],
        cwd=settings.base_dir / "app",
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    health_url = f"http://127.0.0.1:{settings.node_port}/health"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if node_process.poll() is not None:
            raise RuntimeError("Node.js 播放服務未能啟動，請確認 app 套件已安裝且連接埠可用。")
        try:
            with urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    print(f"✅ Node.js 播放服務已就緒：http://{LOCAL_IP}:{settings.node_port}")
                    return node_process
        except (OSError, URLError):
            time.sleep(0.25)
    node_process.terminate()
    node_process.wait(timeout=5)
    raise RuntimeError("等待 Node.js 播放服務逾時。")


def stop_node_server():
    if node_process and node_process.poll() is None:
        node_process.terminate()
        try:
            node_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            node_process.kill()


def run_server_thread():
    try:
        print("🚀 準備啟動 Flask 伺服器...")
        from flask import cli

        cli.show_server_banner = lambda *args, **kwargs: None
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        socketio.run(
            app,
            host=settings.host,
            port=settings.port,
            debug=False,
            allow_unsafe_werkzeug=True,
        )
    except Exception as error:
        print(f"❌ 伺服器啟動失敗: {error}")
        print(traceback.format_exc())


class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KTV 伺服器狀態")
        self.geometry("450x500")
        self.configure(bg="#f4f4f9")

        tk.Label(
            self,
            text="🎤 KTV 系統運作中",
            font=("Microsoft JhengHei", 20, "bold"),
            fg="#4CAF50",
            bg="#f4f4f9",
        ).pack(pady=10)

        info_frame = tk.Frame(self, bg="white", bd=1, relief="solid")
        info_frame.pack(fill="x", padx=20, pady=5)

        self.create_clickable_link(info_frame, "🏠 首頁", f"http://{LOCAL_IP}:{settings.node_port}/", "blue")
        self.create_clickable_link(info_frame, "🎛️ 包廂主控台", f"http://{LOCAL_IP}:{settings.node_port}/console", "#9C27B0")
        self.create_clickable_link(info_frame, "📺 大螢幕播放器", f"http://{LOCAL_IP}:{settings.node_port}/player", "#d32f2f")
        self.create_clickable_link(info_frame, "⚙️ 管理端 (加歌用)", f"http://{LOCAL_IP}:{settings.port}/admin", "#F57C00")

        stat_frame = tk.Frame(self, bg="#f4f4f9")
        stat_frame.pack(fill="x", padx=20, pady=5)

        self.lbl_count = tk.Label(stat_frame, text="總歌曲數: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_count.pack(anchor="w")

        self.lbl_size = tk.Label(stat_frame, text="佔用空間: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_size.pack(anchor="w", pady=5)

        self.log_txt = tk.Text(self, height=8, state="disabled", bg="#222", fg="#0f0", font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True, padx=20, pady=10)

        self.update_stats()
        self.check_log_queue()

    def create_clickable_link(self, parent, text_prefix, url, color):
        frame = tk.Frame(parent, bg="white")
        frame.pack(pady=2, anchor="w", padx=10)
        tk.Label(frame, text=f"{text_prefix}: ", font=("Consolas", 11), bg="white").pack(side="left")
        link_lbl = tk.Label(frame, text=url, font=("Consolas", 11, "underline"), fg=color, bg="white", cursor="hand2")
        link_lbl.pack(side="left")
        link_lbl.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target))

    def update_stats(self):
        try:
            songs = [f for f in os.listdir(settings.songs_dir) if f.endswith(".mp4")]
            count = len(songs)
            total_size = sum(os.path.getsize(settings.songs_dir / f) for f in songs)
            size_mb = total_size / (1024 * 1024)
            self.lbl_count.config(text=f"🎵 總歌曲數: {count} 首")
            self.lbl_size.config(text=f"💾 佔用空間: {size_mb:.2f} MB")
        except Exception:
            pass
        self.after(5000, self.update_stats)

    def check_log_queue(self):
        try:
            while not system_log_queue.empty():
                msg = system_log_queue.get_nowait()
                self.log_txt.config(state="normal")
                self.log_txt.insert("end", msg + "\n")
                self.log_txt.see("end")
                self.log_txt.config(state="disabled")
        except Exception:
            pass
        self.after(100, self.check_log_queue)


if __name__ == "__main__":
    multiprocessing.freeze_support()

    if shutil.which("ffmpeg") is None and not settings.ffmpeg_dir.exists():
        try:
            messagebox.showerror("錯誤", "找不到 FFmpeg\n請將 ffmpeg 資料夾放在程式同一目錄")
        except Exception:
            print("找不到 FFmpeg")
    else:
        try:
            if settings.separator_backend in {"demucs", "hybrid"}:
                ensure_demucs_weights(
                    settings.demucs_model,
                    cache_dir=settings.demucs_cache_dir,
                    log_cb=print,
                )
        except Exception as error:
            try:
                messagebox.showerror("錯誤", str(error))
            except Exception:
                print(str(error))
            sys.exit(1)

        try:
            node_process = start_node_server()
            thread = threading.Thread(target=run_server_thread)
            thread.daemon = True
            thread.start()

            gui = ServerApp()
            gui.protocol("WM_DELETE_WINDOW", lambda: (stop_node_server(), gui.destroy()))
            gui.mainloop()
        except Exception as error:
            stop_node_server()
            try:
                messagebox.showerror("錯誤", str(error))
            except Exception:
                print(str(error))
            sys.exit(1)
