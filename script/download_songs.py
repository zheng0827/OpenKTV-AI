from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALIASES = {
    "url": "youtube_url",
    "yt_url": "youtube_url",
    "youtube_url": "youtube_url",
    "video_url": "youtube_url",
    "source_url": "youtube_url",
    "lrclib_url": "lrclib_url",
    "lyrics_url": "lrclib_url",
    "separator": "separator_mode",
    "separate_mode": "separator_mode",
    "sparate_mode": "separator_mode",
    "separation_mode": "separator_mode",
    "separation_backend": "separator_mode",
    "separator_mode": "separator_mode",
    "alignment": "alignment_model",
    "text_alignment_model": "alignment_model",
    "text_align_model": "alignment_model",
    "text_aligan_model": "alignment_model",
    "alignment_model": "alignment_model",
    "singer": "artist",
    "artist": "artist",
    "song_title": "title",
    "title": "title",
    "youtube_title": "youtube_title",
    "lyrics": "lyrics",
    "lyrics_text": "lyrics",
    "stems": "stems",
    "device": "device",
    "title_is_auto": "title_is_auto",
}

PIPE_COLUMNS = (
    "youtube_url", "lrclib_url", "separator_mode", "alignment_model",
    "title", "artist", "stems", "device", "lyrics",
)


def _canonical_key(value: str) -> str:
    key = " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())
    return ALIASES.get(key.replace(" ", "_"), ALIASES.get(key, key.replace(" ", "_")))


def _normalize_row(row: dict) -> dict[str, str]:
    normalized = {}
    for key, value in row.items():
        if key is None or value is None:
            continue
        canonical = _canonical_key(str(key))
        if canonical:
            normalized[canonical] = str(value).strip()
    return normalized


def _parse_text_rows(path: Path) -> list[dict[str, str]]:
    lines = [
        line for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []

    delimiter = "|" if "|" in lines[0] else "\t" if "\t" in lines[0] else ","
    parsed = list(csv.reader(lines, delimiter=delimiter))
    header = [_canonical_key(value) for value in parsed[0]]
    has_header = "youtube_url" in header and any(
        value in {"lrclib_url", "separator_mode", "alignment_model", "title", "artist", "lyrics"}
        for value in header
    )
    if has_header:
        names = header
        rows = parsed[1:]
    elif delimiter in lines[0]:
        names = list(PIPE_COLUMNS)
        rows = parsed
    else:
        return [{"youtube_url": line.strip()} for line in lines]

    result = []
    for values in rows:
        if not values:
            continue
        result.append(_normalize_row(dict(zip(names, values))))
    return result


def read_input_jobs(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("songs", [payload])
        else:
            raise ValueError("JSON 檔案必須是歌曲物件或歌曲陣列")
        if not isinstance(rows, list):
            raise ValueError("JSON 檔案必須是歌曲物件或歌曲陣列")
        return [_normalize_row(row) for row in rows if isinstance(row, dict)]
    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(_normalize_row(value))
        return rows
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [_normalize_row(row) for row in csv.DictReader(handle)]
    return _parse_text_rows(path)


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be",
    }


def build_jobs(args: argparse.Namespace) -> list[dict[str, str]]:
    jobs = [{"youtube_url": url} for url in args.urls]
    if args.input:
        jobs.extend(read_input_jobs(Path(args.input)))

    overrides = {
        "title": args.title,
        "artist": args.artist,
        "lyrics": args.lyrics,
        "lrclib_url": args.lrclib_url,
        "separator_mode": args.separator,
        "alignment_model": args.alignment,
        "device": args.device,
        "stems": args.stems,
    }
    for job in jobs:
        job.update({key: str(value) for key, value in overrides.items() if value is not None})
    return [job for job in jobs if job.get("youtube_url")]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="直接逐首下載並處理 YouTube 歌曲，不啟動 Flask/Node 伺服器。"
    )
    parser.add_argument("urls", nargs="*", help="YouTube 影片網址")
    parser.add_argument("--input", help="歌曲輸入檔：.txt/.csv/.json/.jsonl")
    parser.add_argument("--title", help="所有歌曲使用的標題或「歌手 - 歌名」")
    parser.add_argument("--artist", help="指定歌手名稱")
    parser.add_argument("--lyrics", help="指定歌詞文字")
    parser.add_argument("--lrclib-url", help="指定 LRCLIB /api/get/{id} 歌詞網址")
    parser.add_argument("--separator", choices=("demucs", "uvr", "hybrid"))
    parser.add_argument("--alignment", choices=("ctc", "whisperx", "qwen"))
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--stems", choices=("2", "4"))
    args = parser.parse_args()

    try:
        jobs = build_jobs(args)
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as error:
        parser.error(f"讀取輸入檔失敗：{error}")
    if not jobs:
        parser.error("請提供至少一個 YouTube 網址或 --input 檔案")
    invalid_rows = [str(index) for index, job in enumerate(jobs, 1) if not is_youtube_url(job.get("youtube_url", ""))]
    if invalid_rows:
        parser.error(f"歌曲設定包含非 YouTube 或無效網址，項目：{', '.join(invalid_rows)}")
    for index, job in enumerate(jobs, 1):
        for field, allowed in {
            "separator_mode": {"demucs", "uvr", "hybrid"},
            "alignment_model": {"ctc", "whisperx", "qwen"},
            "device": {"auto", "cuda", "cpu"},
            "stems": {"2", "4"},
        }.items():
            if job.get(field) and job[field].lower() not in allowed:
                parser.error(f"第 {index} 首歌曲的 {field} 設定無效：{job[field]}")

    from core.config import load_settings
    from core.unified_nightingale import KTVProcessor

    settings = load_settings()
    processor = KTVProcessor(settings, print)
    failures = []
    for index, job in enumerate(jobs, start=1):
        url = job["youtube_url"]
        options = {
            "singer": job.get("artist", ""),
            "singer_is_manual": bool(job.get("artist")),
            "lyrics_text": job.get("lyrics", ""),
            "lrclib_url": job.get("lrclib_url", ""),
            "separator_backend": job.get("separator_mode") or settings.separator_backend,
            "alignment_backend": job.get("alignment_model") or settings.alignment_backend,
            "device": job.get("device") or settings.device_preference,
            "stems": job.get("stems") or settings.separator_stems,
            "youtube_title": job.get("youtube_title", ""),
            "title_is_auto": job.get("title_is_auto", "").lower() in {"1", "true", "yes"},
        }
        print(f"\n[{index}/{len(jobs)}] 開始處理：{url}", flush=True)
        succeeded = False
        for attempt in range(max(1, settings.download_retry_count + 1)):
            if processor.process_song(url, job.get("title", ""), options):
                succeeded = True
                break
            if attempt < settings.download_retry_count:
                print(
                    f"處理失敗，稍後重試 ({attempt + 1}/{settings.download_retry_count})",
                    flush=True,
                )
                time.sleep(1)
        if not succeeded:
            failures.append(url)
    print(f"\n完成：{len(jobs) - len(failures)} 成功，{len(failures)} 失敗。", flush=True)
    if failures:
        print("失敗網址：\n" + "\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
