import stable_whisper
import whisper
import torch
import numpy as np
from scipy.signal import butter, sosfilt

# ================= 參數設定 =================
songname = "多多 x 以捷 - 走建國路回家但後座少ㄌ泥"
audio_file = "../ktv_songs/" + songname + ".vocals.wav"
lyrics_file = "../lyrics/" + songname
output_lrc = "output_nightingale.lrc"
device = "cuda" if torch.cuda.is_available() else "cpu"
language = "zh"
# ============================================

# --- 1. 來自 audio.py 的音訊物理處理模組[cite: 8] ---
def detect_vocal_region(audio, sr=16000, win_secs=0.5, threshold_pct=0.15, min_consecutive=4, padding=1.0):
    """偵測真正有人聲的區段，直接剪掉超長前奏，根絕 Whisper 30秒崩潰死穴[cite: 8]"""
    window_samples = int(win_secs * sr)
    rms_values = [float(np.sqrt(np.mean(audio[i : i + window_samples] ** 2))) 
                  for i in range(0, len(audio), window_samples)]
    
    duration_secs = len(audio) / sr
    if not rms_values: return 0.0, duration_secs
    
    threshold = max(rms_values) * threshold_pct
    active = [rms >= threshold for rms in rms_values]
    
    vocal_start_win, vocal_end_win = 0, len(rms_values) - 1
    for i in range(len(active) - min_consecutive + 1):
        if all(active[i : i + min_consecutive]):
            vocal_start_win = i
            break
            
    for i in range(len(active) - 1, min_consecutive - 2, -1):
        start_check = max(i - min_consecutive + 1, 0)
        if all(active[start_check : start_check + min_consecutive]):
            vocal_end_win = i
            break
            
    vocal_start = max((vocal_start_win * win_secs) - padding, 0.0)
    vocal_end = min(((vocal_end_win + 1) * win_secs) + padding, duration_secs)
    return vocal_start, vocal_end

def highpass_filter(audio, sr=16000, cutoff_hz=80.0):
    """過濾 80Hz 以下的 BASS 與大鼓，防止對齊時吸附到鼓聲[cite: 8]"""
    sos = butter(5, cutoff_hz, btype="high", fs=sr, output="sos")
    return sosfilt(sos, audio.astype(np.float64)).astype(audio.dtype)

def normalize_rms(audio, target_rms=0.1, max_gain=10.0):
    """音量正規化，確保唇齒音也能被精準捕捉[cite: 8]"""
    raw_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if raw_rms > 0:
        audio = (audio * min(target_rms / raw_rms, max_gain)).astype(audio.dtype)
    return audio

# --- 2. 來自 cjk.py 的字元處理模組[cite: 9] ---
# Nightingale 內建的無發音符號庫，對齊時會自動排除[cite: 9]
NOISE_CHARS = set(" ,?¿.!¡;:\"%~`_+<>=…–—°´«»„“”'’/\\^。、，、；：！？「」『』【】〝〟〜〽‧～｛｝（）［］〈〉《》♪♫♬·・…‥─━‐‑‒–—―•※\t\n\r\u3000")

def clean_for_alignment(text: str) -> str:
    """剔除標點符號，只留給模型有實際發音的字[cite: 9]"""
    return "".join(ch for ch in text if ch not in NOISE_CHARS)

def merge_punct(entries: list[dict]) -> list[dict]:
    """將被剔除的標點符號，完美吸附回相鄰的字詞上供 KTV 顯示[cite: 9]"""
    out, pending_prefix = [], []
    for e in entries:
        if e.get("_punct"):
            if out: out[-1]["word"] += e["word"]
            else: pending_prefix.append(e["word"])
            continue
        cleaned = {k: v for k, v in e.items() if k != "_punct"}
        if pending_prefix:
            cleaned["word"] = "".join(pending_prefix) + cleaned["word"]
            pending_prefix = []
        out.append(cleaned)
    if pending_prefix and out:
        out[-1]["word"] += "".join(pending_prefix)
    return out

# --- 3. 來自 align.py 的安全插值模組[cite: 7] ---
def _interpolate_missing(word_entries: list[dict]):
    """安全填補漏字的空缺時間，絕對不往後偷借未來的時間[cite: 7]"""
    unset = [i for i, e in enumerate(word_entries) if e.get("start") is None]
    set_entries = [e for e in word_entries if e.get("start") is not None]
    if not unset or not set_entries: return
    
    for ui in unset:
        prev_end = set_entries[0]["start"]
        next_start = set_entries[-1]["end"]
        for j in range(ui - 1, -1, -1):
            if word_entries[j].get("start") is not None:
                prev_end = word_entries[j]["end"]
                break
        for j in range(ui + 1, len(word_entries)):
            if word_entries[j].get("start") is not None:
                next_start = word_entries[j]["start"]
                break
        mid = (prev_end + next_start) / 2
        word_entries[ui]["start"] = round(prev_end, 3)
        word_entries[ui]["end"] = round(mid, 3)

