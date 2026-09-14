# -*- coding: utf-8 -*-

import os
import sys
import site
import re
import argparse
import json
import subprocess
import tempfile
from pathlib import Path


# ============================================================
# Windows NVIDIA CUDA DLL 自動掛載
# 必須放在 faster-whisper import 前面
# ============================================================

def inject_nvidia_dlls():

    search_paths = []

    try:
        search_paths.extend(site.getsitepackages())
    except Exception:
        pass

    try:
        search_paths.append(site.getusersitepackages())
    except Exception:
        pass

    search_paths.append(sys.prefix)

    seen = set()

    for base_path in search_paths:

        nvidia_dir = os.path.join(base_path, "nvidia")

        if not os.path.isdir(nvidia_dir):
            continue

        for root, dirs, files in os.walk(nvidia_dir):

            if os.path.basename(root).lower() != "bin":
                continue

            root = os.path.abspath(root)

            if root in seen:
                continue

            seen.add(root)

            # 加入 PATH
            current_path = os.environ.get("PATH", "")

            if root not in current_path.split(os.pathsep):
                os.environ["PATH"] = (
                    root +
                    os.pathsep +
                    current_path
                )

            # Python 3.8+ DLL search path
            if hasattr(os, "add_dll_directory"):

                try:
                    os.add_dll_directory(root)
                except Exception:
                    pass

            if "cublas64_12.dll" in files:
                print(
                    f"✅ 找到 CUDA DLL：{root}"
                )

    print("✅ NVIDIA DLL 搜尋路徑初始化完成")


if "--alignment-worker" not in sys.argv:
    inject_nvidia_dlls()


# ============================================================
# 第三方套件
# ============================================================

if "--alignment-worker" not in sys.argv:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("❌ 找不到 faster-whisper")
        print("請執行：pip install -U faster-whisper")
        sys.exit(1)


if "--alignment-worker" not in sys.argv:
    try:
        from opencc import OpenCC
    except ImportError:
        print("❌ 找不到 OpenCC")
        print("請執行：pip install opencc-python-reimplemented")
        sys.exit(1)


# ============================================================
# 簡體 → 繁體
# ============================================================

converter = (
    OpenCC("s2t")
    if "--alignment-worker" not in sys.argv
    else None
)


def traditional(text):
    if "--alignment-worker" in sys.argv:
        return text
    return converter.convert(text)


# ============================================================
# 文字清理
# ============================================================

def clean_text(text):

    text = traditional(text)

    # 去掉常見標點
    text = re.sub(
        r"[，。！？；：、,.!?;:]",
        "",
        text
    )

    # 多個空白合併
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# 中文 / 英文 token
# ============================================================

def tokenize(text):

    text = clean_text(text)

    result = []

    i = 0

    while i < len(text):

        ch = text[i]

        # 空白
        if ch.isspace():

            i += 1

            continue

        # 英文 / 數字
        if re.match(
            r"[A-Za-z0-9]",
            ch
        ):

            j = i + 1

            while j < len(text):

                if re.match(
                    r"[A-Za-z0-9']",
                    text[j]
                ):

                    j += 1

                else:

                    break

            result.append(
                text[i:j]
            )

            i = j

            continue

        # 中文 / CJK
        result.append(ch)

        i += 1

    return result


# ============================================================
# 歌詞讀取
# ============================================================

def load_lyrics(path):

    text = Path(path).read_text(
        encoding="utf-8-sig"
    )

    lines = []

    for raw in text.splitlines():

        line = raw.strip()

        if not line:
            continue

        # 自訂 metadata
        if line.startswith("@"):
            continue

        # 已經是 KTV-LRC
        if line.startswith(
            ("%", "$", "&")
        ):
            continue

        line = clean_text(line)

        if line:
            lines.append(line)

    return lines


# ============================================================
# 比對用文字
# ============================================================

