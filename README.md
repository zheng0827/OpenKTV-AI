# OpenKTV-AI

OpenKTV-AI 是一套 **本機 KTV 播放 + 歌曲處理 + 手機遙控 + Discord 整合** 系統。

- **Python / Flask**：下載、分離、歌詞對齊、曲庫維護
- **Node.js / Electron**：播放器、中控、遙控器、佇列與即時狀態
- **曲庫單一真實來源**：`library_index.csv`（由 Python 維護，Node 讀取）

---

## 快速開始

### 1) 安裝

```bash
pip install -r requirements.txt
npm --prefix app install
```

### 2) 啟動桌面版（建議）

```bash
npm --prefix app start
```

啟動後會：

1. 啟動並監控 Python 處理服務（`core.service`）
2. 啟動 Node 播放服務
3. 開啟中控畫面（`/operator`，相容 `/console`、`/ktv`）

> 若要純 Python 啟動器：`python main.py`

---

## 系統入口

- `/operator`（相容 `/console`、`/ktv`）：本機中控
- `/player`：本機播放器（可第二螢幕全螢幕）
- `/remote`：手機遙控器
- `/`、`/home`：首頁（區網使用者會導向遙控器）

> 中控與播放器頁面僅允許本機回環位址；遙控頁可供區網使用。

---

## 核心架構

### Python / core

- `core/web.py`：Flask API、下載任務、管理端路由
- `core/config.py`：`KTV_*` 環境變數與設定解析
- `core/unified_nightingale.py`：歌曲處理流程總調度
- `core/separators.py`：`demucs` / `uvr` / `hybrid` 後端選擇
- `core/transcription.py`：WhisperX 轉錄與對齊流程
- `core/whisper_alignment.py`、`core/ctc_alignment.py`、`core/qwen_alignment.py`：對齊後端
- `core/library.py`：曲庫 CSV schema 與原子更新
- `core/metadata.py`：YouTube 標題解析 + Gemini/Spotify metadata

### Node / app

- `app/server.js`：播放狀態權威、Socket.IO、搜尋、媒體 HTTP Range
- `app/main.js`：Electron 主程序（啟停 Python/Node）
- `app/public/*.html`：中控、播放器、遙控器前端

---

## 歌曲處理流程

1. `yt-dlp` 下載影片
2. 依 `KTV_SEPARATOR_BACKEND` 執行分離（Demucs / UVR / Hybrid）
3. 取得歌詞（LRCLIB 或手動）
4. 以去混響人聲進行 Whisper 轉錄 + `ctc` / `whisperx` / `qwen` 對齊
5. 輸出 KTV LRC（含對白偵測資訊）
6. 混音輸出伴奏與最終 MP4

### 主要產出檔案

- `<song>.mp4`
- `<song>.instrumental.m4a`
- `<song>.lyrics.vocals.wav`
- `<song>.dialogue.vocals.wav`
- `<song>.vocals.dereverbed.wav`
- `<song>.lrc`
- `<song>.dialogue_audit.json`

---

## 曲庫與 metadata

- 正式曲庫：`KTV_LIBRARY_INDEX_PATH`（預設 `ktv_songs/library_index.csv`）
- Python 以暫存檔 + 原子替換更新 CSV
- Node 不會建立第二份正式曲庫，只讀取同一份 CSV
- 曲庫管理頁可編輯 metadata / 刪除記錄（不刪媒體檔）

### metadata 補全

- 可用 YouTube 標題推測歌名/歌手
- 設定 `GEMINI_API_KEY` 可啟用 Gemini 解析
- 設定 `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` 可補齊專輯、年份、時長、曲風

---

## 直接批次處理（不啟動 Flask / Node）

```bash
python script/download_songs.py "https://www.youtube.com/watch?v=VIDEO_ID"
python script/download_songs.py --input urls.txt
python script/download_songs.py --title "歌手 - 歌名" --artist "歌手" "https://www.youtube.com/watch?v=VIDEO_ID"
```

`urls.txt`（pipe-delimited）範例：

```text
youtube_url|lrclib_url|separator_mode|alignment_model|title|artist|stems|device
https://www.youtube.com/watch?v=VIDEO_ID|https://lrclib.net/api/get/12345|uvr|whisperx|歌名|歌手|2|auto
https://youtu.be/ANOTHER_ID||hybrid|ctc|另一首歌|歌手|4|cuda
```

支援 TXT / CSV / JSON / JSONL，並支援別名欄位（如 `url`、`yt_url`、`separator`、`alignment`）。

---

## Discord 整合

### 1) 內建 `app/discord-bot.js`（slash）

啟動：

```bash
npm --prefix app run bot
```

必要 `.env`：

- `DISCORD_BOT_TOKEN`
- `DISCORD_CLIENT_ID`
- `KTV_BOT_API_TOKEN`（Node 與 Bot 要一致）
- `DISCORD_GUILD_ID`（可選，限伺服器內即時註冊）

