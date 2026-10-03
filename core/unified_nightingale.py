from __future__ import annotations

import os
import shutil
import subprocess
import uuid
import urllib.request
import urllib.parse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppSettings
from .demucs_separator import ensure_demucs_weights
from .downloader import download_youtube_video
from .lyrics_alignment import LyricLine, write_ktv_lrc, parse_lyrics_text, build_text_transform
from .runtime import compute_type_for, resolve_device, align_device_for
from .separators import separate_audio
from .transcription import transcribe_segments
from .audio import detect_vocal_region, highpass_filter, normalize_rms, suppress_reverb
from .language_detection import detect_language_multiwindow
from .whisper_alignment import align_with_backend
from .cjk import is_cjk, tokenize_for_alignment, align_lang_code, attribute_chars_to_tokens, merge_punct, attach_reading, qwen_kept_len, is_supported_lang, align_model_for, clean_for_alignment
from .uvr_separator import dereverb_vocals_with_uvr
from .metadata import extract_youtube_metadata, match_spotify_track, safe_filename, fetch_lyrics

@dataclass(frozen=True)
class PipelineArtifacts:
    original_instrumental: Path
    dialogue_instrumental: Path
    vocals: Path
    original_vocal_mp4: Path
    dialogue_vocals: Path
    lrc: Path
    detected_language: str
    alignment_results: dict[str, str]

def get_ffmpeg_bin(settings: AppSettings) -> str:
    if settings.ffmpeg_dir:
        p = Path(settings.ffmpeg_dir)
        candidate = p / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if candidate.is_file(): return str(candidate)
        if p.is_file(): return str(p)
    return os.environ.get("FFMPEG_PATH", "ffmpeg")

def enhanced_fetch_lrclib(song_name: str, singer: str) -> str:
    queries = []
    if singer:
        queries.append(urllib.parse.urlencode({"track_name": song_name, "artist_name": singer}))
        queries.append(urllib.parse.urlencode({"q": f"{singer} {song_name}"}))
    else:
        queries.append(urllib.parse.urlencode({"q": song_name}))

    for query in queries:
        url = f"https://lrclib.net/api/search?{query}"
        req = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data:
                    if item.get("plainLyrics"):
                        return item.get("plainLyrics")
        except Exception:
            continue
    return ""

def smart_join(words: list[str]) -> str:
    if not words: return ""
    res = [words[0]]
    for w in words[1:]:
        prev = res[-1]
        prev_is_en = bool(re.search(r'[A-Za-z0-9]', prev[-1:]))
        curr_is_en = bool(re.search(r'[A-Za-z0-9]', w[:1]))
        res.append(" " + w if (prev_is_en or curr_is_en) else w)
    return "".join(res)

def _normalize(word: str) -> str:
    return re.sub(r"[^\w]", "", word).lower()

def _collect_aligned(align_result: dict) -> list[dict]:
    out = []
    for seg in align_result.get("segments", []):
        items = seg.get("words") or seg.get("chars") or []
        for w in items:
            text = w.get("word", w.get("char", ""))
            if text is None or not str(text).strip(): continue
            out.append({
                "word": str(text), "norm": _normalize(str(text)),
                "start": w.get("start"), "end": w.get("end"), "score": w.get("score"),
            })
    return out

def _interpolate_missing(word_entries: list[dict]):
    unset = [i for i, e in enumerate(word_entries) if e.get("start") is None]
    set_entries = [e for e in word_entries if e.get("start") is not None]
    if not unset or not set_entries: return
    for ui in unset:
        prev_end = set_entries[0]["start"]
        next_start = set_entries[-1]["end"]
        for j in range(ui - 1, -1, -1):
            if word_entries[j].get("start") is not None:
                prev_end = word_entries[j]["end"]
                break
        for j in range(ui + 1, len(word_entries)):
            if word_entries[j].get("start") is not None:
                next_start = word_entries[j]["start"]
                break
        mid = (prev_end + next_start) / 2
        word_entries[ui]["start"] = round(prev_end, 3)
        word_entries[ui]["end"] = round(mid, 3)

# ==========================================
# 核心防漂移收束邏輯 (Anti-Drift Clamp)
# ==========================================
MAX_WORD_DURATION = 2.0