def normalize_match(text):

    text = traditional(text)

    text = text.lower()

    text = re.sub(
        r"\s+",
        "",
        text
    )

    text = re.sub(
        r"[^\w\u3400-\u9fff]",
        "",
        text
    )

    return text


# ============================================================
# Levenshtein similarity
# ============================================================

def similarity(a, b):

    if a == b:
        return 1.0

    if not a or not b:
        return 0.0

    if len(a) < len(b):

        a, b = b, a

    previous = list(
        range(len(b) + 1)
    )

    for i, ca in enumerate(a, 1):

        current = [i]

        for j, cb in enumerate(b, 1):

            insert = (
                current[j - 1] + 1
            )

            delete = (
                previous[j] + 1
            )

            replace = (
                previous[j - 1]
                + (ca != cb)
            )

            current.append(
                min(
                    insert,
                    delete,
                    replace
                )
            )

        previous = current

    distance = previous[-1]

    return (
        1.0 -
        distance /
        max(len(a), len(b))
    )


# ============================================================
# Faster-Whisper
# ============================================================

def transcribe_audio(
    model,
    wav_path,
    language
):

    print()
    print(
        "開始辨識 "
        "(word_timestamps=True)..."
    )

    segments, info = model.transcribe(

        str(wav_path),

        language=language,

        beam_size=5,

        word_timestamps=True,

        vad_filter=True,

        condition_on_previous_text=True,

        temperature=0.0
    )

    segments = list(segments)

    result = []

    for segment in segments:

        words = []

        if segment.words:

            for word in segment.words:

                if (
                    word.start is None
                    or
                    word.end is None
                ):
                    continue

                text = clean_text(
                    word.word
                )

                if not text:
                    continue

                words.append({

                    "text": text,

                    "start":
                        float(word.start),

                    "end":
                        float(word.end)

                })

        result.append({

            "text":
                clean_text(
                    segment.text
                ),

            "start":
                float(segment.start),

            "end":
                float(segment.end),

            "words":
                words

        })

    return result, info


# ============================================================
# WhisperX 強制對齊
# ============================================================

def resolve_alignment_python(path):
    if path:
        return path

    candidates = [
        Path(".venv-whisperx") / "Scripts" / "python.exe",
        Path("venv-whisperx") / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())

    return None


def forced_align_segments(
    segments,
    wav_path,
    language,
    device,
    alignment_python
):
    """在獨立 Python 環境中執行 WhisperX，避免 Torch 版本互相污染。"""

    if not segments:
        return segments

    alignment_python = resolve_alignment_python(alignment_python)
    if not alignment_python:
        raise RuntimeError(
            "找不到 WhisperX Python。請用 --alignment-python 指定獨立 venv 的 python.exe"
        )

    with tempfile.TemporaryDirectory(prefix="ktv-align-") as temp_dir:
        temp_path = Path(temp_dir)
        input_path = temp_path / "input.json"
        output_path = temp_path / "output.json"
        input_path.write_text(
            json.dumps({
                "segments": segments,
                "wav": str(Path(wav_path).resolve()),
                "language": language,
                "device": device
            }, ensure_ascii=False),
            encoding="utf-8"
        )

        command = [
            alignment_python,
            str(Path(__file__).resolve()),
            "--alignment-worker",
            "--alignment-input",
            str(input_path),
            "--alignment-output",
            str(output_path)
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                "WhisperX 強制對齊程序失敗"
                + (f"：{detail}" if detail else "")
            )

        if not output_path.exists():
            raise RuntimeError("WhisperX 強制對齊沒有產生結果檔")

        aligned_segments = json.loads(
            output_path.read_text(encoding="utf-8")
        )
        if len(aligned_segments) != sum(
            bool(segment["text"]) for segment in segments
        ):
            raise RuntimeError(
                "WhisperX 強制對齊回傳的 segment 數量與辨識結果不一致"
            )

        result = []
        aligned_index = 0
        for segment in segments:
            updated = dict(segment)
            if segment["text"]:
                aligned_segment = aligned_segments[aligned_index]
                aligned_index += 1
                aligned_words = []

                for item in aligned_segment.get("chars", []):
                    text = clean_text(
                        item.get("char", item.get("text", ""))
                    )
                    start = item.get("start")
                    end = item.get("end")
                    if (
                        text
                        and start is not None
                        and end is not None
                        and float(end) >= float(start)
                    ):
                        aligned_words.append({
                            "text": text,
                            "start": float(start),
                            "end": float(end)
                        })

                if not aligned_words:
                    for item in aligned_segment.get("words", []):
                        text = clean_text(item.get("word", ""))
                        start = item.get("start")
                        end = item.get("end")
                        if (
                            text
                            and start is not None
                            and end is not None
                            and float(end) >= float(start)
                        ):
                            aligned_words.append({
                                "text": text,
                                "start": float(start),
                                "end": float(end)
                            })

                if aligned_words:
                    updated["words"] = aligned_words
                    updated["start"] = min(
                        item["start"] for item in aligned_words
                    )
                    updated["end"] = max(
                        item["end"] for item in aligned_words
                    )
            result.append(updated)

        return result