提供 `/songs`、`/song`、`/room`、`/queue`、`/add`。

### 2) `discord-bot-standalone/`（slash + prefix）

獨立於 `app` / `core`，可直接呼叫 `script/download_songs.py`，並支援 `activitylaunch`。

詳見：

- `discord-bot-standalone/README.md`
- `discord-bot-standalone/activity-app/README.md`

### 3) Discord Embedded Activity

`discord-bot-standalone/activity-app` 提供真正語音房內嵌 Activity 子專案（非外跳頁）。

---

## 播放、遙控與手機麥克風

- Node 是播放狀態權威，透過 Socket.IO 同步播放/佇列/位置
- 媒體以 HTTP Range 供應，不做即時轉碼
- 伴奏模式在有有效 `.instrumental.m4a` 時會完整靜音影片原聲

### 手機即時麥克風

- 手機麥克風透過 WebRTC 單向傳到播放器
- Socket.IO 只做訊號交換，不回傳歌曲聲到手機
- 預設低延遲模式（關閉 AEC/NS/AGC）
- 每次只允許一支手機麥克風連線

### HTTPS / STUN / TURN

- 手機麥克風需安全來源（HTTPS 或 localhost）
- 可設定：
  - `KTV_TLS_CERT_PATH`
  - `KTV_TLS_KEY_PATH`
  - `KTV_HTTPS_PORT`（預設 3443）
- STUN/TURN：`KTV_STUN_URLS`、`KTV_TURN_URLS`、`KTV_TURN_USERNAME`、`KTV_TURN_CREDENTIAL`

---

## 安全性與權限建議

- `KTV_LIBRARY_ADMIN_TOKEN`：保護曲庫編輯/刪除
- `KTV_BOT_API_TOKEN`：保護 Bot 狀態 API
- `KTV_ROOM_ADMIN_TOKEN`（可選）：保護敏感 Socket 控制（切歌、seek、重排、播放控制）
- `KTV_SOCKET_SESSION_SECRET`：Socket session 簽章金鑰（必填，伺服器簽發角色 session）
- `KTV_PLAYBACK_STATE_PATH`（可選）：播放狀態落地檔案（預設 `logs/playback-state.json`）
- `KTV_AUDIT_LOG_PATH`（可選）：敏感操作審計日志（預設 `logs/audit.log`）

若啟用 `KTV_ROOM_ADMIN_TOKEN`，控制端可在瀏覽器設定：

```js
localStorage.setItem('ktvControlToken', '你的控制權杖')
```

> 請務必設定強隨機值，避免使用預設或弱字串。

---

## 對外部署基線（Reverse Proxy / TLS）

- 建議在 Nginx/Caddy/Traefik 後方部署，對外只開放 HTTPS
- Node 端可設定 `KTV_TRUST_PROXY=true`（或代理層級）以正確處理 `x-forwarded-proto`
- 服務已內建常見安全回應標頭（HSTS 僅在 HTTPS 請求回應）
- 建議代理層再加上：
  - `/media/*` 與 Socket 路徑分流限流
  - 長連線 timeout（給 Socket/WebRTC 訊號）
  - access/error log 分離與輪替

---

## 可觀測性與健康檢查

- `GET /health`：基礎存活檢查
- `GET /api/services`：Node/Python 服務狀態
- `GET /api/health/dashboard`：執行狀態 + 記憶體 + 佇列 + 即時指標摘要
- `GET /api/metrics`：JSON 指標（連線數、拒絕次數、搜尋延遲等）
- `GET /metrics`：Prometheus 文字格式

---

## 主要環境變數（精簡）

- 分離/對齊
  - `KTV_SEPARATOR_BACKEND=demucs|uvr|hybrid`
  - `KTV_ALIGNMENT_BACKEND=ctc|whisperx|qwen`
  - `KTV_DEVICE=auto|cuda|cpu`
- 服務連接埠
  - `KTV_PORT`（Python，預設 5000）
  - `KTV_NODE_PORT`（Node，預設 3000）
  - `KTV_TRUST_PROXY`（反向代理部署時建議設定）
- 路徑
  - `KTV_SONGS_DIR`
  - `KTV_LIBRARY_INDEX_PATH`
- 安全
  - `KTV_SECRET_KEY`
  - `KTV_SOCKET_SESSION_SECRET`
  - `KTV_LIBRARY_ADMIN_TOKEN`
  - `KTV_BOT_API_TOKEN`
  - `KTV_ROOM_ADMIN_TOKEN`（可選）

完整範例請見 `.env.example`。

---

## 開發模式啟動

分離啟動：

```bash
python -m core.service
npm --prefix app run server
```

測試：

```bash
npm --prefix app test
npm --prefix discord-bot-standalone test
npm --prefix discord-bot-standalone/activity-app test
```
