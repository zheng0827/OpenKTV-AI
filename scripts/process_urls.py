from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
SEPARATION_BACKENDS = {"demucs", "uvr", "hybrid"}
ALIGNMENT_BACKENDS = {"ctc", "whisperx", "qwen"}
TERMINAL_STATES = {"complete", "failed", "needs_review"}


def read_rows(path: Path) -> Iterator[tuple[int, str, str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for line_number, row in enumerate(csv.reader(source), start=1):
            if not row or not any(value.strip() for value in row):
                continue
            if row[0].lstrip().startswith("#"):
                continue
            if line_number == 1 and row[0].strip().lower() in {"url", "youtube_url"}:
                continue
            if len(row) != 3:
                raise ValueError(
                    f"第 {line_number} 行應為 3 欄：YouTube URL,分離模式,對齊模式"
                )

            url, separator, alignment = (value.strip() for value in row)
            separator = separator.lower()
            alignment = alignment.lower()
            if separator == "hybird":
                print(f"警告：第 {line_number} 行的 hybird 拼字已自動修正為 hybrid")
                separator = "hybrid"
            if not url.startswith(("https://www.youtube.com/", "https://youtube.com/", "https://youtu.be/")):
                raise ValueError(f"第 {line_number} 行不是支援的 YouTube HTTPS URL")
            if separator not in SEPARATION_BACKENDS:
                raise ValueError(
                    f"第 {line_number} 行分離模式須為 demucs、uvr 或 hybrid"
                )
            if alignment not in ALIGNMENT_BACKENDS:
                raise ValueError(f"第 {line_number} 行對齊模式須為 ctc、whisperx 或 qwen")
            yield line_number, url, separator, alignment


def request_json(url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            detail = {}
        return error.code, detail


def submit_and_wait(
    api_url: str,
    token: str,
    url: str,
    separator: str,
    alignment: str,
    poll_seconds: float,
    capacity_retries: int,
) -> str:
    payload = {
        "url": url,
        "options": {
            "separator_backend": separator,
            "alignment_backend": alignment,
        },
    }
    job_url = api_url.rstrip("/")
    for attempt in range(capacity_retries + 1):
        status_code, result = request_json(job_url, token, payload)
        if status_code == 202:
            break
        if status_code == 429 and attempt < capacity_retries:
            print("處理容量已滿，稍後重試提交…")
            time.sleep(poll_seconds)
            continue
        raise RuntimeError(f"提交失敗 HTTP {status_code}: {result.get('error', 'unknown_error')}")
    else:
        raise RuntimeError("提交背景工作失敗")

    job_id = result.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("API 未回傳有效的 job_id")
    print(f"已提交工作 {job_id}")
    while True:
        time.sleep(poll_seconds)
        status_code, result = request_json(f"{job_url}/{job_id}", token)
        if status_code != 200:
            raise RuntimeError(
                f"查詢工作失敗 HTTP {status_code}: {result.get('error', 'unknown_error')}"
            )
        state = result.get("status", "unknown")
        progress = result.get("progress", 0)
        attempt = result.get("attempt", 1)
        print(f"\r狀態：{state} · {progress}% · 第 {attempt} 次", end="", flush=True)
        if state in TERMINAL_STATES:
            print()
            if state != "complete":
                raise RuntimeError(f"工作結束狀態為 {state}: {result.get('error', '')}")
            return job_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="依 urls.txt 指定的分離與歌詞對齊模式，逐首提交歌曲處理工作。"
    )
    parser.add_argument("file", nargs="?", type=Path, default=ROOT_DIR / "urls.txt")
    parser.add_argument("--api-url", default=None, help="Flask jobs API URL (預設讀取 .env)")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--capacity-retries", type=int, default=12)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.capacity_retries < 0:
        parser.error("--poll-seconds 必須大於 0，--capacity-retries 不可小於 0")

    load_dotenv(ROOT_DIR / ".env", override=False)
    token = os.getenv("KTV_JOB_API_TOKEN", "")
    if len(token) < 32 or token.startswith("replace-"):
        print(
            "錯誤：請先在專案根目錄 .env 設定至少 32 字元的隨機 KTV_JOB_API_TOKEN。",
            file=sys.stderr,
        )
        return 2
    api_url = args.api_url or os.getenv(
        "KTV_PROCESSING_API_URL", "http://127.0.0.1:5000/api/jobs"
    )

    try:
        rows = list(read_rows(args.file))
    except (OSError, ValueError, csv.Error) as error:
        print(f"讀取清單失敗：{error}", file=sys.stderr)
        return 2
    if not rows:
        print(f"清單沒有可處理項目：{args.file}")
        return 0

    succeeded = 0
    failed = 0
    for index, (line_number, url, separator, alignment) in enumerate(rows, start=1):
        print(
            f"[{index}/{len(rows)}] 第 {line_number} 行 · "
            f"分離={separator} · 對齊={alignment} · {url}"
        )
        try:
            submit_and_wait(
                api_url,
                token,
                url,
                separator,
                alignment,
                args.poll_seconds,
                args.capacity_retries,
            )
            succeeded += 1
        except (OSError, RuntimeError, ValueError) as error:
            failed += 1
            print(f"處理失敗：{error}", file=sys.stderr)

    print(f"批次處理結束：成功 {succeeded} 首，失敗 {failed} 首。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
