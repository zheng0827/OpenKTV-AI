# OpenKTV-AI

目前主程式仍由 `main.py` 啟動，但執行時套件已切到 `core/`，並把音訊分離、歌詞來源、對齊、語言辨識、混音與網頁流程拆成可維護的模組。

## 核心架構

- `core/web.py`：Flask / Socket.IO 網頁與管理流程
- `core/config.py`：`KTV_*` 環境變數設定
- `core/downloader.py`：`yt-dlp` 下載影片
- `core/demucs_separator.py`：Demucs 分離與權重檢查
- `core/uvr_separator.py`：UVR 分離（透過 `audio-separator`）
- `core/separators.py`：Demucs / UVR / Hybrid 後端選擇
- `core/language_detection.py`：歌曲語言辨識
- `core/whisper_alignment.py`：WhisperX / CTC 對齊入口
- `core/ctc_alignment.py`：torchaudio CTC 強制對齊
- `core/qwen_alignment.py`：Qwen 強制對齊
- `core/transcription.py`：WhisperX 轉錄並套用對齊後端
- `core/lyrics_alignment.py`：lrclib 歌詞匹配、對白偵測、`.lrc` 輸出
- `core/processing.py`：保留既有 pseudo-spatial 音訊效果，並新增 lyrics/dialogue vocals 切分與混音工具
- `core/unified_nightingale.py`：整體流程總調度

## 新流程

1. `yt-dlp` 下載影片
2. 依 `KTV_SEPARATOR_BACKEND` 執行 `demucs` / `uvr` / `hybrid`
3. 從 `lrclib.net` 取得歌詞（或使用手動提供歌詞）
4. WhisperX 轉錄 + `ctc` / `whisperx` / `qwen` 對齊
5. 偵測對白時間並一起寫入 `ktv-lrc` `.lrc`
6. 依時間切出 `lyrics.vocals.wav` 與 `dialogue.vocals.wav`
7. 將 `dialogue.vocals.wav` 混入伴奏，輸出 `instrumental.m4a`
8. 將 `lyrics.vocals.wav + dialogue + backing track` 混回 `mp4`

## 產出檔案

每首歌會輸出：

- `<song>.mp4`
- `<song>.instrumental.m4a`
- `<song>.lyrics.vocals.wav`
- `<song>.dialogue.vocals.wav`
- `<song>.lrc`
- `<song>.dialogue_audit.json`

## 主要環境變數

- `KTV_SEPARATOR_BACKEND=demucs|uvr|hybrid`
- `KTV_SEPARATOR_STEMS=4|2`
- `KTV_DEMUCS_MODEL=htdemucs_ft`
- `KTV_UVR_MODEL=UVR-MDX-NET-Inst_HQ_320d.onnx`
- `KTV_UVR_MODEL_DIR`
- `KTV_DEVICE=auto|cuda|cpu`
- `KTV_WHISPER_MODEL=large-v3`
- `KTV_WHISPER_LANGUAGE=`（留空時自動辨識）
- `KTV_ALIGNMENT_BACKEND=ctc|whisperx|qwen`
- `KTV_WHISPER_COMPUTE_TYPE=auto`
- `KTV_INTRO_SKIP_LEAD_SECONDS=5`
- `KTV_DOWNLOAD_RETRY_COUNT=2`

## 啟動

```bash
pip install -r requirements.txt
python main.py
```

如果目前分離後端使用 `demucs` 或 `hybrid`，啟動前會先檢查 Demucs 權重。
