# OpenKTV-AI

OpenKTV-AI 使用 Python／Flask 下載與處理歌曲，Node.js 負責 KTV 播放器、歌房與即時播放同步。兩個服務共用 `ktv_songs/`，由 Python 維護 `ktv_songs/library.csv`，Node.js 直接讀取曲庫，不在 `app/public` 複製資料。

## 核心架構

- `core/web.py`：Flask API、背景處理工作與既有管理流程
- `core/server.py`：不依賴 Tk GUI 的 Flask 服務入口
- `core/config.py`：載入 `config.yaml`，並支援 `KTV_*` 環境變數覆寫
- `core/metadata.py`：YouTube 標題整理、Spotify 曲目比對、LRCLIB／Musixmatch 歌詞查詢
- `core/downloader.py`：`yt-dlp` 下載媒體
- `core/separators.py`：保留 Demucs／UVR／Hybrid 分離流程
- `core/uvr_separator.py`：UVR 分離與 vocals-only 去混響模型
- `core/unified_nightingale.py`：音訊後製、去混響、對齊模型、對白與輸出檔案總調度
- `app/server.js`：Node.js 播放、歌房、Socket.IO、QR Code、HTTP Range 媒體傳送

## 歌曲處理

1. yt-dlp 擷取 YouTube 標題，僅作為 Spotify 搜尋線索，並正規化歌名、歌手與檔名。
2. Spotify Web API 比對正規曲目並提供歌曲 metadata；比對分數未達門檻時不會下載，需檢查 API 設定或人工確認。
3. 依 Spotify 曲目向 LRCLIB 查詢歌詞，失敗時可使用 Musixmatch 備援；歌詞服務都無結果時才使用 Whisper 轉錄。
4. 保留既有 Hybrid 分離邏輯，原始 vocals 檔不會被覆寫。
5. vocals-only UVR DeEcho/DeReverb 處理產生 `.vocals.dereverbed.wav`；模型不可用時記錄錯誤並回退保守 DSP。
6. 以去混響 vocals 執行 `ctc`、`whisperx`、`qwen` 對齊，分別保存每個成功結果；平行度可設定。
7. 產生 KTV 歌詞、對白與伴奏檔案，更新單一正式曲庫 `library.csv`。

常見輸出包含 `.mp4`、`.instrumental.m4a`、`.vocals.wav`、`.vocals.dereverbed.wav`、`.dialogue.vocals.wav`、`.lrc` 及各對齊後端的 `.lyrics_alignment_<backend>.lrc`。

CSV 會記錄 Spotify 歌曲／歌手／專輯 metadata、來源、對齊模型與輸出路徑、純文字歌詞、處理狀態等欄位。Spotify 未提供的歌手性別等資料保留空白。純文字歌詞會去除時間戳及 KTV 格式標記，再將換行轉成空格。

## 啟動

先安裝 Python／Node.js 依賴並完成 `.env` 設定：

```bash
pip install -r requirements.txt
npm --prefix app install
```

桌面環境下，使用下列命令一鍵啟動 Flask 與 Node.js。服務就緒後會自動開啟新版 KTV 介面；關閉啟動器視窗會停止 Node.js 播放服務。

```bash
python main.py
```

沒有桌面 GUI 時，可在兩個終端分別執行 `python -m core.server` 與 `npm --prefix app run server`。Node 播放服務預設位於 `http://localhost:3000`；Flask 背景工作 API 預設位於 `http://localhost:5000`。啟動器會逐一檢查 `app/package.json` 宣告的 Node runtime dependencies；若有缺漏（例如 `express-rate-limit`），會顯示套件名稱及修復命令 `npm --prefix app install`，不會再只因 `node_modules` 資料夾存在就誤判依賴完整。Node 服務日誌寫入 `logs/node.log`。

## 設定與金鑰

- `config.yaml` 是兩個服務共用的非機密設定檔；可用 `KTV_CONFIG_PATH` 指定其他位置。啟動時會依專案根目錄的絕對路徑載入 `.env`，不受目前終端機工作目錄影響；同名的系統環境變數優先，啟動器日誌會列出被系統環境變數遮蔽的變數名稱（不顯示值）。
- 複製 `.env.example` 為 `.env`，設定 Spotify Developer Dashboard 的 Client ID/Secret。Musixmatch 是選用備援，需自行提供合法 API key。
- 使用 `python main.py` 桌面一鍵啟動時，若背景工作 token 遺漏、過短或不一致，啟動器會為該次執行產生一組暫存 token，不會寫回 `.env`。若分別啟動 Flask／Node，則 `KTV_JOB_API_TOKEN` 與 `KTV_PROCESSING_API_TOKEN` 必須是相同且至少 32 字元的隨機 token。
- 對齊模型預設逐一執行以限制記憶體和 GPU 佔用；可設定 `audio_processing.alignment.max_parallel_backends`。Raspberry Pi 4B 建議維持 1。
- audio-separator 會在首次執行時嘗試取得 `UVR-DeEcho-DeReverb.pth` 模型；若不可用，流程使用保守 DSP 備援。

## 新增／批次下載歌曲

- 桌面啟動後，在新版播放器右側建立歌房，下載區會出現「分離模式」及「歌詞對齊模式」選單。選好模式、貼上 YouTube 影片網址，再按「背景下載與處理」。
- 也可以在專案根目錄建立 `urls.txt`，每列使用 CSV 格式：`YouTube URL,分離模式,對齊模式`。模式支援 `hybrid`／`demucs`／`uvr` 及 `ctc`／`whisperx`／`qwen`。可有標題列及 `#` 註解；Hybrid 拼字請用 `hybrid`（腳本也會自動修正常見的 `hybird` typo）。
- 複製 `urls.txt.example` 作為格式範本，並在 Flask 與 Node 服務啟動後執行：

  ```bash
  python scripts/process_urls.py
  ```

  或指定清單：`python scripts/process_urls.py D:\OpenKTV\urls.txt`。腳本會按順序提交每首歌、輪詢工作狀態，避免同時塞滿背景處理容量；Flask `.env` 中的 `KTV_JOB_API_TOKEN` 必須是至少 32 字元的隨機值。
- 若播放器顯示 processing token 設定提醒，請將 `.env` 的 `KTV_JOB_API_TOKEN` 和 `KTV_PROCESSING_API_TOKEN` 設為相同且至少 32 字元的隨機值，然後重新啟動兩個服務。`KTV_SEPARATOR_BACKEND` 若設在 `.env`，請使用有效值 `hybrid`，不要寫成 `hybird`。

## 部署與監控

- systemd 範本：`deploy/openktv-flask.service`、`deploy/openktv-node.service`。範本預期安裝於 `/opt/openktv-ai`，環境變數檔位於權限受限的 `/etc/openktv-ai/openktv.env`。
- Nginx TLS 範例：`deploy/nginx-openktv.conf`，代理 Node Socket.IO、HTTP Range 媒體及 Flask processing API。瀏覽器麥克風權限需要 HTTPS（或 localhost）。
- Node `GET /health` 提供曲庫、歌房、Socket、CPU、記憶體和磁碟摘要；Flask `GET /health` 提供背景工作數、CPU、記憶體和歌曲數。
- systemd 會將服務 stdout/stderr 交由 journald 管理；Node 每 3 秒偵測 CSV 修改並重載曲庫。

## 驗證

```bash
python -m unittest discover -s tests
cd app && npm test
```
