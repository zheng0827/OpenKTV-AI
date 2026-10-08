# OpenKTV-AI

Python/Flask 負責歌曲下載、音訊處理與管理頁；Node.js 負責播放器、遙控器、歌曲搜尋、佇列與即時播放狀態。兩端以正式曲庫 CSV 和媒體檔案目錄作為共用介面。

## 核心架構

- `core/web.py`：Flask 管理頁、下載工作與處理進度；播放器入口轉往 Node.js
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
- `core/library.py`：歌曲曲庫 schema、歌詞純文字化與原子 CSV 更新
- `core/metadata.py`：YouTube 標題解析、Gemini 歌名／歌手辨識及 Spotify 曲目 metadata
- `script/download_songs.py`：不啟動 Flask/Node.js、依序執行歌曲處理的 CLI
- `app/server.js`：Node.js 播放狀態、Socket.IO 同步、搜尋與 HTTP Range 媒體服務

## 新流程

1. `yt-dlp` 下載影片
2. 依 `KTV_SEPARATOR_BACKEND` 執行 `demucs` / `uvr` / `hybrid`
3. 從 `lrclib.net` 取得歌詞（或使用手動提供歌詞）
4. 將分離人聲高通與去混響後存為 `.vocals.dereverbed.wav`，再用它執行 Whisper 轉錄和 `ctc` / `whisperx` / `qwen` 對齊
5. 偵測對白時間並一起寫入 `ktv-lrc` `.lrc`
6. 依時間切出 `lyrics.vocals.wav` 與 `dialogue.vocals.wav`
7. 將 `dialogue.vocals.wav` 混入伴奏，輸出 `instrumental.m4a`
8. 將 `lyrics.vocals.wav + dialogue + backing track` 混回 `mp4`

## 產出檔案

每首歌會輸出：

- `<song>.mp4`
- `<song>.instrumental.m4a`（伴奏 + dialogue.vocals）
- `<song>.lyrics.vocals.wav`
- `<song>.dialogue.vocals.wav`
- `<song>.vocals.dereverbed.wav`（辨識／對齊使用，不取代原始人聲）
- `<song>.lrc`
- `<song>.dialogue_audit.json`

## 主要環境變數

- `KTV_SEPARATOR_BACKEND=demucs|uvr|hybrid`
- `KTV_SEPARATOR_STEMS=4|2`
- `KTV_DEMUCS_MODEL=htdemucs_ft`
- `KTV_UVR_MODEL=UVR-MDX-NET-Voc_FT.onnx`
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
npm --prefix app install
python main.py
```

`python main.py` 啟動 Flask 管理服務及 Node 播放服務。Node 首頁 `/` 提供歌曲搜尋與包廂狀態；`/console` 是整合大螢幕播放器、點歌、佇列、歌詞搜尋、混音及歌曲管理的一頁式主控台。`/player` 保留獨立大螢幕播放器，`/remote` 保留手機遙控器。歌曲處理仍由 Flask 管理服務提供；主控台的「新增歌曲」區塊直接嵌入管理頁。Node 直接讀取 `KTV_LIBRARY_INDEX_PATH` 指定的正式 CSV（預設 `ktv_songs/library_index.csv`），並由 `KTV_SONGS_DIR` 讀取媒體檔。

若預設分離後端使用 `demucs` 或 `hybrid`，啟動前會先檢查 Demucs 權重；若在管理頁單次切換到 `demucs` / `hybrid`，處理該首歌前也會補做權重檢查。

## 直接下載，不啟動伺服器

CLI 逐首處理 YouTube 影片，沿用歌曲分離、歌詞、去混響、對齊及歸檔流程，並直接更新正式 CSV。輸入檔可用每行一個網址的舊格式，也可用帶欄位名稱的 pipe-delimited `.txt`、CSV、JSON 或 JSONL，為每首歌分別設定歌詞來源及處理模型：

```bash
python script/download_songs.py "https://www.youtube.com/watch?v=VIDEO_ID"
python script/download_songs.py --input urls.txt
python script/download_songs.py --title "歌手 - 歌名" --artist "歌手" "https://www.youtube.com/watch?v=VIDEO_ID"
```

`urls.txt` 範例：

```text
youtube_url|lrclib_url|separator_mode|alignment_model|title|artist|stems|device
https://www.youtube.com/watch?v=VIDEO_ID|https://lrclib.net/api/get/12345|uvr|whisperx|歌名|歌手|2|auto
https://youtu.be/ANOTHER_ID||hybrid|ctc|另一首歌|歌手|4|cuda
```

欄位可用 `url`／`yt_url`、`lyrics_url`、`separator`／`separate_mode`、`alignment`／`text_alignment_model` 等別名；設定 `lrclib_url` 時必須是 LRCLIB 的 HTTPS `/api/get/{id}` API 網址。亦可用 JSON 陣列或 CSV 標頭提供相同欄位。空行與 `#` 註解會忽略。CLI 不會啟動 Flask/Node，也不會同時平行處理歌曲。

