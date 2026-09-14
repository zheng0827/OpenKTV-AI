import csv
import json
import multiprocessing
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox
from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit

# ==========================================
# GUI stdout/stderr capture
# ==========================================
system_log_queue = queue.Queue()


class GUIWriter:
    def __init__(self):
        self.null_file = open(os.devnull, "w")

    def write(self, data):
        if data and data.strip():
            system_log_queue.put(data.strip())

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        return self.null_file.fileno()


if getattr(sys, "frozen", False):
    sys_writer = GUIWriter()
    sys.stdout = sys_writer
    sys.stderr = sys_writer

# ==========================================
# Paths and config
# ==========================================
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg", "bin")
YT_DLP_PATH = os.path.join(BASE_DIR, "yt-dlp.exe")
SONGS_DIR = os.path.join(BASE_DIR, "ktv_songs")
TEMP_BASE_DIR = os.path.join(BASE_DIR, "temp_processing")
ASSETS_DIR = os.path.join(BASE_DIR, "ktv_assets")
INDEX_CSV_PATH = os.path.join(BASE_DIR, "song_index.csv")
DEFAULT_DEMUCS_STEMS = int(os.environ.get("KTV_DEMUCS_STEMS", "4"))

if os.path.exists(FFMPEG_DIR):
    os.environ["PATH"] += os.pathsep + FFMPEG_DIR
os.environ["PATH"] += os.pathsep + BASE_DIR

for p in [SONGS_DIR, TEMP_BASE_DIR, ASSETS_DIR]:
    os.makedirs(p, exist_ok=True)

# ==========================================
# Flask + SocketIO
# ==========================================
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config["SECRET_KEY"] = "ktv_secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


LOCAL_IP = get_local_ip()
PORT = 5000


def broadcast_log(msg):
    print(msg)
    socketio.emit("admin_log", {"msg": msg})


# ==========================================
# In-memory state
# ==========================================
state_lock = threading.Lock()
client_roles = {}
is_processing = False

playback_state = {
    "current_song": None,
    "status": "stopped",  # stopped / playing / paused
    "position": 0.0,
    "started_at": None,
    "updated_at": time.time(),
    "audio_mode": "original",
    "intro_skip_at": None,
}

queue_state = {
    "upcoming": [],
    "history": [],
}


def is_controller_sid(sid):
    role = client_roles.get(sid)
    return role in {"remote", "queue", "admin", "combo-remote"}


def _compute_position_locked():
    pos = float(playback_state["position"])
    if playback_state["status"] == "playing" and playback_state["started_at"]:
        pos += max(0.0, time.time() - float(playback_state["started_at"]))
    return max(0.0, pos)


def _song_title_from_filename(filename):
    return Path(filename).stem


