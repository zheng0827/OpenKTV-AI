from __future__ import annotations


def is_cuda_available() -> bool:
    try:
        import torch  # pylint: disable=import-outside-toplevel

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def detect_device() -> str:
    try:
        import torch  # pylint: disable=import-outside-toplevel

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def resolve_device(preference: str) -> str:
    preferred = (preference or "auto").strip().lower()
    if preferred == "mps":
        return "mps" if detect_device() == "mps" else "cpu"
    if preferred == "cpu":
        return "cpu"
    if preferred == "cuda":
        return "cuda" if is_cuda_available() else "cpu"
    return detect_device()


def align_device_for(device: str) -> str:
    return "cpu" if device == "mps" else device


def compute_type_for(device: str) -> str:
    if device != "cuda":
        return "float32"
    try:
        import torch  # pylint: disable=import-outside-toplevel

        major, _minor = torch.cuda.get_device_capability()
        return "float16" if major >= 7 else "int8"
    except Exception:
        return "int8"


def is_oom(error: Exception | str) -> bool:
    lower = str(error).lower()
    return "out of memory" in lower or "outofmemoryerror" in lower