## Discord 機器人

在根目錄 `.env` 設定 `DISCORD_BOT_TOKEN`、`DISCORD_CLIENT_ID`、`KTV_BOT_API_TOKEN`，並在 Node KTV 服務使用相同的 `KTV_BOT_API_TOKEN`；`DISCORD_GUILD_ID` 可選，設定後會立即向指定伺服器註冊指令，未設定則註冊全域指令。啟動：

```bash
npm install --prefix app
npm --prefix app run bot
```

機器人提供 `/songs`、`/song`、`/room`、`/queue` 與 `/add`。`/add` 可輸入 YouTube 關鍵字、影片或播放清單網址，會先排除曲庫內歌曲，播放清單以分頁選單預設全選；接著依選擇的語言查 LRCLIB，讓使用者逐首選歌詞結果，再把設定交給獨立 Python 腳本執行。新增流程與長時間處理進度會顯示在呼叫指令的頻道；房間／佇列資訊透過 `KTV_BOT_API_TOKEN` 保護的 Node API 讀取。

## 曲庫與 metadata

Python 維護唯一正式 `library_index.csv`，使用 CSV 跳脫並透過同目錄暫存檔和原子替換更新。欄位包括穩定 ID、歌手、歌名、專輯、年份、音源時長、性別／曲風、純文字歌詞、分離／對齊模型、媒體檔名、來源、原始 YouTube 標題、處理狀態與建立時間。歌曲 ID 依歌手與歌名穩定產生；metadata 不可得時使用空值，重複歌曲合併到同一曲庫項目，檔名衝突則為成品產生唯一檔名。純文字歌詞從 LRC 移除時間戳與 KTV 格式標記後以空格串接。

下載前會擷取 YouTube 標題；設定 `GEMINI_API_KEY` 時會用 Gemini 推測正式歌名與歌手，否則使用標題格式和上傳者進行本機解析。設定 `SPOTIFY_CLIENT_ID` 和 `SPOTIFY_CLIENT_SECRET` 後，會以 Spotify Web API 搜尋曲目，補入專輯、發行年份、時長及可取得的曲風；無憑證、搜尋不到或 API 無資料時欄位留空。Spotify 不提供歌手性別資料，因此該欄位目前維持空值。

## Node 播放與麥克風

Node 伺服器是播放狀態權威來源，透過 Socket.IO 同步佇列、播放／暫停、播放位置、音軌與音量。播放位置以伺服器時間週期校正，重連時重新接收狀態快照。媒體採支援 HTTP Range 的原始檔案串流，不做即時轉碼。歌曲處理輸出的 MP4 使用 `-movflags +faststart` 將索引 metadata 移至檔案前端，降低播放器開始播放前的等待時間。伴奏檔缺失或無效時會拒絕切換並回報錯誤，不會把影片原唱混入伴奏。

播放器的麥克風路徑獨立於歌曲伴奏混音，包含瀏覽器回音消除、噪音抑制、自動增益、高通、動態壓縮、限幅與可調乾／混響音量。回音消除效果依麥克風、喇叭／耳機、瀏覽器與作業系統而異；喇叭聲仍可能經空氣回灌，建議使用耳機並在目標設備上實測。非 localhost 的麥克風存取通常需要 HTTPS。
