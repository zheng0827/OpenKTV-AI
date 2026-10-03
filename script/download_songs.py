from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_settings
from core.unified_nightingale import KTVProcessor


def read_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.input:
        urls.extend(
            line.strip()
            for line in Path(args.input).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="直接逐首下載並處理 YouTube 歌曲，不啟動 Flask/Node 伺服器。")
    parser.add_argument("urls", nargs="*", help="YouTube 影片網址")
    parser.add_argument("--input", help="每行一個 YouTube 網址的文字檔")
    parser.add_argument("--title", default="", help="單首歌的標題或「歌手 - 歌名」")
    parser.add_argument("--artist", default="", help="指定歌手名稱")
    parser.add_argument("--lyrics", default="", help="指定歌詞文字")
    parser.add_argument("--separator", choices=("demucs", "uvr", "hybrid"))
    parser.add_argument("--alignment", choices=("ctc", "whisperx", "qwen"))
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"))
    args = parser.parse_args()
    urls = read_urls(args)
    if not urls:
        parser.error("請提供至少一個網址或 --input 檔案")
    if (args.title or args.artist or args.lyrics) and len(urls) != 1:
        parser.error("--title、--artist、--lyrics 僅能用於單一網址")

    settings = load_settings()
    processor = KTVProcessor(settings, print)
    options = {
        "singer": args.artist,
        "lyrics_text": args.lyrics,
        "separator_backend": args.separator or settings.separator_backend,
        "alignment_backend": args.alignment or settings.alignment_backend,
        "device": args.device or settings.device_preference,
    }
    failures = []
    for index, url in enumerate(urls, start=1):
        print(f"\n[{index}/{len(urls)}] 開始處理：{url}", flush=True)
        succeeded = False
        for attempt in range(max(1, settings.download_retry_count + 1)):
            if processor.process_song(url, args.title, options):
                succeeded = True
                break
            if attempt < settings.download_retry_count:
                print(f"處理失敗，稍後重試 ({attempt + 1}/{settings.download_retry_count})", flush=True)
                time.sleep(1)
        if not succeeded:
            failures.append(url)
    print(f"\n完成：{len(urls) - len(failures)} 成功，{len(failures)} 失敗。", flush=True)
    if failures:
        print("失敗網址：\n" + "\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
