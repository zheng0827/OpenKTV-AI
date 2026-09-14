# OpenKTV-AI

本分支 (`copilot/migrate-audio-separation-to-demucs`) 目前採用 **Demucs + faster-whisper 對齊管線 + 伺服器權威播放同步**。

## 核心變更

1. **播放權限模型**
   - `/player` 僅負責唯讀播放顯示，不允許本地控制成為權威。
   - 控制操作僅允許 `remote / queue / admin / combo` 角色發送。
   - 伺服器以 `playback_snapshot`（含 `play/pause/seek/position/server_time`）持續同步所有裝置。

2. **Queue 重構**
   - 新增 `/queue` 頁面：點播、插播、刪除、目前播放與後續清單、已唱灰化。
   - `/remote` 新增跳轉 Queue 按鈕。
   - `/combo` 改為左側嵌入 player、右側可切 `remote / queue`。

3. **前奏跳過（Intro Skip）**
   - 由伺服器從 `.ktv.lrc` / `.lrc` 解析第一句時間，自動計算「第一句前 N 秒」（預設 5 秒）。
   - 僅在可用時間窗內顯示按鈕，超過第一句時間自動隱藏。

4. **Demucs + 歌詞對齊音訊管線**
   - 支援 `2 stems / 4 stems`，預設 `4 stems`。
   - 保留輸出 `vocals.wav`（`<song>.vocals.wav`）。
   - 混音策略固定為 `pseudo-spatial`：
     - vocals 置中
     - 不對 vocals 加 EQ
     - backing track 才套用輕量偽空間（delay/early reflections/reverb）
   - 另輸出 `<song>.instrumental.m4a` 供伴奏切換。
   - 整體流程：`下載影片 -> Demucs 分離 -> faster-whisper 句級定位 -> 強制對齊 word-level -> 對白回填 -> 輸出 .mp4/.m4a/.lrc/.wav -> 寫入曲庫索引`

5. **Admin 下載流程強化**
   - 支援單曲與 playlist 連結（含 playlist 預覽/勾選 modal）。
   - 任務進度回報（逐首、重試次數、成功/失敗統計）。
   - 下載失敗自動重試（可設定重試次數）。

6. **歌詞與索引**
   - 新增 `openktv_ai/lyrics_pipeline/` 專責處理：
     - faster-whisper 句級定位
     - 強制對齊 word-level（whisperx，可走獨立 python）
     - 對白偵測與回填 backing track
   - 產生 `ktv-lrc` 格式 `.lrc`，供播放器字幕特效使用。
   - 曲庫索引 CSV：`library_index.csv`。

---

## 安裝

```bash
pip install -r requirements.txt
```

> CUDA 請依 PyTorch 官方安裝對應 wheel，`KTV_DEVICE=auto` 會優先 CUDA，不可用時 fallback CPU。

---

## 啟動

```bash
python main.py
```

啟動前會檢查並下載 Demucs 權重（若尚未快取）。

可用頁面：
- `/player`
- `/remote`
- `/queue`
- `/admin`
- `/combo`

---

## 主要環境變數

- `KTV_SEPARATOR_STEMS=4|2`（預設 `4`）
- `KTV_DEVICE=auto|cuda|cpu`
- `KTV_DEMUCS_MODEL=htdemucs_ft`
- `KTV_MIX_MODE=pseudo-spatial`（固定策略）
- `KTV_WHISPER_MODEL=large-v3`
- `KTV_WHISPER_COMPUTE_TYPE=auto`
- `KTV_WHISPER_LANGUAGE=zh`
- `KTV_ALIGNMENT_PYTHON`（選填，指定 whisperx 獨立環境 python）
- `KTV_INTRO_SKIP_LEAD_SECONDS=5`
- `KTV_DOWNLOAD_RETRY_COUNT=2`
- `KTV_LIBRARY_INDEX_PATH`（預設 `ktv_songs/library_index.csv`）

Pseudo-spatial 參數：
- `KTV_PSEUDO_DELAY_MS`
- `KTV_PSEUDO_REFLECTION_GAIN`
- `KTV_PSEUDO_REVERB_ROOM`
- `KTV_PSEUDO_REVERB_DAMPING`
- `KTV_PSEUDO_BACKING_GAIN`

---

## 驗收建議（分階段）

1. **播放同步 + Queue + 前奏跳過**
   - 開三個裝置：`/player`、`/remote`、`/queue`
   - 從 remote 點歌，測試 pause/seek/cut 是否同步
   - 測試 skip intro 按鈕是否在第一句後消失

2. **Demucs / 混音**
   - 分別跑 `2 stems` 與 `4 stems`
   - 確認輸出 `.mp4 + .instrumental.m4a + .vocals.wav`
   - 切換 original/instrumental 檢查平滑度

3. **歌詞資料與索引**
   - 處理歌曲後確認 `library_index.csv` 更新
   - 檢查 `*.lrc`（ktv-lrc 格式）是否建立
