import sys
import re
import librosa
import numpy as np

def detect_vocal_onsets(vocal_wav_path: str) -> np.ndarray:
    """載入純人聲檔案，偵測所有的起音 (Onset) 爆發點"""
    print(f"🔍 正在分析人聲起音點: {vocal_wav_path} ... (需時數十秒)")
    y, sr = librosa.load(vocal_wav_path, sr=16000)
    onset_frames = librosa.onset.onset_detect(
        y=y, sr=sr, wait=1, pre_max=1, post_max=1, pre_avg=3, post_avg=3
    )
    return librosa.frames_to_time(onset_frames, sr=sr)

def quantize_lrc(lrc_path: str, wav_path: str, out_path: str, tolerance: float = 0.15):
    """
    字元級網格吸附演算法 (Snap-to-Grid) + 彈性碰撞防重疊機制 (零延遲)
    """
    onsets = detect_vocal_onsets(wav_path)
    if len(onsets) == 0:
        print("❌ 無法從音檔中偵測到任何人聲起音。")
        return

    with open(lrc_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    def snap(time_val):
        idx = np.abs(onsets - time_val).argmin()
        nearest = onsets[idx]
        if abs(nearest - time_val) <= tolerance:
            return nearest
        return time_val

    parsed_items = []
    current_sentence = None

    # 1. 讀取並結構化整個 LRC 檔案
    for line in lines:
        raw_line = line.strip('\r\n')
        if not raw_line:
            parsed_items.append({"type": "empty", "text": ""})
            continue

        s_match = re.match(r'^%([\d\.]+)\s+([\d\.]+)\s+(.*)', raw_line)
        if s_match:
            current_sentence = {
                "type": "sentence",
                "start": float(s_match.group(1)),
                "end": float(s_match.group(2)),
                "text": s_match.group(3),
                "words": []
            }
            parsed_items.append(current_sentence)
            continue

        w_match = re.match(r'^\$([\d\.]+)\s+([\d\.]+)\s+(.*)', raw_line)
        if w_match and current_sentence is not None:
            current_sentence["words"].append({
                "orig_start": float(w_match.group(1)),
                "orig_end": float(w_match.group(2)),
                "new_start": 0.0,
                "new_end": 0.0,
                "text": w_match.group(3)
            })
            continue

        parsed_items.append({"type": "raw", "text": raw_line})

    # 將所有字元攤平，進行全域的碰撞處理
    all_words = []
    total_words = 0
    snapped_words = 0

    # 2. 進行時間軸吸附與初始化
    for item in parsed_items:
        if item["type"] == "sentence" and item["words"]:
            for w in item["words"]:
                new_s = snap(w["orig_start"])
                w["new_start"] = new_s
                # 初始化結尾時間 (不改變原本長度)
                w["new_end"] = max(new_s + 0.05, w["orig_end"])
                
                total_words += 1
                if abs(new_s - w["orig_start"]) > 0.001:
                    snapped_words += 1
                all_words.append(w)

    # 3. 彈性碰撞解決 (Elastic Collision Resolution) - 根治骨牌效應延遲！
    for i in range(1, len(all_words)):
        prev = all_words[i-1]
        curr = all_words[i]
        
        # 確保順序性：後一個字的起點，絕對不能早於前一個字的起點
        if curr["new_start"] < prev["new_start"]:
            curr["new_start"] = prev["new_start"]
            
        # 【關鍵】：處理重疊！如果當前字被前一個字的長尾音壓到
        if curr["new_start"] < prev["new_end"]:
            # 將當前字的起點往後推，完美銜接上一個字
            curr["new_start"] = prev["new_end"]
            # 注意：這裡「不」去動 curr 的 new_end，讓它的長度自然縮短來吸收碰撞！
            # 這就切斷了往後擠壓的骨牌效應！
            
        # 唯一的例外防呆：確保當前字被擠壓後，依然擁有極限最小發音長度 (0.05s)
        if curr["new_end"] < curr["new_start"] + 0.05:
            curr["new_end"] = curr["new_start"] + 0.05

    # 4. 句內無縫連接與句子邊界更新
    for sent in [i for i in parsed_items if i["type"] == "sentence" and i["words"]]:
        words = sent["words"]
        for k in range(len(words) - 1):
            # 讓句內的字元完美相連，畫面上不會有閃爍斷層
            if words[k]["new_end"] < words[k+1]["new_start"]:
                words[k]["new_end"] = words[k+1]["new_start"]
        
        # 結算句子的 % 起訖時間
        sent["start"] = words[0]["new_start"]
        sent["end"] = words[-1]["new_end"]

    # 5. 寫入輸出的檔案
    with open(out_path, 'w', encoding='utf-8') as f:
        for item in parsed_items:
            if item["type"] == "empty":
                f.write("\n")
            elif item["type"] == "raw":
                f.write(item["text"] + "\n")
            elif item["type"] == "sentence":
                f.write(f"%{item['start']:.3f} {item['end']:.3f} {item['text']}\n")
                for w in item["words"]:
                    f.write(f"${w['new_start']:.3f} {w['new_end']:.3f} {w['text']}\n")

    print("\n" + "="*50)
    print("🎯 字元級零延遲校正報告")
    print("="*50)
    print(f"總字數: {total_words}")
    print(f"成功微調吸附字數: {snapped_words} ({(snapped_words/total_words)*100:.1f}%)")
    print(f"吸附容忍半徑: ±{tolerance} 秒")
    print(f"✅ 跨句重疊已修復，且完全消除骨牌延遲現象！")
    print("="*50)

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("使用方式: python check_alignment.py <輸入LRC路徑> <人聲WAV路徑> <輸出LRC路徑>")
        sys.exit(1)
        
    lrc_in = sys.argv[1]
    wav_file = sys.argv[2]
    lrc_out = sys.argv[3]
    
    quantize_lrc(lrc_in, wav_file, lrc_out, tolerance=0.15)