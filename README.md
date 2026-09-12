# 🎤 OpenKTV-AI

OpenKTV-AI 是一個區網 KTV 系統：輸入 YouTube 連結後，會自動下載影片、抽取音訊、分離人聲/伴奏，再輸出可播放的 KTV 影片。

本次版本重點：
- 音訊分離核心由 **Spleeter 改為 Meta Demucs**（預設 `htdemucs_ft`）
- Flask 升級為較新版，並調整為 **app factory + config 分層**
- 混音由舊版硬分改為可切換策略（含 `legacy` 相容模式）

---

## 1) 安裝

```bash
pip install -r requirements.txt
pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu132
```

> PyTorch / torchaudio：
> - CPU：可直接安裝 requirements。
> - CUDA：請依官方頁面安裝對應 CUDA wheel（版本策略：`torch, torchaudio >=2.4,<2.7`）。
以 RTX 5060 8GB Laptop，CUDA Version 13.2 為例
```bash
pip install --pre torch torchaudio --index-url https://download.pytorch.org/whl/nightly/cu132
```
---

## 2) 啟動

```bash
python main.py
```

啟動前會先檢查 Demucs 權重；若本機沒有對應模型快取，系統會自動下載後再啟動服務。

啟動後可使用：
- `/player` 播放端
- `/remote` 遙控端
- `/admin` 後台（下載/分離/混音選項）
- `/combo` 一體機

---

## 3) 設定（環境變數）

### 分離核心
- `KTV_DEMUCS_MODEL`：預設 `htdemucs_ft`
- `KTV_SEPARATOR_STEMS`：`2`（預設）或 `4`
- `KTV_DEVICE`：`auto`（預設，優先 CUDA）、`cuda`、`cpu`

### 混音策略
- `KTV_MIX_MODE`：`pseudo-spatial`（預設）、`stereo-balance`、`legacy`

`pseudo-spatial` 可調參數（保守預設）：
- `KTV_PSEUDO_DELAY_MS`
- `KTV_PSEUDO_REFLECTION_GAIN`
- `KTV_PSEUDO_ORIGINAL_GAIN`
- `KTV_PSEUDO_ACCOMPANIMENT_GAIN`
- `KTV_PSEUDO_LEFT_ORIGINAL`
- `KTV_PSEUDO_LEFT_ACCOMPANIMENT`
- `KTV_PSEUDO_RIGHT_ORIGINAL`
- `KTV_PSEUDO_RIGHT_ACCOMPANIMENT`

`stereo-balance` 可調參數：
- `KTV_BALANCE_LEFT_ORIGINAL`
- `KTV_BALANCE_RIGHT_ORIGINAL`

### 路徑與伺服器
- `KTV_SONGS_DIR`
- `KTV_TEMP_DIR`
- `KTV_TEMPLATES_DIR`
- `KTV_FFMPEG_DIR`
- `KTV_HOST`
- `KTV_PORT`

---

## 4) 分離與輸出相容性

### 流程相容
仍維持原本流程：
1. 下載影片
2. Demucs 分離
3. FFmpeg 混音
4. 輸出 mp4 與 m4a 到曲庫

### 輸出路徑/命名
- 最終輸出仍是 `ktv_songs/<歌名>.mp4` 與 `ktv_songs/<歌名>.m4a`
- 若重名仍會自動加上 job id 後綴

### stems 相容層
- `2 stems`：使用 `vocals.wav + no_vocals.wav`
- `4 stems`：自動將 `drums+bass+other` 混成 `accompaniment.wav` 後續沿用既有流程

---

## 5) 混音模式說明

- `legacy`：舊行為（L: 原曲、R: 伴奏）                                              - **預計移除**
- `stereo-balance`：左右都保留兩者，只調整權重，聽感較溫和                            - **預計移除**
- `pseudo-spatial`（新預設）：小延遲 + 輕 EQ + 少量 early reflection + 保守聲像混合

---

## 6) 驗收步驟（手動）

### A. 2 stems（預設）
1. 到 `/admin` 貼 YouTube 連結
2. 選 `2 stems`
3. 開始製作，確認成功輸出

### B. 4 stems
1. 到 `/admin` 選 `4 stems`
2. 其他設定不變
3. 確認可成功輸出

### C. 三種混音策略
在 `/admin` 依序選 `legacy` / `stereo-balance` / `pseudo-spatial`，分別產出歌曲並聆聽差異。

### D. CUDA / CPU 切換
- `KTV_DEVICE=auto`：有 GPU 時用 CUDA，否則自動 fallback CPU
- `KTV_DEVICE=cuda`：若 CUDA 不可用，日誌顯示警告並 fallback CPU
- `KTV_DEVICE=cpu`：固定 CPU

---

## 7) Flask 架構調整

- `main.py`：啟動 GUI 與 server thread
- `openktv_ai/config.py`：設定集中管理
- `openktv_ai/web.py`：app factory、Blueprint routes、SocketIO handlers
- `openktv_ai/processing.py`：下載/分離/混音核心流程

---

## 8) TO DO LIST
- 可以輸入 YT playlist 連結並選取多個曲目，而不是一首一首歌慢慢下載
- 去抓同步或整篇的完整歌詞，搭配語音辨識AI模型，製作出類似 KTV 以秒為單位的歌詞字幕
- 改善串流模式

既有端點路徑維持不變，不需修改前端連結。
