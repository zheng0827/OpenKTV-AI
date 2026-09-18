from __future__ import annotations

LANGUAGES_WITHOUT_SPACES = ["ja", "zh"]


def ctc_align(
    transcript,
    model,
    align_model_metadata: dict,
    audio,
    device: str,
    interpolate_method: str = "nearest",
    return_char_alignments: bool = False,
) -> dict:
    import nltk  # pylint: disable=import-outside-toplevel
    import numpy as np  # pylint: disable=import-outside-toplevel
    import pandas as pd  # pylint: disable=import-outside-toplevel
    import torch  # pylint: disable=import-outside-toplevel
    from nltk.data import load as nltk_load  # pylint: disable=import-outside-toplevel
    from whisperx.audio import SAMPLE_RATE  # pylint: disable=import-outside-toplevel
    from whisperx.utils import PUNKT_LANGUAGES, interpolate_nans  # pylint: disable=import-outside-toplevel

    if not torch.is_tensor(audio):
        if isinstance(audio, str):
            from whisperx.audio import load_audio

            audio = load_audio(audio)
        audio = torch.from_numpy(audio)
    if len(audio.shape) == 1:
        audio = audio.unsqueeze(0)

    max_duration = audio.shape[1] / SAMPLE_RATE
    model_dictionary = align_model_metadata["dictionary"]
    model_lang = align_model_metadata["language"]
    model_type = align_model_metadata["type"]

    blank_id = 0
    for char, code in model_dictionary.items():
        if char == "[pad]" or char == " ":
            blank_id = code

    segment_data: dict[int, dict] = {}
    for sdx, segment in enumerate(transcript):
        text = segment["text"]
        num_leading = len(text) - len(text.lstrip())
        num_trailing = len(text) - len(text.rstrip())
        clean_char, clean_cdx = [], []
        for cdx, char in enumerate(text):
            char_ = char.lower()
            if model_lang not in LANGUAGES_WITHOUT_SPACES:
                char_ = char_.replace(" ", "|")
            if cdx < num_leading or cdx > len(text) - num_trailing - 1:
                continue
            if model_dictionary.get(char_) == blank_id:
                continue
            if char_ in model_dictionary or char_ not in (" ", "|"):
                clean_char.append(char_)
                clean_cdx.append(cdx)

        clean_wdx = list(range(len(text.split(" ") if model_lang not in LANGUAGES_WITHOUT_SPACES else text)))
        punkt_lang = PUNKT_LANGUAGES.get(model_lang, "english")
        try:
            sentence_splitter = nltk_load(f"tokenizers/punkt_tab/{punkt_lang}.pickle")
        except LookupError:
            nltk.download("punkt_tab", quiet=True)
            sentence_splitter = nltk_load(f"tokenizers/punkt_tab/{punkt_lang}.pickle")
        segment_data[sdx] = {
            "clean_char": clean_char,
            "clean_cdx": clean_cdx,
            "clean_wdx": clean_wdx,
            "sentence_spans": list(sentence_splitter.span_tokenize(text)),
        }

    aligned_segments = []
    for sdx, segment in enumerate(transcript):
        t1 = segment["start"]
        t2 = segment["end"]
        text = segment["text"]
        avg_logprob = segment.get("avg_logprob")
        aligned_seg = {"start": t1, "end": t2, "text": text, "words": [], "chars": None}
        if avg_logprob is not None:
            aligned_seg["avg_logprob"] = avg_logprob
        if return_char_alignments:
            aligned_seg["chars"] = []

        if len(segment_data[sdx]["clean_char"]) == 0 or t1 >= max_duration:
            aligned_segments.append(aligned_seg)
            continue

        text_clean = "".join(segment_data[sdx]["clean_char"])
        waveform_segment = audio[:, int(t1 * SAMPLE_RATE):int(t2 * SAMPLE_RATE)]
        if waveform_segment.shape[-1] < 400:
            lengths = torch.as_tensor([waveform_segment.shape[-1]]).to(device)
            waveform_segment = torch.nn.functional.pad(waveform_segment, (0, 400 - waveform_segment.shape[-1]))
        else:
            lengths = None

        with torch.inference_mode():
            if model_type == "torchaudio":
                emissions, _ = model(waveform_segment.to(device), lengths=lengths)
            elif model_type == "huggingface":
                emissions = model(waveform_segment.to(device)).logits
            else:
                raise NotImplementedError(f"Align model of type {model_type} not supported.")
            emissions = torch.log_softmax(emissions, dim=-1)

        emission = emissions[0].detach().float().contiguous()
        has_wildcard = any(c not in model_dictionary for c in text_clean)
        if has_wildcard:
            non_blank_mask = torch.ones(emission.size(1), dtype=torch.bool, device=emission.device)
            non_blank_mask[blank_id] = False
            wildcard_col = emission[:, non_blank_mask].max(dim=1).values
            emission = torch.cat([emission, wildcard_col.unsqueeze(1)], dim=1)
            wildcard_id = emission.size(1) - 1
            tokens = [model_dictionary.get(c, wildcard_id) for c in text_clean]
        else:
            tokens = [model_dictionary[c] for c in text_clean]

        char_segments = _forced_align_segment(emission, tokens, blank_id)
        if char_segments is None or len(char_segments) != len(text_clean):
            aligned_segments.append(aligned_seg)
            continue

        ratio = (t2 - t1) * waveform_segment.size(0) / emission.size(0)
        char_segments_arr = []
        word_idx = 0
        for cdx, char in enumerate(text):
            start, end, score = None, None, None
            if cdx in segment_data[sdx]["clean_cdx"]:
                char_seg = char_segments[segment_data[sdx]["clean_cdx"].index(cdx)]
                start = round(char_seg["start"] * ratio + t1, 3)
                end = round(char_seg["end"] * ratio + t1, 3)
                score = round(char_seg["score"], 3)
            char_segments_arr.append({"char": char, "start": start, "end": end, "score": score, "word-idx": word_idx})
            if model_lang in LANGUAGES_WITHOUT_SPACES:
                word_idx += 1
            elif cdx == len(text) - 1 or text[cdx + 1] == " ":
                word_idx += 1
        char_segments_arr = pd.DataFrame(char_segments_arr)

        aligned_subsegments = []
        char_segments_arr["sentence-idx"] = None
        for sdx2, (sstart, send) in enumerate(segment_data[sdx]["sentence_spans"]):
            curr_chars = char_segments_arr.loc[(char_segments_arr.index >= sstart) & (char_segments_arr.index <= send)]
            char_segments_arr.loc[(char_segments_arr.index >= sstart) & (char_segments_arr.index <= send), "sentence-idx"] = sdx2
            sentence_text = text[sstart:send]
            sentence_start = curr_chars["start"].min()
            end_chars = curr_chars[curr_chars["char"] != " "]
            sentence_end = end_chars["end"].max()
            sentence_words = []
            for word_idx in curr_chars["word-idx"].unique():
                word_chars = curr_chars.loc[curr_chars["word-idx"] == word_idx]
                word_text = "".join(word_chars["char"].tolist()).strip()
                if not word_text:
                    continue
                word_chars = word_chars[word_chars["char"] != " "]
                word_start = word_chars["start"].min()
                word_end = word_chars["end"].max()
                word_score = round(word_chars["score"].mean(), 3)
                word_segment = {"word": word_text}
                if not np.isnan(word_start):
                    word_segment["start"] = word_start
                if not np.isnan(word_end):
                    word_segment["end"] = word_end
                if not np.isnan(word_score):
                    word_segment["score"] = word_score
                sentence_words.append(word_segment)
            aligned_subsegments.append({"text": sentence_text, "start": sentence_start, "end": sentence_end, "words": sentence_words})

        aligned_subsegments = pd.DataFrame(aligned_subsegments)
        aligned_subsegments["start"] = interpolate_nans(aligned_subsegments["start"], method=interpolate_method)
        aligned_subsegments["end"] = interpolate_nans(aligned_subsegments["end"], method=interpolate_method)
        if len(aligned_subsegments) > 0:
            aligned_seg["start"] = float(aligned_subsegments.iloc[0]["start"])
            aligned_seg["end"] = float(aligned_subsegments.iloc[-1]["end"])
            aligned_seg["words"] = [word for words in aligned_subsegments["words"] for word in words]
        if return_char_alignments:
            aligned_seg["chars"] = char_segments_arr.to_dict("records")
        aligned_segments.append(aligned_seg)

    word_segments = []
    for segment in aligned_segments:
        word_segments.extend(segment["words"])
    return {"segments": aligned_segments, "word_segments": word_segments}


def _forced_align_segment(emission, tokens: list[int], blank_id: int):
    import torch  # pylint: disable=import-outside-toplevel
    from torchaudio.functional import forced_align, merge_tokens  # pylint: disable=import-outside-toplevel

    try:
        targets = torch.tensor([tokens], device=emission.device, dtype=torch.int32)
        input_lengths = torch.tensor([emission.size(0)], device=emission.device, dtype=torch.int32)
        target_lengths = torch.tensor([len(tokens)], device=emission.device, dtype=torch.int32)
        aligned, scores = forced_align(emission.unsqueeze(0), targets, input_lengths=input_lengths, target_lengths=target_lengths, blank=blank_id)
        merged = merge_tokens(aligned[0], scores[0])
    except Exception:
        return None
    return [{"start": int(span.start), "end": int(span.end), "score": float(span.score)} for span in merged]
