from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def gpu_model(_name: str):
    held: list[object] = []
    try:
        yield held
    finally:
        held.clear()
        hard_free_gpu(_name)


def hard_free_gpu(_label: str | None = None) -> None:
    try:
        import gc

        gc.collect()
    except Exception:
        pass

    try:
        import torch  # pylint: disable=import-outside-toplevel

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if hasattr(torch.cuda, "ipc_collect"):
                torch.cuda.ipc_collect()
        if hasattr(torch, "mps") and torch.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def end_of_song_cleanup() -> None:
    hard_free_gpu("end_of_song")


def reset_peak_stats() -> None:
    try:
        import torch  # pylint: disable=import-outside-toplevel

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def vram_snapshot() -> dict[str, int]:
    try:
        import torch  # pylint: disable=import-outside-toplevel

        if torch.cuda.is_available():
            return {
                "allocated": int(torch.cuda.memory_allocated()),
                "reserved": int(torch.cuda.memory_reserved()),
                "peak": int(torch.cuda.max_memory_allocated()),
            }
    except Exception:
        pass
    return {}


def log_vram(label: str) -> None:
    snapshot = vram_snapshot()
    if snapshot:
        print(f"[core:VRAM] {label}: {snapshot}", flush=True)