def run_alignment_worker():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alignment-worker", action="store_true")
    parser.add_argument("--alignment-input", required=True)
    parser.add_argument("--alignment-output", required=True)
    args = parser.parse_args()

    try:
        import whisperx
    except ImportError as exc:
        raise SystemExit(
            "此 Python 環境缺少 WhisperX，請在獨立 venv 安裝 whisperx"
        ) from exc

    payload = json.loads(
        Path(args.alignment_input).read_text(encoding="utf-8")
    )
    print("載入 WhisperX 強制對齊模型...", flush=True)
    alignment_model, metadata = whisperx.load_align_model(
        language_code=payload["language"],
        device=payload["device"]
    )
    aligned = whisperx.align(
        payload["segments"],
        alignment_model,
        metadata,
        whisperx.load_audio(payload["wav"]),
        payload["device"],
        return_char_alignments=True
    )
    Path(args.alignment_output).write_text(
        json.dumps(aligned.get("segments", []), ensure_ascii=False),
        encoding="utf-8"
    )


# ============================================================
# 找最符合的 Whisper segment
# ============================================================

def find_segment(
    lyric,
    segments,
    start_index
):

    target = normalize_match(
        lyric
    )

    best_index = None
    best_score = -1.0

    # 只往後搜尋有限範圍
    end_index = min(
        len(segments),
        start_index + 20
    )

    for i in range(
        start_index,
        end_index
    ):

        candidate = normalize_match(
            segments[i]["text"]
        )

        if not candidate:
            continue

        score = similarity(
            target,
            candidate
        )

        if score > best_score:

            best_score = score

            best_index = i

    return (
        best_index,
        best_score
    )


# ============================================================
# 中文逐字時間
# ============================================================

def interpolate_words(
    text,
    start,
    end
):

    words = tokenize(text)

    if not words:
        return []

    duration = max(
        0.001,
        end - start
    )

    count = len(words)

    result = []

    for index, word in enumerate(words):

        word_start = (
            start +
            duration *
            index /
            count
        )

        word_end = (
            start +
            duration *
            (index + 1) /
            count
        )

        result.append({

            "text": word,

            "start": word_start,

            "end": word_end

        })

    return result


# ============================================================
# 歌詞對齊
# ============================================================

def align_lyrics(
    lyrics,
    segments,
    language
):

    result = []

    search_index = 0

    for lyric in lyrics:

        index, score = find_segment(
            lyric,
            segments,
            search_index
        )

        if index is None:
            continue

        segment = segments[index]

        start = segment["start"]

        end = segment["end"]

        tokens = tokenize(lyric)

        # ----------------------------------------------------
        # 強制對齊結果
        #
        # 中文使用 chars，英文及其他語言通常使用 words。
        # 只有 token 數一致時才直接套用，避免辨識漏字造成錯位。
        # ----------------------------------------------------

        if len(segment["words"]) == len(tokens):

            words = []

            for token, whisper_word in zip(
                tokens,
                segment["words"]
            ):

                words.append({

                    "text": token,

                    "start":
                        whisper_word["start"],

                    "end":
                        whisper_word["end"]

                })

        # 對齊模型漏掉字元時，保留可用的整句時間範圍 fallback。

        else:

            words = interpolate_words(

                lyric,

                start,

                end

            )

        result.append({

            "start": start,

            "end": end,

            "text":
                traditional(lyric),

            "words": words,

            "score": score

        })

        search_index = index + 1

    return result