# --- 4. 主程式管線 ---
def main():
    print("讀取正確歌詞中...")
    with open(lyrics_file, "r", encoding="utf-8") as f:
        lyric_lines = [line.strip() for line in f if line.strip()]
    
    # 載入並進行 Nightingale 物理處理
    full_audio = whisper.load_audio(audio_file)
    vocal_start, vocal_end = detect_vocal_region(full_audio)
    
    trim_start = int(vocal_start * 16000)
    trim_end = int(vocal_end * 16000)
    audio = full_audio[trim_start:trim_end] # 只取有人聲的區段[cite: 13]
    
    audio = highpass_filter(audio)
    audio = normalize_rms(audio)
    
    model = stable_whisper.load_model("large-v3", device=device)

    # 第一趟：盲聽抓取全域文字 (用於分離對白)[cite: 13]
    print(f"[{device.upper()}] 正在盲聽轉錄 (捕捉隱藏對白)...")
    blind_result = model.transcribe(audio, language=language, vad=True, initial_prompt="\n".join(lyric_lines))
    
    # 將盲聽的時間加上裁切掉的前奏時間[cite: 13]
    blind_segments = []
    for seg in blind_result.segments:
        blind_segments.append({
            "start": seg.start + vocal_start,
            "end": seg.end + vocal_start,
            "text": seg.text.strip()
        })

    # 第二趟：精準強制對齊 (Nightingale cjk.py 映射法)[cite: 9]
    print("正在執行核心強制對齊...")
    cleaned_lyrics = "".join([clean_for_alignment(line) for line in lyric_lines])
    aligned_result = model.align(audio, cleaned_lyrics, language=language)
    
    # 將字元展開，並補回前奏時間
    aligned_chars = []
    for seg in aligned_result.segments:
        for w in seg.words:
            w_text = w.word.strip()
            if w_text:
                char_dur = (w.end - w.start) / len(w_text)
                for i, char in enumerate(w_text):
                    aligned_chars.append({
                        "char": char,
                        "start": w.start + (i * char_dur) + vocal_start,
                        "end": w.start + ((i + 1) * char_dur) + vocal_start
                    })

    # 將字元映射回原始句子[cite: 9]
    print("正在將時間軸安全映射回原始歌詞...")
    custom_segments = []
    cursor = 0
    
    for orig_line in lyric_lines:
        line_clean = clean_for_alignment(orig_line)
        length = len(line_clean)
        if length == 0: continue

        slice_chars = aligned_chars[cursor:cursor + length]
        cursor += length

        entries = []
        slice_idx = 0
        for orig_char in orig_line:
            if orig_char in NOISE_CHARS:
                entries.append({"word": orig_char, "_punct": True, "start": None, "end": None})
            else:
                c_data = slice_chars[slice_idx] if slice_idx < len(slice_chars) else {}
                entries.append({
                    "word": orig_char,
                    "start": c_data.get("start"),
                    "end": c_data.get("end"),
                    "_punct": False
                })
                slice_idx += 1

        # Nightingale 核心：插值填補與標點合併[cite: 7, 9]
        _interpolate_missing(entries)
        entries = merge_punct(entries)

        valid = [e for e in entries if e.get("start") is not None]
        if not valid: continue
        
        seg_start = valid[0]["start"]
        seg_end = valid[-1]["end"]
        if seg_end < seg_start: seg_end = seg_start

        custom_segments.append({
            "text": orig_line,
            "start": seg_start,
            "end": seg_end,
            "words": valid
        })

    # Nightingale 核心：邊界保護機制[cite: 7]
    for i in range(1, len(custom_segments)):
        prev = custom_segments[i - 1]
        cur = custom_segments[i]
        if cur["start"] < prev["end"]:
            cur["start"] = prev["end"]
            if cur["end"] < cur["start"]:
                cur["end"] = cur["start"]

    # 抓取對白：比對盲聽與歌詞的時間交集
    print("正在分離口白與歌詞...")
    dialogues = []
    for b_seg in blind_segments:
        is_lyric = False
        for c_seg in custom_segments:
            # 若盲聽聲音與最終歌詞時間有交集，視為歌詞
            if max(b_seg["start"], c_seg["start"]) < min(b_seg["end"], c_seg["end"]):
                is_lyric = True
                break
        if not is_lyric:
            dialogues.append(b_seg)

    # 輸出最終 LRC
    print(f"正在輸出至 {output_lrc}...")
    duration = (len(full_audio) / 16000)

    with open(output_lrc, "w", encoding="utf-8") as f:
        f.write("@format ktv-lrc\n@version 1\n\n@id 000000\n\n")
        f.write("@song_name 多多 x 以捷 - 走建國路回家但後座少ㄌ泥, zh-tw\n@singer , zh-tw\n")
        f.write(f"@duration {int(duration)}\n\n@lyrics zh-tw\n@lyric_synced word\n\n@lyric zh-tw\n")

        for seg in custom_segments:
            f.write(f"%{seg['start']:.3f} {seg['end']:.3f} {seg['text']}\n")
            for w in seg['words']:
                f.write(f"${w['start']:.3f} {w['end']:.3f} {w['word']}\n")
            f.write("\n")

        f.write("@dialogue\n")
        for d in dialogues:
            f.write(f"&{d['start']:.3f} {d['end']:.3f} {d['text']}\n")

    print(f"🎉 任務完成！完全移植 Nightingale 邏輯，時間軸穩定且安全。")

if __name__ == "__main__":
    main()