from __future__ import annotations

import time
from typing import Iterable


def normalize_priority(value, default: int = 0) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        priority = default
    return max(-100, min(100, priority))


def sort_pending_tasks(tasks: Iterable[dict]) -> list[dict]:
    return sorted(
        tasks,
        key=lambda item: (
            -normalize_priority(item.get("priority", 0)),
            float(item.get("created_at", time.time())),
            int(item.get("task_id", 0)),
        ),
    )


def queue_state(tasks: list[dict], active_task_id: int | None, history_limit: int = 20) -> dict:
    pending = sort_pending_tasks(item for item in tasks if item.get("status") == "pending")
    running = next((item for item in tasks if item.get("task_id") == active_task_id), None)
    finished = [item for item in tasks if item.get("status") in {"done", "failed", "cancelled"}]
    finished.sort(key=lambda item: float(item.get("finished_at", 0.0)), reverse=True)
    return {
        "active_task_id": active_task_id,
        "pending": [
            {
                "task_id": item.get("task_id"),
                "priority": item.get("priority"),
                "status": item.get("status"),
                "label": item.get("label"),
                "created_at": item.get("created_at"),
                "retries": item.get("retries", 0),
                "max_retries": item.get("max_retries", 0),
            }
            for item in pending
        ],
        "running": None if running is None else {
            "task_id": running.get("task_id"),
            "priority": running.get("priority"),
            "status": running.get("status"),
            "label": running.get("label"),
            "started_at": running.get("started_at"),
            "progress": running.get("progress", {}),
        },
        "recent": [
            {
                "task_id": item.get("task_id"),
                "priority": item.get("priority"),
                "status": item.get("status"),
                "label": item.get("label"),
                "finished_at": item.get("finished_at"),
                "retries": item.get("retries", 0),
                "max_retries": item.get("max_retries", 0),
                "summary": item.get("summary", {}),
            }
            for item in finished[:history_limit]
        ],
    }


def make_retry_task(original: dict, next_task_id: int, priority: int | None = None) -> dict:
    return {
        **original,
        "task_id": next_task_id,
        "status": "pending",
        "cancel_requested": False,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "progress": {},
        "summary": {},
        "priority": normalize_priority(original.get("priority", 0) if priority is None else priority),
        "retries": int(original.get("retries", 0)) + 1,
        "retry_of": int(original.get("task_id", 0)),
    }