def _map_words_to_lines(align_result: dict, clean_lines: list[str]) -> list[LyricLine]:
    aligned = _collect_aligned(align_result)
    LOOKAHEAD = 6
    ai = 0
    segments = []
    for line_text in clean_lines:
        word_entries = []
        for word_text in line_text.split():
            target = _normalize(word_text)
            matched = -1
            if target:
                limit = min(ai + LOOKAHEAD, len(aligned))
                for k in range(ai, limit):
                    if aligned[k]["norm"] == target:
                        matched = k
                        break
            if matched >= 0:
                a = aligned[matched]
                ai = matched + 1
                entry = {"word": word_text, "start": a.get("start"), "end": a.get("end")}
            else:
                entry = {"word": word_text, "start": None, "end": None, "estimated": True}
            word_entries.append(entry)

        _interpolate_missing(word_entries)
        valid_words = [e for e in word_entries if e.get("start") is not None]
        if not valid_words: continue
        for w in valid_words:
            w["start"] = round(w["start"], 3)
            w["end"] = round(w["end"], 3)
            w["text"] = w["word"]
            if w["end"] - w["start"] > MAX_WORD_DURATION:
                w["end"] = round(w["start"] + MAX_WORD_DURATION, 3)
        segments.append(LyricLine(start=valid_words[0]["start"], end=valid_words[-1]["end"], text=line_text, words=valid_words))
    return segments

def _map_chars_to_lines_cjk(align_result: dict, clean_lines: list[str], line_token_pairs: list, language: str) -> list[LyricLine]:
    aligned = _collect_aligned(align_result)
    segments = []
    cursor = 0
    for original, token_pairs in zip(clean_lines, line_token_pairs):
        n = sum(len(r) for _, r in token_pairs)
        if n == 0: continue
        slice_chars = aligned[cursor:cursor + n]
        cursor += len(slice_chars)

        if len(slice_chars) < n:
            slice_chars += [{"word": "", "start": None, "end": None}] * (n - len(slice_chars))

        surfaces = [s for s, _ in token_pairs]
        lengths = [len(r) for _, r in token_pairs]
        fb_start = next((c["start"] for c in slice_chars if c.get("start") is not None), None)
        fb_end = next((c["end"] for c in reversed(slice_chars) if c.get("end") is not None), None)

        entries = attribute_chars_to_tokens(surfaces, slice_chars, fallback_start=fb_start, fallback_end=fb_end, cleaned_lengths=lengths)
        entries = merge_punct(entries)
        valid = [e for e in entries if e.get("start") is not None and e.get("end") is not None]
        if not valid: continue

        for e in valid:
            e["start"] = round(e["start"], 3)
            e["end"] = round(e["end"], 3)
            if e["end"] < e["start"]: e["end"] = e["start"]
            if e["end"] - e["start"] > MAX_WORD_DURATION:
                e["end"] = round(e["start"] + MAX_WORD_DURATION, 3)
            e["text"] = e["word"]

        attach_reading(valid, language)
        segments.append(LyricLine(start=valid[0]["start"], end=valid[-1]["end"], text=original, words=valid))
    return segments

def _map_qwen_units_to_lines(align_result: dict, clean_lines: list[str], language: str) -> list[LyricLine]:
    units = _collect_aligned(align_result)
    segments = []
    cursor = 0
    for line_text in clean_lines:
        need = qwen_kept_len(line_text)
        if need == 0: continue
        taken = []
        acc = 0
        while cursor < len(units) and acc < need:
            u = units[cursor]
            cursor += 1
            taken.append(u)
            acc += qwen_kept_len(u["word"])
        words = []
        for u in taken:
            if u.get("start") is None or u.get("end") is None: continue
            s = round(u["start"], 3)
            e = round(u["end"], 3)
            if e - s > MAX_WORD_DURATION:
                e = round(s + MAX_WORD_DURATION, 3)
            words.append({"word": u["word"], "text": u["word"], "start": s, "end": e})
        if not words: continue
        if is_supported_lang(language): attach_reading(words, language)
        segments.append(LyricLine(start=words[0]["start"], end=words[-1]["end"], text=line_text, words=words))
    return segments


