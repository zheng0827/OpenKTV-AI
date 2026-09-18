from __future__ import annotations

import os
import site
import sys
from contextlib import contextmanager
from pathlib import Path


def _candidate_dll_directories() -> list[Path]:
    roots: list[Path] = []
    try:
        roots.extend(Path(path) for path in site.getsitepackages())
    except (AttributeError, OSError):
        pass
    try:
        roots.append(Path(site.getusersitepackages()))
    except (AttributeError, OSError):
        pass
    roots.append(Path(sys.prefix))

    directories: list[Path] = []
    seen: set[Path] = set()
    for base in roots:
        if not base.is_dir():
            continue
        for path in (base, base / "nvidia", base / "torch" / "lib", base / "ctranslate2"):
            if path.is_dir() and path not in seen:
                directories.append(path)
                seen.add(path)
        for path in base.rglob("*.dll"):
            parent = path.parent
            if parent not in seen:
                directories.append(parent)
                seen.add(parent)
    return directories


def inject_nvidia_dlls() -> dict[str, object]:
    """Register every installed CUDA DLL directory and report missing pieces.

    PyTorch CUDA 12 uses cuDNN 9 names while older CTranslate2 builds may
    still request cuDNN 8 names.  They are reported separately instead of
    pretending that a different major version is compatible.
    """
    directories = _candidate_dll_directories()
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    for directory in directories:
        value = str(directory)
        if value not in path_entries:
            path_entries.insert(0, value)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(value)
            except OSError:
                pass
    os.environ["PATH"] = os.pathsep.join(path_entries)

    files = {name.lower() for directory in directories for name in os.listdir(directory)}
    required = {
        "cublas": any(name.startswith("cublas64_") and name.endswith(".dll") for name in files),
        "cudart": any(name.startswith("cudart64_") and name.endswith(".dll") for name in files),
        "cudnn9": any(name.startswith("cudnn_ops") and name.endswith("_9.dll") for name in files),
        "ctranslate2_cudnn8": "cudnn_ops_infer64_8.dll" in files,
    }
    missing = [name for name, present in required.items() if not present]
    for name, present in required.items():
        print(f"[core:CUDA] {name}: {'OK' if present else 'MISSING'}", flush=True)
    if missing:
        print(
            "[core:CUDA] Missing DLL capabilities: "
            + ", ".join(missing)
            + ". CUDA 12/PyTorch may work, but older CTranslate2 VAD paths can fall back or fail.",
            flush=True,
        )
    return {"directories": directories, "required": required, "missing": missing}


if "--alignment-worker" not in sys.argv:
    CUDA_DLL_REPORT = inject_nvidia_dlls()


@contextmanager
def gpu_model(name: str):
    held: list[object] = []
    try:
        yield held
    finally:
        held.clear()
        hard_free_gpu(name)


def hard_free_gpu(_label: str | None = None) -> None:
    try:
        import gc

        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if hasattr(torch.cuda, "ipc_collect"):
                torch.cuda.ipc_collect()
        if hasattr(torch, "mps") and torch.mps.is_available():
            torch.mps.empty_cache()
    except (ImportError, RuntimeError):
        pass


def end_of_song_cleanup() -> None:
    hard_free_gpu("end_of_song")


def reset_peak_stats() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except (ImportError, RuntimeError):
        pass


def vram_snapshot() -> dict[str, int]:
    try:
        import torch

        if torch.cuda.is_available():
            return {
                "allocated": int(torch.cuda.memory_allocated()),
                "reserved": int(torch.cuda.memory_reserved()),
                "peak": int(torch.cuda.max_memory_allocated()),
            }
    except (ImportError, RuntimeError):
        pass
    return {}


def log_vram(label: str) -> None:
    snapshot = vram_snapshot()
    if snapshot:
        print(f"[core:VRAM] {label}: {snapshot}", flush=True)
