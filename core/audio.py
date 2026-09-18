"""Audio analysis utilities: vocal region detection, silence splitting, normalization."""

import numpy as np

DEFAULT_VOCAL_THRESHOLD_PCT = 0.15
_vocal_threshold_pct = DEFAULT_VOCAL_THRESHOLD_PCT


def set_vocal_threshold_pct(pct) -> None:
    """Set the RMS threshold (fraction of peak) used to detect the vocal region.

    Lower values keep more quiet audio at the song's edges (less aggressive
    trimming); higher values trim harder. Invalid/None values are ignored so the
    default stays in effect.
    """
    global _vocal_threshold_pct
    if pct is None:
        return
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return
    _vocal_threshold_pct = min(max(pct, 0.0), 1.0)


def get_vocal_threshold_pct() -> float:
    return _vocal_threshold_pct


def detect_vocal_region(audio, sr: int = 16000, win_secs: float = 0.5,
                        threshold_pct=None, min_consecutive: int = 4,
                        padding: float = 1.0) -> tuple[float, float]:
    """Detect where vocals start and end using RMS energy analysis.

    Returns (vocal_start, vocal_end) in seconds. When ``threshold_pct`` is
    ``None`` the module-level configurable threshold is used.
    """
    if threshold_pct is None:
        threshold_pct = _vocal_threshold_pct
    window_samples = int(win_secs * sr)
    rms_values = []
    for start_idx in range(0, len(audio), window_samples):
        chunk = audio[start_idx : start_idx + window_samples]
        rms_values.append(float(np.sqrt(np.mean(chunk ** 2))))

    duration_secs = len(audio) / sr

    if not rms_values:
        return 0.0, duration_secs

    peak_rms = max(rms_values)
    threshold = peak_rms * threshold_pct
    active = [rms >= threshold for rms in rms_values]
    active_count = sum(active)
    print(f"[nightingale:LOG] Vocal detection: peak_rms={peak_rms:.5f}, threshold({threshold_pct*100:.0f}%)={threshold:.5f}, windows={len(rms_values)}, active={active_count}", flush=True)

    top_windows = sorted(enumerate(rms_values), key=lambda x: x[1], reverse=True)[:10]
    for rank, (idx, rms) in enumerate(top_windows):
        t = idx * win_secs
        print(f"[nightingale:LOG]   RMS top {rank+1}: t={t:.1f}s rms={rms:.5f} {'<<ACTIVE>>' if active[idx] else ''}", flush=True)

    vocal_start_win = 0
    for i in range(len(active) - min_consecutive + 1):
        if all(active[i : i + min_consecutive]):
            vocal_start_win = i
            break

    vocal_end_win = len(rms_values) - 1
    for i in range(len(active) - 1, min_consecutive - 2, -1):
        start_check = max(i - min_consecutive + 1, 0)
        if all(active[start_check : start_check + min_consecutive]):
            vocal_end_win = i
            break

    vocal_start = vocal_start_win * win_secs
    vocal_end = min((vocal_end_win + 1) * win_secs, duration_secs)
    print(f"[nightingale:LOG] Sustained activity (>={min_consecutive} consecutive): first at win={vocal_start_win} ({vocal_start:.1f}s), last at win={vocal_end_win} ({vocal_end:.1f}s)", flush=True)

    vocal_start = max(vocal_start - padding, 0.0)
    vocal_end = min(vocal_end + padding, duration_secs)
    print(f"[nightingale:LOG] Vocal region (with {padding}s padding): {vocal_start:.1f}s - {vocal_end:.1f}s (song duration: {duration_secs:.1f}s)", flush=True)

    return vocal_start, vocal_end

def highpass_filter(audio, sr: int = 16000, cutoff_hz: float = 80.0):
    """Apply a simple highpass filter to remove sub-bass rumble from stems."""
    from scipy.signal import butter, sosfilt
    sos = butter(5, cutoff_hz, btype="high", fs=sr, output="sos")
    filtered = sosfilt(sos, audio.astype(np.float64)).astype(audio.dtype)
    print(f"[nightingale:LOG] Applied highpass filter at {cutoff_hz}Hz", flush=True)
    return filtered


def suppress_reverb(
    audio,
    sr: int = 16000,
    frame_length: int = 1024,
    hop_length: int = 256,
    reduction: float = 0.7,
    floor: float = 0.2,
):
    """Suppress stationary reverb/bleed while preserving vocal transients.

    This is a conservative spectral-subtraction pass. The lower spectral
    percentile across frames is used as an ambience estimate, then only that
    estimated component is attenuated. It is intended for lyric alignment,
    not for producing a mix-ready vocal stem.
    """
    from scipy.signal import istft, stft

    samples = np.asarray(audio)
    if samples.size == 0:
        return samples
    if samples.ndim != 1:
        raise ValueError("suppress_reverb expects a mono audio array")

    _, _, spectrum = stft(
        samples.astype(np.float64),
        fs=sr,
        nperseg=frame_length,
        noverlap=frame_length - hop_length,
        boundary="zeros",
        padded=True,
    )
    power = np.abs(spectrum) ** 2
    ambience = np.percentile(power, 20, axis=1, keepdims=True)
    excess = np.maximum(power - ambience, 0.0)
    gain = np.maximum(
        floor,
        1.0 - reduction * ambience / np.maximum(power, np.finfo(float).eps),
    )
    cleaned_spectrum = spectrum * gain * np.sqrt(
        excess / np.maximum(power, np.finfo(float).eps)
    )
    _, cleaned = istft(
        cleaned_spectrum,
        fs=sr,
        nperseg=frame_length,
        noverlap=frame_length - hop_length,
        boundary=True,
    )
    cleaned = cleaned[: samples.size]
    peak = float(np.max(np.abs(cleaned), initial=0.0))
    original_peak = float(np.max(np.abs(samples), initial=0.0))
    if peak > 0 and original_peak > 0:
        cleaned *= min(1.0, original_peak / peak)
    print(
        f"[nightingale:LOG] Applied conservative reverb suppression "
        f"(reduction={reduction:.2f}, floor={floor:.2f})",
        flush=True,
    )
    return cleaned.astype(samples.dtype, copy=False)


def normalize_rms(audio, target_rms: float = 0.1, max_gain: float = 10.0):
    """Boost audio volume to a target RMS level. Returns normalized audio."""
    raw_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if raw_rms > 0:
        gain = min(target_rms / raw_rms, max_gain)
        audio = (audio * gain).astype(audio.dtype)
        print(f"[nightingale:LOG] Normalized vocals: rms {raw_rms:.5f} -> {target_rms} (gain {gain:.2f}x)", flush=True)
    return audio