def _align_lyric_lines(
    backend: str,
    clean_lines: list[str],
    full_audio,
    detected_language: str,
    transcription_device: str,
    vocal_start: float,
    vocal_end: float,
    text_transform,
) -> list[LyricLine]:
    is_cjk_lang = is_cjk(detected_language)
    aligned_lines = None
    if backend == "qwen":
        from .qwen_alignment import qwen_align_with_cpu_fallback, is_supported as qwen_supported
        if qwen_supported(detected_language):
            qwen_text = "\n".join(clean_lines)
            try:
                payload = qwen_align_with_cpu_fallback(
                    [{"text": qwen_text, "start": vocal_start, "end": vocal_end}],
                    full_audio,
                    detected_language,
                )
                aligned_lines = _map_qwen_units_to_lines(payload, clean_lines, detected_language)
            except Exception as error:
                print(f"[core:pipeline] Qwen 對齊失敗: {error}，改用 WhisperX 對齊", flush=True)

    if aligned_lines is None:
        line_token_pairs = []
        for line in clean_lines:
            if detected_language in {"zh", "yue"}:
                line_token_pairs.append([(char, clean_for_alignment(char)) for char in line])
            elif is_cjk_lang:
                line_token_pairs.append(tokenize_for_alignment(line, detected_language))
        if is_cjk_lang:
            alignment_text = "".join("".join(token for _, token in pairs) for pairs in line_token_pairs)
        else:
            alignment_text = " ".join(clean_lines)
        payload = align_with_backend(
            [{"text": alignment_text, "start": vocal_start, "end": vocal_end}],
            full_audio,
            align_lang_code(detected_language) if is_cjk_lang else detected_language,
            transcription_device,
            backend=backend if backend != "qwen" else "whisperx",
            model_name=align_model_for(detected_language) if is_cjk_lang else None,
        )
        if is_cjk_lang:
            aligned_lines = _map_chars_to_lines_cjk(payload, clean_lines, line_token_pairs, detected_language)
        else:
            aligned_lines = _map_words_to_lines(payload, clean_lines)

    for line in aligned_lines:
        line.text = text_transform(line.text)
        for word in line.words:
            word["text"] = text_transform(word.get("text", word.get("word", "")))
    for index in range(1, len(aligned_lines)):
        previous, current = aligned_lines[index - 1], aligned_lines[index]
        if current.start < previous.end:
            overlap_point = max(current.start, previous.start + 0.1)
            previous.end = round(overlap_point, 3)
            if previous.words and previous.words[-1]["end"] > previous.end:
                previous.words[-1]["end"] = previous.end
            if current.start < previous.end:
                current.start = previous.end
                if current.words and current.words[0]["start"] < current.start:
                    current.words[0]["start"] = current.start
    return _merge_short_segments(_split_long_segments(aligned_lines))

def _split_long_segments(segments: list[LyricLine]) -> list[LyricLine]:
    MAX_WORDS_PER_LINE = 10
    out = []
    for seg in segments:
        words = seg.words
        if len(words) <= MAX_WORDS_PER_LINE:
            out.append(seg)
            continue
        for i in range(0, len(words), MAX_WORDS_PER_LINE):
            chunk = words[i:i + MAX_WORDS_PER_LINE]
            text = smart_join([w.get("text", w.get("word", "")) for w in chunk])
            out.append(LyricLine(start=chunk[0]["start"], end=chunk[-1]["end"], text=text, words=chunk))
    return out

def _merge_short_segments(segments: list[LyricLine]) -> list[LyricLine]:
    MIN_WORDS = 3
    merged = True
    while merged:
        merged = False
        i = 0
        while i < len(segments):
            if len(segments[i].words) < MIN_WORDS and len(segments) > 1:
                neighbor = i + 1 if i == 0 else (i - 1 if i == len(segments) - 1 else (i - 1 if segments[i].start - segments[i - 1].end <= segments[i + 1].start - segments[i].end else i + 1))
                if neighbor < i:
                    new_words = segments[neighbor].words + segments[i].words
                    new_text = smart_join([w.get("text", w.get("word", "")) for w in new_words])
                    segments[neighbor] = LyricLine(start=new_words[0]["start"], end=new_words[-1]["end"], text=new_text, words=new_words)
                    segments.pop(i)
                else:
                    new_words = segments[i].words + segments[neighbor].words
                    new_text = smart_join([w.get("text", w.get("word", "")) for w in new_words])
                    segments[neighbor] = LyricLine(start=new_words[0]["start"], end=new_words[-1]["end"], text=new_text, words=new_words)
                    segments.pop(i)
                merged = True
            else:
                i += 1
    return segments

