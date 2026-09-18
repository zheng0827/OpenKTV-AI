def _patch_torch_load_weights_only_default() -> None:
    """PyTorch >=2.6 defaults torch.load(weights_only=True), which breaks loading
    pyannote/whisperx checkpoints that pickle omegaconf config objects (e.g. the VAD
    model used by whisperx). Some callers (e.g. lightning_fabric) even pass
    weights_only=True explicitly, so we force it back to False for every torch.load
    call in this process. We trust these first-party model checkpoints.
    """
    try:
        import torch  # pylint: disable=import-outside-toplevel
    except Exception:
        return

    original_load = torch.load
    if getattr(original_load, "_ktv_weights_only_patched", False):
        return

    def _patched_load(*args, **kwargs):
        kwargs["weights_only"] = False
        return original_load(*args, **kwargs)

    _patched_load._ktv_weights_only_patched = True  # type: ignore[attr-defined]
    torch.load = _patched_load


_patch_torch_load_weights_only_default()

from .config import AppSettings, DevelopmentConfig, ProductionConfig, load_settings

__all__ = ["AppSettings", "DevelopmentConfig", "ProductionConfig", "load_settings"]
