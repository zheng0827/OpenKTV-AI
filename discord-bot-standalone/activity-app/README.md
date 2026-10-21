# OpenKTV Discord Embedded Activity App

這是放在 `discord-bot-standalone/activity-app` 的 **Discord 內嵌 Activity 子專案**，不是外跳 OpenKTV 的連結頁。

## 架構

- `src/`：Activity 前端（Vite + Discord Embedded App SDK）
- `server/index.js`：Activity 後端
  - 提供 `/api/activity/config` 給前端取得 Client ID
  - 提供 `/api/discord/token` 做 OAuth2 code exchange
  - 代理 KTV API：`/api/ktv/room`、`/api/ktv/songs`

## 必要環境變數（寫在 repo 根目錄 `.env`）

- `DISCORD_ACTIVITY_CLIENT_ID`
- `DISCORD_ACTIVITY_CLIENT_SECRET`
- `DISCORD_ACTIVITY_APPLICATION_ID`（給 bot 建立語音房 Activity invite）
- `DISCORD_BOT_TOKEN`
- `KTV_BOT_API_TOKEN`
- `KTV_ROOM_ADMIN_TOKEN`（啟用後，敏感 Socket 控制指令需 control token）

## 可選環境變數

- `DISCORD_ACTIVITY_PORT`（預設 `4173`）
- `KTV_BOT_API_URL`（預設 `http://127.0.0.1:${KTV_NODE_PORT|3000}`）
- `DISCORD_ACTIVITY_ALLOWED_REDIRECT_ORIGINS`（逗號分隔 origin 白名單）
- `DISCORD_ACTIVITY_AUTH_CHALLENGE_TTL_MS`（一次性 OAuth challenge 存活毫秒，預設 5 分鐘）

## OAuth 安全強化

- 前端會先向 `/api/discord/auth-challenge` 取得一次性 `challengeId/state`
- 回傳授權碼後，`/api/discord/token` 會驗證 `challengeId + state + redirectUri`
- challenge 會短時效且用後即焚，降低重放與 CSRF 風險

## 開發與啟動

```bash
npm install --prefix discord-bot-standalone/activity-app
npm --prefix discord-bot-standalone/activity-app run build
npm --prefix discord-bot-standalone/activity-app run start
```

## 在語音房啟動內嵌 Activity

啟動 bot 後，在語音房執行：

- Slash: `/activitylaunch`
- Prefix: `!ktv activitylaunch`

Bot 會建立 `target_type=2` 的 Discord Activity invite，直接在語音房啟用。