# ============================================================
# 對白候選
# ============================================================

def detect_dialogue(
    aligned_lines,
    segments
):

    used = []

    for line in aligned_lines:

        used.append(
            (
                line["start"],
                line["end"]
            )
        )

    dialogue = []

    for segment in segments:

        start = segment["start"]

        end = segment["end"]

        overlap = False

        for lyric_start, lyric_end in used:

            if (
                max(
                    start,
                    lyric_start
                )
                <
                min(
                    end,
                    lyric_end
                )
            ):

                overlap = True

                break

        if not overlap:

            if segment["text"]:

                dialogue.append({

                    "start": start,

                    "end": end,

                    "text":
                        traditional(
                            segment["text"]
                        )

                })

    return dialogue


# ============================================================
# 寫 KTV-LRC
# ============================================================

def write_lrc(
    output,
    song_name,
    singer,
    album,
    song_id,
    duration,
    language,
    lyrics,
    dialogue
):

    with open(
        output,
        "w",
        encoding="utf-8",
        newline="\n"
    ) as f:

        f.write(
            "@format ktv-lrc\n"
        )

        f.write(
            "@version 1\n"
        )

        f.write(
            f"@id {song_id}\n"
        )

        f.write(
            f"@song_name "
            f"{traditional(song_name)}, "
            f"zh-tw\n"
        )

        f.write(
            f"@singer "
            f"{traditional(singer)}, "
            f"zh-tw\n"
        )

        if album:

            f.write(
                f"@album "
                f"{traditional(album)}, "
                f"zh-tw\n"
            )

        f.write(
            f"@duration "
            f"{duration:.3f}\n"
        )

        f.write(
            f"@lyric_language "
            f"{language}\n"
        )

        f.write(
            "@lyric_synced word\n"
        )

        f.write("\n")

        # ====================================================
        # 歌詞
        # ====================================================

        f.write(
            f"@lyric {language}\n"
        )

        for line in lyrics:

            # %
            # 整句
            f.write(

                f"%"
                f"{line['start']:.3f} "
                f"{line['end']:.3f} "
                f"{line['text']}\n"

            )

            # $
            # word / 中文字
            for word in line["words"]:

                f.write(

                    f"$"
                    f"{word['start']:.3f} "
                    f"{word['end']:.3f} "
                    f"{word['text']}\n"

                )

        # ====================================================
        # MV 對白
        # ====================================================

        if dialogue:

            f.write("\n")

            f.write(
                "@dialogue\n"
            )

            for item in dialogue:

                f.write(

                    f"&"
                    f"{item['start']:.3f} "
                    f"{item['end']:.3f} "
                    f"{item['text']}\n"

                )


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=
        "Faster-Whisper KTV Lyrics Aligner"
    )

    parser.add_argument(
        "wav",
        help="含有人聲的 WAV"
    )

    parser.add_argument(
        "--lyrics",
        required=True,
        help="完整歌詞 TXT"
    )

    parser.add_argument(
        "--model",
        default="large-v3"
    )

    parser.add_argument(
        "--device",
        default="cuda",
        choices=[
            "cuda",
            "cpu"
        ]
    )

    parser.add_argument(
        "--compute-type",
        default="float16"
    )

    parser.add_argument(
        "--language",
        default="zh"
    )

    parser.add_argument(
        "--song-name",
        default="未命名"
    )

    parser.add_argument(
        "--singer",
        default="未知"
    )

    parser.add_argument(
        "--album",
        default=""
    )

    parser.add_argument(
        "--song-id",
        default=""
    )

    parser.add_argument(
        "--output",
        default=""
    )

    parser.add_argument(
        "--no-forced-alignment",
        action="store_true",
        help="停用 WhisperX 強制對齊，改用 Faster-Whisper 時間戳"
    )

    parser.add_argument(
        "--alignment-python",
        default="",
        help="獨立 WhisperX venv 的 python.exe 路徑"
    )

    args = parser.parse_args()

    wav = Path(args.wav)

    lyrics_file = Path(
        args.lyrics
    )

    if not wav.exists():

        raise SystemExit(
            f"❌ 找不到 WAV：{wav}"
        )

    if not lyrics_file.exists():

        raise SystemExit(
            f"❌ 找不到歌詞："
            f"{lyrics_file}"
        )

    output = (

        Path(args.output)

        if args.output

        else wav.with_suffix(".lrc")

    )

    print("=" * 70)

    print(
        "KTV Faster-Whisper Lyrics Aligner"
    )

    print("=" * 70)

    print(
        f"WAV       : {wav}"
    )

    print(
        f"Lyrics    : {lyrics_file}"
    )

    print(
        f"Model     : {args.model}"
    )

    print(
        f"Device    : {args.device}"
    )

    print(
        f"Compute   : {args.compute_type}"
    )

    print(
        f"Language  : {args.language}"
    )

    # ========================================================
    # 完整歌詞
    # ========================================================

    lyrics = load_lyrics(
        lyrics_file
    )

    if not lyrics:

        raise SystemExit(
            "❌ 歌詞檔沒有內容"
        )

    print(
        f"歌詞行數：{len(lyrics)}"
    )

    # ========================================================
    # 模型
    # ========================================================

    print()

    print(
        "載入 Faster-Whisper..."
    )

    model = WhisperModel(

        args.model,

        device=args.device,

        compute_type=args.compute_type

    )

    # ========================================================
    # ASR
    # ========================================================

    segments, info = transcribe_audio(

        model,

        wav,

        args.language

    )

    if not args.no_forced_alignment:
        segments = forced_align_segments(
            segments,
            wav,
            args.language,
            args.device,
            args.alignment_python
        )

    print()

    print(
        f"辨識語言："
        f"{info.language}"
    )

    print(
        f"語言機率："
        f"{info.language_probability:.3f}"
    )

    print(
        f"Whisper segments："
        f"{len(segments)}"
    )

    # ========================================================
    # 歌詞對齊
    # ========================================================

    print()

    print(
        "開始對齊完整歌詞..."
    )

    aligned = align_lyrics(

        lyrics,

        segments,

        args.language

    )

    # ========================================================
    # 對白
    # ========================================================

    dialogue = detect_dialogue(

        aligned,

        segments

    )

    # ========================================================
    # Duration
    # ========================================================

    duration = max(

        (
            x["end"]
            for x in segments
        ),

        default=0.0

    )

    # ========================================================
    # LRC
    # ========================================================

    write_lrc(

        output,

        args.song_name,

        args.singer,

        args.album,

        args.song_id,

        duration,

        args.language,

        aligned,

        dialogue

    )

    # ========================================================
    # 完成
    # ========================================================

    print()

    print("=" * 70)

    print("✅ 完成")

    print("=" * 70)

    print(
        f"LRC：{output}"
    )

    print(
        f"歌詞：{len(aligned)} 行"
    )

    print(
        f"對白候選：{len(dialogue)} 段"
    )

    print()

    if args.no_forced_alignment:
        print(
            "⚠️ 已停用強制對齊，中文逐字時間使用"
            " Whisper segment 範圍插值。"
        )
    else:
        print(
            "✅ 已使用 WhisperX forced alignment"
            " 產生逐字時間。"
        )


if __name__ == "__main__":
    if "--alignment-worker" in sys.argv:
        run_alignment_worker()
    else:
        main()