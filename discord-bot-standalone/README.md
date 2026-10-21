# Standalone Discord Bot

這個資料夾提供一個獨立於 `app` / `core` 的 Node.js Discord Bot，功能包含：

- 同時支援 **slash command** 與 **prefix command**（預設 prefix：`!ktv`）
- 查詢曲庫（`songs` / `song`）
- 讀取房間與佇列狀態（`room` / `queue`）
- 建立 KTV 與低延遲 MIC activity 連結（`activity`）
- 建立 Discord 語音房內嵌 Activity 邀請（`activitylaunch`）
- 呼叫 `script/download_songs.py` 批次新增歌曲（`add`）
- 關鍵字搜尋 YouTube 後，以按鈕切換頁面並多選歌曲再送交處理
- 內建子專案 `activity-app/`，可作為真正 Discord Embedded App Activity

## 安裝與啟動

```bash
npm install --prefix discord-bot-standalone
npm --prefix discord-bot-standalone run start
```

## 必要環境變數（可寫在專案根目錄 `.env`）

- `DISCORD_BOT_TOKEN`
- `DISCORD_CLIENT_ID`
- `KTV_BOT_API_TOKEN`（需與 Node 服務一致）

## 可選環境變數

- `DISCORD_GUILD_ID`：只註冊到指定伺服器（較快）
- `DISCORD_BOT_PREFIX`：prefix 指令前綴（預設 `!ktv`）
- `KTV_BOT_API_URL`：KTV Node API 位址（預設 `http://127.0.0.1:${KTV_NODE_PORT|3000}`）
- `KTV_ACTIVITY_BASE_URL`：`activity ktv` 要分享的入口網址
- `KTV_MIC_REMOTE_URL`：`activity mic` 要分享的低延遲 MIC 網址
- `DISCORD_ACTIVITY_APPLICATION_ID`：建立語音房內嵌 Activity invite 使用
- `KTV_ROOM_ADMIN_TOKEN`：啟用後，敏感 Socket 控制操作需提供 control token
- `KTV_LIBRARY_INDEX_PATH` / `KTV_SONGS_DIR`：曲庫 CSV 位置
- `KTV_PYTHON`：Python 執行檔名稱或路徑

## 指令

Slash 與 Prefix 對應如下：

- `/songs [keyword] [page]` ↔ `!ktv songs [keyword] [page]`
- `/song <keyword>` ↔ `!ktv song <keyword>`
- `/room` ↔ `!ktv room`
- `/queue` ↔ `!ktv queue`
- `/activity [ktv|mic]` ↔ `!ktv activity [ktv|mic]`
- `/activitylaunch` ↔ `!ktv activitylaunch`
- `/add <query>` ↔ `!ktv add <query>`

## Discord Embedded Activity 子專案

請看 `discord-bot-standalone/activity-app/README.md`。  
這個子專案會在 Discord 語音房以內嵌 Activity 方式執行，不是外跳 OpenKTV 網頁。

若有設定 `KTV_ROOM_ADMIN_TOKEN`，可在瀏覽器 Console 設定：

```js
localStorage.setItem('ktvControlToken', '你的控制權杖')
```

之後主控台/遙控器會把 token 帶入 Socket 連線，敏感操作（切歌、暫停、重排等）才會被允許。
