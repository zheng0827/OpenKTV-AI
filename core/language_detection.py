from __future__ import annotations


def detect_language_multiwindow(model, audio, sample_rate=16000, window_secs=30) -> str:
    from whisperx.audio import log_mel_spectrogram  # pylint: disable=import-outside-toplevel

    window_samples = window_secs * sample_rate
    total_samples = len(audio)
    n_mels = model.model.feat_kwargs.get("feature_size") or 80
    offsets = [0]
    if total_samples > window_samples:
        offsets.append(total_samples // 2 - window_samples // 2)
    if total_samples > window_samples * 2:
        offsets.append(total_samples // 4)
        offsets.append(total_samples * 3 // 4 - window_samples)

    votes: list[tuple[str, float]] = []
    for offset in offsets:
        offset = max(0, min(offset, total_samples - window_samples))
        chunk = audio[offset:offset + window_samples]
        padding = max(0, window_samples - len(chunk))
        segment = log_mel_spectrogram(chunk, n_mels=n_mels, padding=padding)
        encoder_output = model.model.encode(segment)
        results = model.model.model.detect_language(encoder_output)
        lang_token, prob = results[0][0]
        language = lang_token[2:-2]
        print(
            f"[core:language] window @{offset / sample_rate:.0f}s: "
            f"lang={language} prob={float(prob):.2f}",
            flush=True,
        )
        votes.append((language, float(prob)))

    scores: dict[str, float] = {}
    for language, probability in votes:
        scores[language] = scores.get(language, 0.0) + probability
    detected = max(scores, key=lambda item: scores[item]) if scores else "zh"
    print(f"[core:language] scores={scores} -> {detected}", flush=True)
    return detected
