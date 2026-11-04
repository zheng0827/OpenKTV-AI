from __future__ import annotations

from contextlib import contextmanager


def _build_unsafe_torch_load(original_load):
    def _patched_load(*args, **kwargs):
        kwargs["weights_only"] = False
        return original_load(*args, **kwargs)

    return _patched_load


@contextmanager
def unsafe_torch_load_context():
    """Temporarily force torch.load(weights_only=False) only within a narrow scope.

    PyTorch >=2.6 defaults to weights_only=True, which breaks some whisperx/pyannote
    checkpoints that include pickled config objects. We avoid process-wide monkey
    patches and only relax this behavior around known model-loading callsites.
    """

    try:
        import torch  # pylint: disable=import-outside-toplevel
    except Exception:
        yield
        return

    original_load = torch.load
    torch.load = _build_unsafe_torch_load(original_load)
    try:
        yield
    finally:
        torch.load = original_load


from .config import AppSettings, DevelopmentConfig, ProductionConfig, load_settings

__all__ = [
    "AppSettings",
    "DevelopmentConfig",
    "ProductionConfig",
    "load_settings",
    "unsafe_torch_load_context",
]