def parse_first_lyric_seconds(lrc_path):
    if not os.path.exists(lrc_path):
        return None
    try:
        with open(lrc_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("%"):
                    parts = line[1:].strip().split(" ", 2)
                    if parts and parts[0]:
                        return float(parts[0])
    except Exception:
        return None
    return None


def get_song_asset_dir(filename):
    return os.path.join(ASSETS_DIR, Path(filename).stem)


def get_song_lrc_path(filename):
    return os.path.join(get_song_asset_dir(filename), "lyrics.ktv.lrc")


def get_intro_skip_at(filename):
    first = parse_first_lyric_seconds(get_song_lrc_path(filename))
    if first is None:
        return None
    return max(0.0, first - 5.0)


def build_snapshot_locked():
    return {
        "current_song": playback_state["current_song"],
        "status": playback_state["status"],
        "position": _compute_position_locked(),
        "audio_mode": playback_state["audio_mode"],
        "intro_skip_at": playback_state["intro_skip_at"],
        "server_time": time.time(),
        "queue": list(queue_state["upcoming"]),
        "history": list(queue_state["history"]),
    }


def emit_snapshot():
    with state_lock:
        snapshot = build_snapshot_locked()
    socketio.emit("playback_snapshot", snapshot, broadcast=True)
    socketio.emit(
        "update_queue",
        {"queue": snapshot["queue"], "history": snapshot["history"], "current": snapshot["current_song"]},
        broadcast=True,
    )


def start_song(filename, start_at=0.0):
    with state_lock:
        playback_state["current_song"] = filename
        playback_state["status"] = "playing"
        playback_state["position"] = float(start_at)
        playback_state["started_at"] = time.time()
        playback_state["updated_at"] = time.time()
        playback_state["intro_skip_at"] = get_intro_skip_at(filename)
        data = {
            "filename": filename,
            "title": _song_title_from_filename(filename),
            "start_at": float(start_at),
            "audio_mode": playback_state["audio_mode"],
            "intro_skip_at": playback_state["intro_skip_at"],
        }
    socketio.emit("play_video", data, broadcast=True)
    emit_snapshot()


def stop_playback():
    with state_lock:
        playback_state["status"] = "stopped"
        playback_state["position"] = 0.0
        playback_state["started_at"] = None
        playback_state["updated_at"] = time.time()
        playback_state["current_song"] = None
        playback_state["intro_skip_at"] = None
    socketio.emit("stop_video", broadcast=True)
    emit_snapshot()


def advance_to_next():
    with state_lock:
        current = playback_state["current_song"]
        if current:
            queue_state["history"].append(current)
        next_song = queue_state["upcoming"].pop(0) if queue_state["upcoming"] else None
    if next_song:
        start_song(next_song, start_at=0.0)
    else:
        stop_playback()


def _state_sync_loop():
    while True:
        socketio.sleep(1.0)
        emit_snapshot()


# ==========================================
# Song index
# ==========================================
def load_song_index():
    if not os.path.exists(INDEX_CSV_PATH):
        return {}
    rows = {}
    try:
        with open(INDEX_CSV_PATH, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows[row.get("filename", "")] = row
    except Exception:
        return {}
    return rows


def save_song_index(index_map):
    fields = ["filename", "song_name", "singer", "duration_sec", "lrc_path", "updated_at"]
    with open(INDEX_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for _, row in sorted(index_map.items()):
            writer.writerow({k: row.get(k, "") for k in fields})


def upsert_song_index(filename, song_name, singer, duration_sec, lrc_path):
    data = load_song_index()
    data[filename] = {
        "filename": filename,
        "song_name": song_name,
        "singer": singer,
        "duration_sec": str(duration_sec or ""),
        "lrc_path": lrc_path,
        "updated_at": datetime.utcnow().isoformat(),
    }
    save_song_index(data)


# ==========================================
# Lyric helpers
# ==========================================
def normalize_zh_tw(text):
    # Prefer OpenCC if installed.
    try:
        from opencc import OpenCC

        cc = OpenCC("s2t")
        return cc.convert(text)
    except Exception:
        return text


def parse_artist_title(raw_title, uploader):
    artist = uploader or ""
    song = raw_title or ""
    if " - " in raw_title:
        left, right = raw_title.split(" - ", 1)
        artist = left.strip() or artist
        song = right.strip()
    return artist.strip(), song.strip()


def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def search_lrclib(song_name, singer):
    try:
        query = urllib.parse.urlencode({"track_name": song_name, "artist_name": singer})
        url = f"https://lrclib.net/api/search?{query}"
        payload = fetch_json(url)
        if not payload:
            return None
        best = payload[0]
        synced = best.get("syncedLyrics") or ""
        plain = best.get("plainLyrics") or ""
        return {
            "plain": normalize_zh_tw(plain),
            "synced": normalize_zh_tw(synced),
            "song": best.get("trackName") or song_name,
            "artist": best.get("artistName") or singer,
            "album": best.get("albumName") or "",
        }
    except Exception:
        return None


def build_word_blocks_from_line(start_sec, end_sec, text):
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words:
        chars = [c for c in text.strip() if c]
        words = chars if chars else [text.strip()]
    if not words:
        return []
    duration = max(0.2, end_sec - start_sec)
    step = duration / len(words)
    out = []
    for i, w in enumerate(words):
        s = start_sec + step * i
        e = start_sec + step * (i + 1)
        out.append((s, e, w))
    return out


def parse_lrclib_synced_lines(synced_text):
    result = []
    for ln in synced_text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\](.*)", ln)
        if not m:
            continue
        mm = int(m.group(1))
        ss = int(m.group(2))
        frac = m.group(3) or "0"
        frac_sec = float(f"0.{frac.ljust(3, '0')}")
        t = mm * 60 + ss + frac_sec
        text = m.group(4).strip()
        result.append((t, text))
    lines = []
    for i, (st, txt) in enumerate(result):
        end = result[i + 1][0] if i + 1 < len(result) else st + 4.0
        if txt:
            lines.append((st, end, txt))
    return lines


def write_custom_ktv_lrc(target_path, song_id, song_name, singer, album, duration, plain_lyrics, synced_lyrics):
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    line_rows = parse_lrclib_synced_lines(synced_lyrics)

    with open(target_path, "w", encoding="utf-8") as f:
        f.write("@format ktv-lrc\n")
        f.write("@version 1\n\n")
        f.write(f"@id {song_id}\n\n")
        f.write(f"@song_name {song_name}\n")
        f.write(f"@singer {singer}\n")
        if album:
            f.write(f"@album {album}\n")
        f.write(f"@duration {int(duration) if duration else 0}\n\n")
        f.write("@lyrics zh-tw\n")
        f.write("@lyric_synced word\n\n")
        f.write("@lyric zh-tw\n")

        if line_rows:
            for st, ed, txt in line_rows:
                f.write(f"%{st:.3f} {ed:.3f} {txt}\n")
                for ws, we, w in build_word_blocks_from_line(st, ed, txt):
                    f.write(f"${ws:.3f} {we:.3f} {w}\n")
                f.write("\n")
        else:
            # fallback when synced lyrics not available
            rough_lines = [ln.strip() for ln in plain_lyrics.splitlines() if ln.strip()]
            now = 0.0
            for txt in rough_lines:
                st, ed = now, now + 4.0
                f.write(f"%{st:.3f} {ed:.3f} {txt}\n")
                for ws, we, w in build_word_blocks_from_line(st, ed, txt):
                    f.write(f"${ws:.3f} {we:.3f} {w}\n")
                f.write("\n")
                now += 4.2


def fetch_video_metadata(url):
    cmd = [
        YT_DLP_PATH if os.path.exists(YT_DLP_PATH) else "yt-dlp",
        "--skip-download",
        "--dump-single-json",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "yt-dlp metadata failed")
    data = json.loads(result.stdout)
    title = data.get("title", "")
    uploader = data.get("uploader", "")
    artist, song = parse_artist_title(title, uploader)
    return {
        "title": title,
        "uploader": uploader,
        "artist": artist,
        "song": song or title,
        "duration": data.get("duration") or 0,
        "webpage_url": data.get("webpage_url") or url,
    }


def get_playlist_entries(url):
    cmd = [
        YT_DLP_PATH if os.path.exists(YT_DLP_PATH) else "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "playlist parse failed")
    data = json.loads(result.stdout)
    entries = []
    for item in data.get("entries", []):
        video_id = item.get("id")
        if not video_id:
            continue
        entries.append(
            {
                "id": video_id,
                "title": item.get("title") or video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return entries


# ==========================================
# Demucs + FFmpeg pipeline
# ==========================================
def _run_demucs_process(input_wav_path, output_dir, stems):
    cmd = [sys.executable, "-m", "demucs.separate", "-o", output_dir]
    if stems == 2:
        cmd += ["--two-stems", "vocals"]
    cmd += [input_wav_path]
    subprocess.run(cmd, check=True)


def _find_demucs_stem_dir(output_dir):
    out = Path(output_dir)
    candidates = list(out.glob("**/input"))
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError("Demucs stem directory not found")


class KTVProcessor:
    def __init__(self, log_cb):
        self.log = log_cb

    def sanitize_filename(self, name):
        return "".join([c for c in name if c not in r'\\/:*?"<>|']).strip() or "untitled"

    def process_song(self, url, manual_title, stems=DEFAULT_DEMUCS_STEMS, expected_lyrics=None):
        job_temp_dir = None
        try:
            safe_title = self.sanitize_filename(manual_title)
            self.log(f"目標歌曲：{safe_title}")
            job_id = str(int(time.time() * 1000))
            job_temp_dir = os.path.join(TEMP_BASE_DIR, job_id)
            os.makedirs(job_temp_dir, exist_ok=True)

            temp_input = os.path.join(job_temp_dir, "input.mp4")
            temp_wav = os.path.join(job_temp_dir, "input.wav")
            temp_output = os.path.join(job_temp_dir, "output.mp4")
            acc_mix = os.path.join(job_temp_dir, "accompaniment.wav")

            self.log("步驟 1/6: 下載影片...")
            cmd_dl = [
                YT_DLP_PATH if os.path.exists(YT_DLP_PATH) else "yt-dlp",
                "--ffmpeg-location",
                FFMPEG_DIR,
                "--force-overwrites",
                "--no-playlist",
                "-f",
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
                "-o",
                temp_input,
                url,
            ]
            subprocess.run(
                cmd_dl,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            self.log("步驟 2/6: 提取音訊 WAV...")
            subprocess.run(
                ["ffmpeg", "-y", "-i", temp_input, "-vn", "-ac", "2", "-ar", "44100", temp_wav],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            stems = 2 if stems == 2 else 4
            self.log(f"步驟 3/6: Demucs 分離 ({stems} stems，預設4 stems)...")
            demucs_out_dir = os.path.join(job_temp_dir, "demucs_output")
            p = multiprocessing.Process(target=_run_demucs_process, args=(temp_wav, demucs_out_dir, stems))
            p.start()
            p.join()
            if p.exitcode != 0:
                raise Exception(f"Demucs 分離失敗，子進程異常結束 (Exit code: {p.exitcode})")

            stem_dir = _find_demucs_stem_dir(demucs_out_dir)
            vocals_path = os.path.join(stem_dir, "vocals.wav")
            if not os.path.exists(vocals_path):
                raise Exception("Demucs 失敗：找不到 vocals.wav")

            if stems == 2:
                no_vocals = os.path.join(stem_dir, "no_vocals.wav")
                if not os.path.exists(no_vocals):
                    raise Exception("Demucs 失敗：找不到 no_vocals.wav")
                shutil.copy(no_vocals, acc_mix)
            else:
                bass = os.path.join(stem_dir, "bass.wav")
                drums = os.path.join(stem_dir, "drums.wav")
                other = os.path.join(stem_dir, "other.wav")
                for pth in [bass, drums, other]:
                    if not os.path.exists(pth):
                        raise Exception(f"Demucs 失敗：找不到 {os.path.basename(pth)}")
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        bass,
                        "-i",
                        drums,
                        "-i",
                        other,
                        "-filter_complex",
                        "[0:a][1:a][2:a]amix=inputs=3:weights='1 1 1'[acc]",
                        "-map",
                        "[acc]",
                        acc_mix,
                    ],
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )

            self.log("步驟 4/6: FFmpeg 混音（pseudo-spatial, vocals 置中）...")
            filter_complex = (
                "[1:a]pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1[voc];"
                "[2:a]aformat=channel_layouts=stereo,asplit=2[accdry][accfx];"
                "[accfx]aecho=0.75:0.4:35:0.22,adelay=8|0[accrvb];"
                "[accdry][accrvb]amix=inputs=2:weights='1 0.35'[accmix];"
                "[voc][accmix]amix=inputs=2:weights='1 1.0'[aout]"
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    temp_input,
                    "-i",
                    vocals_path,
                    "-i",
                    acc_mix,
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "0:v",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    temp_output,
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            self.log("步驟 5/6: 產生歌詞索引與 ktv-lrc...")
            meta = fetch_video_metadata(url)
            artist = meta.get("artist") or ""
            song_name = manual_title or meta.get("song") or safe_title
            lyric = expected_lyrics or search_lrclib(song_name=song_name, singer=artist) or {
                "plain": "",
                "synced": "",
                "song": song_name,
                "artist": artist,
                "album": "",
            }

            final = os.path.join(SONGS_DIR, f"{safe_title}.mp4")
            if os.path.exists(final):
                final = os.path.join(SONGS_DIR, f"{safe_title}_{job_id}.mp4")
            final_filename = os.path.basename(final)

            asset_dir = get_song_asset_dir(final_filename)
            os.makedirs(asset_dir, exist_ok=True)
            vocals_keep_path = os.path.join(asset_dir, "vocals.wav")
            shutil.copy(vocals_path, vocals_keep_path)

            lrc_path = os.path.join(asset_dir, "lyrics.ktv.lrc")
            write_custom_ktv_lrc(
                target_path=lrc_path,
                song_id=job_id,
                song_name=normalize_zh_tw(lyric.get("song") or song_name),
                singer=normalize_zh_tw(lyric.get("artist") or artist),
                album=normalize_zh_tw(lyric.get("album") or ""),
                duration=meta.get("duration") or 0,
                plain_lyrics=lyric.get("plain") or "",
                synced_lyrics=lyric.get("synced") or "",
            )

            upsert_song_index(
                filename=final_filename,
                song_name=normalize_zh_tw(lyric.get("song") or song_name),
                singer=normalize_zh_tw(lyric.get("artist") or artist),
                duration_sec=meta.get("duration") or 0,
                lrc_path=lrc_path,
            )

            self.log(f"步驟 6/6: 儲存為 {os.path.basename(final)}")
            shutil.move(temp_output, final)
            self.log("✅ 製作完成！已自動同步至歌單。")
            return True
        except subprocess.CalledProcessError as e:
            self.log(f"❌ 執行失敗 (Code {e.returncode})")
            return False
        except Exception as e:
            self.log(f"❌ 錯誤: {e}")
            return False
        finally:
            if job_temp_dir and os.path.exists(job_temp_dir):
                shutil.rmtree(job_temp_dir, ignore_errors=True)


# ==========================================
# Routes
# ==========================================
@app.route("/")
def page_index():
    return render_template("remote.html")


@app.route("/player")
def page_player():
    return render_template("player.html")


@app.route("/remote")
def page_remote():
    return render_template("remote.html")


@app.route("/queue")
def page_queue():
    return render_template("queue.html")


@app.route("/combo")
def page_combo():
    return render_template("combo.html")


@app.route("/admin")
def page_admin():
    return render_template("admin.html")


@app.route("/songs/<path:filename>")
def serve_song(filename):
    return send_from_directory(SONGS_DIR, filename)


@app.route("/api/list")
def get_song_list():
    songs = sorted([f for f in os.listdir(SONGS_DIR) if f.lower().endswith(".mp4")])
    return json.dumps(songs, ensure_ascii=False)


@app.route("/api/library")
def get_song_library():
    songs = sorted([f for f in os.listdir(SONGS_DIR) if f.lower().endswith(".mp4")])
    index_map = load_song_index()
    data = []
    for f in songs:
        idx = index_map.get(f, {})
        data.append(
            {
                "filename": f,
                "title": idx.get("song_name") or Path(f).stem,
                "singer": idx.get("singer") or "",
                "duration_sec": idx.get("duration_sec") or "",
                "intro_skip_at": get_intro_skip_at(f),
            }
        )
    return json.dumps(data, ensure_ascii=False)


@app.route("/api/song_state")
def api_song_state():
    with state_lock:
        snapshot = build_snapshot_locked()
    return json.dumps(snapshot, ensure_ascii=False)


# ==========================================
# SocketIO events
# ==========================================
@socketio.on("connect")
def on_connect():
    emit("connected", {"ok": True})
    emit_snapshot()


@socketio.on("disconnect")
def on_disconnect():
    client_roles.pop(request.sid, None)


@socketio.on("register_client")
def register_client(data):
    role = (data or {}).get("role", "remote")
    client_roles[request.sid] = role
    emit("registered", {"role": role})
    emit_snapshot()


@socketio.on("request_state")
def request_state():
    with state_lock:
        snapshot = build_snapshot_locked()
    emit("playback_snapshot", snapshot)


@socketio.on("add_to_queue")
def add_to_queue(data):
    if not is_controller_sid(request.sid):
        return
    filename = (data or {}).get("filename")
    if not filename:
        return
    with state_lock:
        queue_state["upcoming"].append(filename)
        should_autoplay = playback_state["current_song"] is None and len(queue_state["upcoming"]) == 1
    if should_autoplay:
        with state_lock:
            next_song = queue_state["upcoming"].pop(0)
        start_song(next_song)
    else:
        emit_snapshot()


@socketio.on("queue_insert_next")
def queue_insert_next(data):
    if not is_controller_sid(request.sid):
        return
    filename = (data or {}).get("filename")
    if not filename:
        return
    with state_lock:
        if filename in queue_state["upcoming"]:
            queue_state["upcoming"].remove(filename)
        queue_state["upcoming"].insert(0, filename)
    emit_snapshot()


@socketio.on("queue_remove")
def queue_remove(data):
    if not is_controller_sid(request.sid):
        return
    idx = (data or {}).get("index")
    with state_lock:
        if isinstance(idx, int) and 0 <= idx < len(queue_state["upcoming"]):
            queue_state["upcoming"].pop(idx)
    emit_snapshot()


@socketio.on("queue_play_now")
def queue_play_now(data):
    if not is_controller_sid(request.sid):
        return
    filename = (data or {}).get("filename")
    if not filename:
        return
    with state_lock:
        if filename in queue_state["upcoming"]:
            queue_state["upcoming"].remove(filename)
        current = playback_state["current_song"]
        if current:
            queue_state["history"].append(current)
    start_song(filename, 0.0)


@socketio.on("song_ended")
def song_ended(_=None):
    # only player pages can report ended event
    if client_roles.get(request.sid) not in {"player", "combo-player"}:
        return
    advance_to_next()


@socketio.on("control")
def control(action):
    if not is_controller_sid(request.sid):
        return

    if isinstance(action, dict):
        action_name = action.get("action")
        seek_position = action.get("position")
    else:
        action_name = action
        seek_position = None

    if action_name == "cut":
        advance_to_next()
        return

    if action_name == "stop":
        stop_playback()
        return

    if action_name == "pause":
        with state_lock:
            if playback_state["status"] == "playing":
                playback_state["position"] = _compute_position_locked()
                playback_state["started_at"] = None
                playback_state["status"] = "paused"
                playback_state["updated_at"] = time.time()
            elif playback_state["status"] == "paused":
                playback_state["started_at"] = time.time()
                playback_state["status"] = "playing"
                playback_state["updated_at"] = time.time()
        socketio.emit("command", {"action": "pause_toggle"}, broadcast=True)
        emit_snapshot()
        return

    if action_name == "seek" and seek_position is not None:
        with state_lock:
            playback_state["position"] = max(0.0, float(seek_position))
            if playback_state["status"] == "playing":
                playback_state["started_at"] = time.time()
            playback_state["updated_at"] = time.time()
        socketio.emit("command", {"action": "seek", "position": float(seek_position)}, broadcast=True)
        emit_snapshot()
        return


@socketio.on("skip_intro")
def skip_intro():
    if not is_controller_sid(request.sid):
        return
    with state_lock:
        intro = playback_state["intro_skip_at"]
        cur = _compute_position_locked()
        can = intro is not None and cur < intro
    if not can:
        return
    control({"action": "seek", "position": intro})


@socketio.on("change_track")
def change_track(mode):
    if not is_controller_sid(request.sid):
        return
    with state_lock:
        playback_state["audio_mode"] = mode if mode in {"original", "instrumental"} else "original"
    emit("set_audio", playback_state["audio_mode"], broadcast=True)
    emit_snapshot()


@socketio.on("control_effect")
def control_effect(data):
    if not is_controller_sid(request.sid):
        return
    emit("apply_effect", data, broadcast=True)


@socketio.on("analyze_input")
def analyze_input(data):
    if not is_controller_sid(request.sid):
        return
    url = (data or {}).get("url", "").strip()
    if not url:
        emit("analyze_result", {"ok": False, "error": "請輸入 URL"})
        return
    try:
        is_playlist = bool(re.search(r"[?&]list=", url))
        if is_playlist:
            entries = get_playlist_entries(url)
            emit("analyze_result", {"ok": True, "mode": "playlist", "entries": entries})
            return

        meta = fetch_video_metadata(url)
        lyric = search_lrclib(meta.get("song") or meta.get("title"), meta.get("artist") or meta.get("uploader"))
        emit(
            "analyze_result",
            {
                "ok": True,
                "mode": "single",
                "item": {
                    "url": meta["webpage_url"],
                    "title": meta.get("song") or meta.get("title"),
                    "artist": meta.get("artist") or meta.get("uploader"),
                    "duration": meta.get("duration") or 0,
                    "lyrics_plain": (lyric or {}).get("plain", ""),
                    "lyrics_synced": (lyric or {}).get("synced", ""),
                    "album": (lyric or {}).get("album", ""),
                },
            },
        )
    except Exception as e:
        emit("analyze_result", {"ok": False, "error": str(e)})


@socketio.on("start_download")
def start_download(data):
    global is_processing
    if not is_controller_sid(request.sid):
        return

    if is_processing:
        emit("task_status", {"status": "busy", "message": "系統正在處理其他歌曲"})
        return

    payload = data or {}
    mode = payload.get("mode", "single")

    def run_process():
        global is_processing
        is_processing = True
        socketio.emit("task_status", {"status": "busy", "message": "任務開始"})

        processor = KTVProcessor(log_cb=broadcast_log)
        try:
            if mode == "playlist":
                entries = payload.get("entries") or []
                stems = int(payload.get("stems", DEFAULT_DEMUCS_STEMS))
                retries = 1
                total = len(entries)
                for idx, item in enumerate(entries, start=1):
                    url = item.get("url")
                    title = item.get("title") or f"playlist_{idx}"
                    ok = False
                    for attempt in range(retries + 1):
                        socketio.emit(
                            "task_progress",
                            {
                                "current": idx,
                                "total": total,
                                "title": title,
                                "attempt": attempt + 1,
                            },
                        )
                        ok = processor.process_song(url, title, stems=stems)
                        if ok:
                            break
                        broadcast_log(f"⚠️ [{idx}/{total}] {title} 失敗，重試 {attempt + 1}/{retries}")
                    if not ok:
                        broadcast_log(f"❌ [{idx}/{total}] {title} 最終失敗")
            else:
                url = payload.get("url")
                title = payload.get("title")
                stems = int(payload.get("stems", DEFAULT_DEMUCS_STEMS))
                expected_lyrics = payload.get("lyrics")
                processor.process_song(url, title, stems=stems, expected_lyrics=expected_lyrics)
        except Exception as e:
            broadcast_log(f"❌ 任務失敗: {e}")
            broadcast_log(traceback.format_exc())
        finally:
            is_processing = False
            socketio.emit("task_status", {"status": "idle", "message": "任務結束"})
            socketio.emit("refresh_list")

    broadcast_log("=== 開始新任務 ===")
    threading.Thread(target=run_process, daemon=True).start()


@socketio.on("update_ytdlp")
def handle_update_ytdlp():
    def run_update():
        socketio.emit("task_status", {"status": "busy", "message": "更新 yt-dlp"})
        broadcast_log("開始更新 yt-dlp 核心...")
        try:
            cmd = ["yt-dlp", "-U"]
            if os.path.exists(YT_DLP_PATH):
                cmd = [YT_DLP_PATH, "-U"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.stdout:
                broadcast_log(result.stdout)
            if result.stderr:
                broadcast_log(result.stderr)
            broadcast_log("✅ yt-dlp 更新程序結束。")
        except Exception as e:
            broadcast_log(f"❌ 更新失敗: {str(e)}")
        finally:
            socketio.emit("task_status", {"status": "idle", "message": "idle"})

    threading.Thread(target=run_update, daemon=True).start()


# ==========================================
# Server thread + GUI
# ==========================================
def run_server_thread():
    try:
        print("🚀 準備啟動 Flask 伺服器...")
        import logging
        from flask import cli

        cli.show_server_banner = lambda *args, **kwargs: None
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        socketio.start_background_task(_state_sync_loop)
        socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        print(f"❌ 伺服器啟動失敗: {e}")
        print(traceback.format_exc())


class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KTV 伺服器狀態")
        self.geometry("460x520")
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
        self.create_clickable_link(info_frame, "📺 播放端", f"http://{LOCAL_IP}:{PORT}/player", "blue")
        self.create_clickable_link(info_frame, "📱 遙控端", f"http://{LOCAL_IP}:{PORT}/remote", "#d32f2f")
        self.create_clickable_link(info_frame, "📋 點歌佇列", f"http://{LOCAL_IP}:{PORT}/queue", "#00897B")
        self.create_clickable_link(info_frame, "🕹️ 一體機", f"http://{LOCAL_IP}:{PORT}/combo", "#9C27B0")
        self.create_clickable_link(info_frame, "⚙️ 管理端", f"http://{LOCAL_IP}:{PORT}/admin", "#F57C00")

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
        link_lbl.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def update_stats(self):
        try:
            songs = [f for f in os.listdir(SONGS_DIR) if f.endswith(".mp4")]
            count = len(songs)
            total_size = sum(os.path.getsize(os.path.join(SONGS_DIR, f)) for f in songs)
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

    if shutil.which("ffmpeg") is None and not os.path.exists(FFMPEG_DIR):
        try:
            messagebox.showerror("錯誤", "找不到 FFmpeg\n請將 ffmpeg 資料夾放在程式同一目錄")
        except Exception:
            print("找不到 FFmpeg")
    else:
        t = threading.Thread(target=run_server_thread)
        t.daemon = True
        t.start()

        app_gui = ServerApp()
        app_gui.mainloop()