# ==========================================
# 音訊處理與防漏音對白提取
# ==========================================

def make_pseudo_spatial_m4a(input_wav: Path, output_m4a: Path, ffmpeg_bin: str) -> None:
    cmd = [ffmpeg_bin, "-y", "-i", str(input_wav), "-af", "extrastereo=m=1.35:c=0", "-c:a", "aac", "-b:a", "320k", str(output_m4a)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def make_original_vocal_mp4(input_mp4: Path, accompaniment_wav: Path, vocals_wav: Path, output_mp4: Path, ffmpeg_bin: str) -> None:
    cmd = [
        ffmpeg_bin, "-y", "-i", str(input_mp4), "-i", str(accompaniment_wav), "-i", str(vocals_wav),
        "-filter_complex", "[1:a]extrastereo=m=1.35:c=0[acc];[acc][2:a]amix=inputs=2:normalize=0:duration=first[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k", str(output_mp4)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def make_dialogue_instrumental_m4a(accompaniment_wav: Path, dialogue_wav: Path, output_m4a: Path, ffmpeg_bin: str, has_dialogue: bool) -> None:
    if not has_dialogue or not dialogue_wav.is_file():
        make_pseudo_spatial_m4a(accompaniment_wav, output_m4a, ffmpeg_bin)
        return
    cmd = [
        ffmpeg_bin, "-y", "-i", str(accompaniment_wav), "-i", str(dialogue_wav),
        "-filter_complex", "[0:a]extrastereo=m=1.35:c=0[acc];[acc][1:a]amix=inputs=2:normalize=0:duration=first[a]",
        "-map", "[a]", "-c:a", "aac", "-b:a", "320k", str(output_m4a)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_safe_vocal_regions(lines: list[LyricLine], pad_start=0.4, pad_end=0.8, merge_gap=2.5) -> list[dict]:
    if not lines: return []
    regions = [{"start": max(0.0, line.start - pad_start), "end": line.end + pad_end} for line in lines]
    merged = [regions[0]]
    for curr in regions[1:]:
        prev = merged[-1]
        if curr["start"] - prev["end"] <= merge_gap:
            prev["end"] = max(prev["end"], curr["end"])
        else:
            merged.append(curr)
    return merged

def erase_lyrics_from_vocals(vocals_wav: Path, safe_regions: list[dict], output_dialogue_wav: Path) -> None:
    import soundfile as sf
    data, sr = sf.read(str(vocals_wav))
    for reg in safe_regions:
        s = max(0, int(reg["start"] * sr))
        e = min(len(data), int(reg["end"] * sr))
        if e > s: data[s:e] = 0
    sf.write(str(output_dialogue_wav), data, sr)

# ==========================================
# 主流程：核心對齊與對白辨識
# ==========================================

def run_unified_nightingale(
    *, settings: AppSettings, vocals_wav: Path, accompaniment_wav: Path, temp_input_mp4: Path,
    output_orig_instrumental: Path, output_dialogue_instrumental: Path, output_vocals: Path, output_mp4: Path,
    output_dialogue_vocals: Path, output_lrc: Path, song_name: str, singer: str, lyrics_text: str,
    device_preference: str, alignment_backend: str,
    output_dereverbed_vocals: Path | None = None,
    output_alignment_paths: dict[str, Path] | None = None,
) -> PipelineArtifacts:
    device = resolve_device(device_preference)
    compute_type = settings.whisper_compute_type if settings.whisper_compute_type != "auto" else compute_type_for(device)
    ffmpeg_bin = get_ffmpeg_bin(settings)

    print("[core:pipeline] 製作原唱音軌 MP4 (空間化伴奏 + 完整無損人聲)...", flush=True)
    make_original_vocal_mp4(temp_input_mp4, accompaniment_wav, vocals_wav, output_mp4, ffmpeg_bin)
    print("[core:pipeline] 產生純伴奏 A' (.original.instrumental.m4a)...", flush=True)
    make_pseudo_spatial_m4a(accompaniment_wav, output_orig_instrumental, ffmpeg_bin)
    shutil.copyfile(str(vocals_wav), str(output_vocals))

    if not (lyrics_text and lyrics_text.strip()):
        print("[core:pipeline] 無歌詞，使用 Whisper 辨識歌詞...", flush=True)
        t_segs, det_lang = transcribe_segments(
            vocals_wav=vocals_wav, model_name=settings.whisper_model, device=device, compute_type=compute_type,
            language=(settings.whisper_language or "").strip() or None, alignment_backend=alignment_backend,
        )
        lyrics_text = "\n".join(s.get("text", "") for s in t_segs if s.get("text"))

    import whisperx
    transcription_device = align_device_for(device)
    dereverbed_path = output_dereverbed_vocals or vocals_wav.with_name(f"{vocals_wav.stem}.dereverbed.wav")
    dereverb_succeeded = False
    if settings.dereverb_enabled:
        try:
            model_output = dereverb_vocals_with_uvr(
                vocals_wav,
                dereverbed_path.parent / "dereverb_model_output",
                settings.uvr_model_dir,
                settings.dereverb_model,
                log_cb=print,
            )
            if model_output.resolve() != dereverbed_path.resolve():
                shutil.copyfile(model_output, dereverbed_path)
            dereverb_succeeded = True
        except Exception as error:
            print(f"[core:pipeline] UVR 去混響模型失敗，使用保守 DSP 備援: {error}", flush=True)
    if not dereverb_succeeded:
        import soundfile as sf
        source_audio = whisperx.load_audio(str(vocals_wav))
        if settings.dereverb_enabled:
            source_audio = suppress_reverb(highpass_filter(source_audio))
        sf.write(str(dereverbed_path), source_audio, 16000)
    full_audio = whisperx.load_audio(str(dereverbed_path))
    full_audio = normalize_rms(full_audio)
    vocal_start, vocal_end = detect_vocal_region(full_audio)

    detected_language = (settings.whisper_language or "").strip()
    if not detected_language:
        from .gpu import gpu_model
        with gpu_model(f"whisperx:tiny:{transcription_device}") as held:
            tiny_model = whisperx.load_model("tiny", device=transcription_device, compute_type=compute_type)
            held.append(tiny_model)
            detected_language = detect_language_multiwindow(tiny_model, full_audio)

    clean_lines = parse_lyrics_text(lyrics_text)
    text_transform, _ = build_text_transform(detected_language)
    backends = list(dict.fromkeys(settings.lyrics_alignment_backends or (alignment_backend,)))
    if alignment_backend not in backends:
        backends.insert(0, alignment_backend)
    requested_parallelism = max(1, min(len(backends), settings.alignment_parallelism))
    lines_by_backend: dict[str, list[LyricLine]] = {}
    alignment_timings: dict[str, dict] = {}

    def run_one(backend: str):
        started = time.monotonic()
        result = _align_lyric_lines(
            backend, clean_lines, full_audio, detected_language, transcription_device,
            vocal_start, vocal_end, text_transform,
        )
        return backend, result, round(time.monotonic() - started, 3)

    if requested_parallelism > 1 and len(backends) > 1:
        with ThreadPoolExecutor(max_workers=requested_parallelism) as executor:
            futures = {executor.submit(run_one, backend): backend for backend in backends}
            for future in as_completed(futures):
                backend = futures[future]
                try:
                    _, lines, elapsed = future.result()
                    lines_by_backend[backend] = lines
                    alignment_timings[backend] = {"status": "complete", "seconds": elapsed}
                except Exception as error:
                    alignment_timings[backend] = {"status": "failed", "error": str(error)}
                    print(f"[core:pipeline] {backend} 對齊失敗: {error}", flush=True)
    else:
        for backend in backends:
            try:
                _, lines, elapsed = run_one(backend)
                lines_by_backend[backend] = lines
                alignment_timings[backend] = {"status": "complete", "seconds": elapsed}
            except Exception as error:
                alignment_timings[backend] = {"status": "failed", "error": str(error)}
                print(f"[core:pipeline] {backend} 對齊失敗: {error}", flush=True)
    if not lines_by_backend:
        raise RuntimeError("所有歌詞對齊後端皆執行失敗")
    primary_backend = alignment_backend if alignment_backend in lines_by_backend else next(iter(lines_by_backend))
    aligned_lines = lines_by_backend[primary_backend]

    print("[core:pipeline] 產生安全發聲區間，提取純對白人聲 D (.dialogue.vocals.wav)...", flush=True)
    safe_vocal_regions = get_safe_vocal_regions(aligned_lines, pad_start=0.4, pad_end=0.8, merge_gap=2.5)
    erase_lyrics_from_vocals(vocals_wav, safe_vocal_regions, output_dialogue_vocals)

    print("[core:pipeline] 對純對白軌執行 ASR 語音辨識...", flush=True)
    dialogue_items = []
    try:
        diag_segs, _ = transcribe_segments(
            vocals_wav=output_dialogue_vocals, model_name="tiny", device=device, compute_type=compute_type,
            language=(settings.whisper_language or "").strip() or None, alignment_backend="whisperx",
        )
        for ds in diag_segs:
            txt = ds.get("text", "").strip()
            if txt: dialogue_items.append({"start": ds["start"], "end": ds["end"], "text": txt})
    except Exception as e:
        print(f"[core:pipeline] 對白辨識略過: {e}")

    has_dialogue = len(dialogue_items) > 0
    print(f"[core:pipeline] 產生含對白伴奏 A'' (.instrumental.m4a) [偵測到 {len(dialogue_items)} 段對白]...", flush=True)
    make_dialogue_instrumental_m4a(accompaniment_wav, output_dialogue_vocals, output_dialogue_instrumental, ffmpeg_bin, has_dialogue)

    write_ktv_lrc(output_lrc, song_name, singer, vocal_end, aligned_lines, dialogue_items)
    alignment_results = {primary_backend: output_lrc.name}
    for backend, backend_lines in lines_by_backend.items():
        if backend == primary_backend:
            continue
        target = (output_alignment_paths or {}).get(backend)
        if target is None:
            target = output_lrc.with_name(f"{output_lrc.stem}.lyrics_alignment_{backend}.lrc")
        write_ktv_lrc(target, song_name, singer, vocal_end, backend_lines, dialogue_items)
        alignment_results[backend] = target.name
    alignment_results["_metrics"] = json.dumps(alignment_timings, ensure_ascii=False)

    return PipelineArtifacts(
        original_instrumental=output_orig_instrumental, dialogue_instrumental=output_dialogue_instrumental,
        vocals=output_vocals, original_vocal_mp4=output_mp4, dialogue_vocals=output_dialogue_vocals,
        lrc=output_lrc, detected_language=detected_language, alignment_results=alignment_results,
    )

class KTVProcessor:
    def __init__(self, settings: AppSettings, log_cb):
        self.settings = settings
        self.log = log_cb

    def sanitize_filename(self, name: str) -> str:
        return "".join([char for char in name if char not in r'\\/:*?"<>|'])

    def _extract_song_artist(self, title: str) -> tuple[str, str]:
        if " - " in title:
            artist, song = title.split(" - ", 1)
            return song.strip(), artist.strip()
        return title.strip(), ""

    def process_song(self, url: str, manual_title: str, options: dict | None = None) -> bool:
        options = options or {}
        stems = 4 if str(options.get("stems", self.settings.separator_stems)) == "4" else 2
        device_pref = str(options.get("device", self.settings.device_preference)).lower()
        separator_backend = str(options.get("separator_backend", self.settings.separator_backend)).lower()
        alignment_backend = str(options.get("alignment_backend", self.settings.alignment_backend)).lower()
        lyrics_text = (options.get("lyrics_text") or "").strip()
        singer = (options.get("singer") or "").strip()

        job_temp_dir: Path | None = None
        try:
            youtube = extract_youtube_metadata(url)
            parsed_title = youtube.get("title") or manual_title
            song_name = manual_title or youtube.get("song") or ""
            inferred_artist = singer or youtube.get("artist") or ""
            self.log(
                f"YouTube 標題解析：{parsed_title} "
                f"(歌名={song_name or '未辨識'}, 歌手={inferred_artist or '未辨識'})"
            )
            spotify = match_spotify_track(song_name, inferred_artist, self.settings.spotify_match_threshold)
            if spotify["status"] != "matched":
                if spotify["status"] == "not_found":
                    self.log(
                        f"Spotify 搜尋無結果：song={song_name!r}, artist={inferred_artist!r}, "
                        f"queries={spotify.get('search_queries', [])!r}"
                    )
                raise RuntimeError(
                    f"Spotify 曲目比對狀態為 {spotify['status']} "
                    f"(confidence={spotify['confidence']:.2f})；"
                    f"{'Spotify 搜尋沒有找到候選曲目，請核對 YouTube 標題解析出的歌名／歌手或人工輸入正確資訊。' if spotify['status'] == 'not_found' else '請人工核對歌曲，'}"
                    f"搜尋詞={json.dumps(spotify.get('search_queries', []), ensure_ascii=False)}；"
                    f"未開始下載。候選={json.dumps(spotify.get('candidates', []), ensure_ascii=False)}"
                )
            spotify_track = spotify["track"]
            song_name = spotify_track["song_name"]
            singer = spotify_track["artist_name"]
            safe_title = safe_filename(f"{singer} - {song_name}")
            self.log(f"Spotify 正規曲目：{safe_title} ({spotify_track['id']})")

            job_id = uuid.uuid4().hex
            job_temp_dir = self.settings.temp_base_dir / job_id
            job_temp_dir.mkdir(parents=True, exist_ok=True)

            temp_input = job_temp_dir / "input.mp4"
            temp_output_mp4 = job_temp_dir / "output.mp4"
            temp_orig_instrumental = job_temp_dir / "original.instrumental.m4a"
            temp_dialogue_instrumental = job_temp_dir / "instrumental.m4a"
            temp_vocals = job_temp_dir / "vocals.wav"
            temp_dereverbed_vocals = job_temp_dir / "vocals.dereverbed.wav"
            temp_dialogue_vocals = job_temp_dir / "dialogue.vocals.wav"
            temp_lrc = job_temp_dir / "lyrics.lrc"
            alignment_backends = list(dict.fromkeys(self.settings.lyrics_alignment_backends or (alignment_backend,)))
            temp_alignment_paths = {
                backend: job_temp_dir / f"lyrics_alignment_{backend}.lrc"
                for backend in alignment_backends if backend != alignment_backend
            }

            self.log("步驟 1/8: 使用 Spotify 曲目資訊比對後下載影片...")
            download_youtube_video(url, temp_input, self.settings.ffmpeg_dir, self.settings.yt_dlp_path)

            if separator_backend in {"demucs", "hybrid"}:
                ensure_demucs_weights(self.settings.demucs_model, cache_dir=self.settings.demucs_cache_dir, log_cb=self.log)

            self.log(f"步驟 2/8: 分離人聲/伴奏 ({separator_backend})...")
            separated = separate_audio(
                input_path=temp_input, work_dir=job_temp_dir, settings=self.settings, stems=stems,
                device_preference=device_pref, backend=separator_backend, log_cb=self.log,
            )

            if not lyrics_text:
                self.log("步驟 3/8: 依 Spotify 曲目資訊取得歌詞...")
                lyrics = fetch_lyrics(
                    song_name,
                    singer,
                    spotify_track.get("album", ""),
                    spotify_track.get("duration_seconds", 0),
                    self.settings.musixmatch_api_key,
                )
                lyrics_text = lyrics.get("synced") or lyrics.get("plain") or ""
                lyrics_source = lyrics.get("source", "")
                if not lyrics_text:
                    lyrics_text = enhanced_fetch_lrclib(song_name, singer) or ""
                    if lyrics_text and not lyrics_source:
                        lyrics_source = "lrclib_search"
                if lyrics_text:
                    self.log(f"✅ 成功獲取歌詞！(字數: {len(lyrics_text)})")
                else:
                    self.log("⚠️ 無法獲取歌詞！將啟用 Whisper 盲聽辨識。")
            else:
                self.log("步驟 3/8: 使用手動提供歌詞...")
                lyrics_source = "manual"

            self.log(f"步驟 4-7/8: 執行音訊空間化、合成無損原唱影片、1:1 精準對齊歌詞與對白分離...")
            artifacts = run_unified_nightingale(
                settings=self.settings, vocals_wav=separated.vocals_path, accompaniment_wav=separated.accompaniment_path,
                temp_input_mp4=temp_input, output_orig_instrumental=temp_orig_instrumental,
                output_dialogue_instrumental=temp_dialogue_instrumental, output_vocals=temp_vocals,
                output_mp4=temp_output_mp4, output_dialogue_vocals=temp_dialogue_vocals, output_lrc=temp_lrc,
                song_name=song_name or safe_title, singer=singer or "", lyrics_text=lyrics_text,
                device_preference=device_pref, alignment_backend=alignment_backend,
                output_dereverbed_vocals=temp_dereverbed_vocals,
                output_alignment_paths=temp_alignment_paths,
            )

            self.log("步驟 8/8: 歸檔媒體與多模型歌詞對齊結果...")
            final = self.settings.songs_dir / f"{safe_title}.mp4"
            suffix = 1
            while final.exists():
                final = self.settings.songs_dir / f"{safe_title}_{suffix}.mp4"
                suffix += 1

            final_orig_instrumental = final.with_name(f"{final.stem}.original.instrumental.m4a")
            final_dialogue_instrumental = final.with_name(f"{final.stem}.instrumental.m4a")
            final_vocals = final.with_name(f"{final.stem}.vocals.wav")
            final_dereverbed_vocals = final.with_name(f"{final.stem}.vocals.dereverbed.wav")
            final_dialogue_vocals = final.with_name(f"{final.stem}.dialogue.vocals.wav")
            final_lrc = final.with_name(f"{final.stem}.lrc")

            shutil.move(str(temp_output_mp4), str(final))
            shutil.move(str(temp_orig_instrumental), str(final_orig_instrumental))
            shutil.move(str(temp_dialogue_instrumental), str(final_dialogue_instrumental))
            shutil.move(str(temp_vocals), str(final_vocals))
            shutil.move(str(temp_dereverbed_vocals), str(final_dereverbed_vocals))
            shutil.move(str(temp_dialogue_vocals), str(final_dialogue_vocals))
            shutil.move(str(temp_lrc), str(final_lrc))

            primary_alignment_backend = next(
                (backend for backend, filename in artifacts.alignment_results.items() if filename == temp_lrc.name),
                alignment_backend,
            )
            alignment_names = {primary_alignment_backend: final_lrc.name}
            for backend, temp_path in temp_alignment_paths.items():
                if temp_path.is_file():
                    final_alignment = final.with_name(f"{final.stem}.lyrics_alignment_{backend}.lrc")
                    shutil.move(str(temp_path), str(final_alignment))
                    alignment_names[backend] = final_alignment.name

            from datetime import datetime, timezone
            metadata_row = {
                "artist_name": singer,
                "song_name": song_name,
                "album": spotify_track.get("album", ""),
                "release_year": spotify_track.get("release_year", ""),
                "duration_seconds": spotify_track.get("duration_seconds", 0),
                "artist_gender": "",
                "genre": spotify_track.get("genre", ""),
                "source_url": youtube.get("url") or url,
                "original_source_title": youtube.get("raw_title", ""),
                "normalized_youtube_title": parsed_title,
                "spotify_track_id": spotify_track.get("id", ""),
                "spotify_track_url": spotify_track.get("url", ""),
                "separator_model": self.settings.demucs_model if separator_backend in {"demucs", "hybrid"} else self.settings.uvr_model,
                "separator_mode": separator_backend,
                "lyrics_alignment_model": primary_alignment_backend,
                "alignment_result_filenames": json.dumps(alignment_names, ensure_ascii=False),
                "alignment_metrics": artifacts.alignment_results.get("_metrics", ""),
                "video_filename": final.name,
                "accompaniment_filename": final_dialogue_instrumental.name,
                "lyrics_filename": final_lrc.name,
                "vocals_filename": final_vocals.name,
                "dereverbed_vocals_filename": final_dereverbed_vocals.name,
                "dialogue_vocals_filename": final_dialogue_vocals.name,
                "lyrics_source": lyrics_source,
                "metadata_match_status": spotify["status"],
                "metadata_match_confidence": spotify["confidence"],
                "processing_status": "complete",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            update_library_index(self.settings.songs_dir, self.settings.library_index_path, {final.name: metadata_row})
            self.log("✅ 處理完成：Spotify metadata、歌曲曲庫與對齊結果已更新。")
            return True
        except subprocess.CalledProcessError as error:
            self.log(f"❌ 執行失敗 (Code {error})")
            return False
        except Exception as error:
            self.log(f"❌ 錯誤: {error}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            if job_temp_dir and job_temp_dir.exists():
                shutil.rmtree(job_temp_dir, ignore_errors=True)