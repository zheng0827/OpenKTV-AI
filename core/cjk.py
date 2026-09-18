"""CJK (Japanese / Chinese / Korean) tokenization, romanization, and
char-timing reattribution helpers.

The wav2vec2 alignment models nightingale uses for ja/zh
(``jonatasgrosman/wav2vec2-large-xlsr-53-{japanese,chinese-zh-cn}``) are
character-level CTC checkpoints whose vocabs hold only kana+kanji / hanzi —
no punctuation, no romaji. ``clean_for_alignment`` mirrors their
training-time ``CHARS_TO_IGNORE`` so the aligner only sees in-vocab chars.
Per-character timing from those aligners is mapped onto fugashi (ja) or
jieba (zh) tokens for display.

Cantonese ("yue") shares written Han characters with Mandarin, so it rides
the same char-level path as ``zh``: jieba tokenization for display and the
Chinese wav2vec2 CTC checkpoint for alignment (WhisperX ships no ``yue``
default). Only its reading differs — Jyutping instead of pinyin.

Korean ("ko") uses ``kresnik/wav2vec2-large-xlsr-korean`` and is *not* in
WhisperX's ``LANGUAGES_WITHOUT_SPACES`` list, so its alignment output is
already per-eojeol (whitespace chunks) and bypasses the char-level
retokenization path entirely; only :func:`reading` is involved for ko.

Reading systems used: pykakasi Hepburn (ja), pypinyin tone-mark pinyin
(zh), ToJyutping Jyutping (yue), hangul-romanize academic Revised
Romanization (ko). All heavy modules are imported lazily on first use so
non-CJK songs don't pay the fugashi/pykakasi/jieba/ToJyutping/
hangul-romanize startup cost.
"""

# Punctuation / symbols / whitespace not present in the wav2vec2 ja/zh
# vocabs. Concatenates the CHARS_TO_IGNORE lists from both model cards plus
# common kaomoji/lyric punctuation we've seen in LRClib payloads.
_NOISE_CHARS = (
    ",?¿.!¡;:\"%~`_+<>=…–—°´«»„“”'’/\\^"
    "。、，、；：！？「」『』【】〝〟〜〽‧～｛｝（）［］〈〉《》"
    "♪♫♬·・…‥─━‐‑‒–—―•※"
)
_NOISE_CHARS_SET = set(_NOISE_CHARS) | {" ", "\t", "\n", "\r", "\u3000"}

# Hiragana-output wav2vec2 CTC model (vocab is ~80 hiragana chars + specials).
# We feed it fugashi-derived hiragana readings, which sidesteps the dense
# kanji vocabulary of the default jonatasgrosman checkpoint and matches the
# acoustic prior of natural Japanese speech much better.
JA_ALIGN_MODEL = "vumichien/wav2vec2-large-xlsr-japanese-hiragana"

# Han-character CTC checkpoint used for zh; also reused for yue since Cantonese
# lyrics are written in the same Han characters and WhisperX has no yue default.
ZH_ALIGN_MODEL = "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn"

_fugashi_tagger = None
_pykakasi_instance = None
_jieba_inited = False
_korean_transliter = None


def is_cjk(lang) -> bool:
    """Languages that go through the char-level alignment + retokenize path."""
    return lang in ("ja", "zh", "yue")


def qwen_kept_len(text: str) -> int:
    """Count characters the Qwen forced aligner keeps when it tokenizes.

    Mirrors transformers' ``_is_kept_char`` for ``Qwen3ASRProcessor`` (letters,
    numbers, apostrophes, CJK; punctuation and whitespace dropped). Used to slice
    Qwen's flat token stream back onto lyric lines by cumulative kept-char count,
    which works uniformly whether a line tokenizes per-character (zh) or per-word
    (ja/ko/latin) since token surfaces concatenate to the line's kept content.
    """
    import unicodedata

    count = 0
    for ch in text:
        if ch == "'":
            count += 1
            continue
        category = unicodedata.category(ch)
        if category.startswith("L") or category.startswith("N"):
            count += 1
    return count


def is_korean(lang) -> bool:
    return lang == "ko"


def is_supported_lang(lang) -> bool:
    """Any language for which we attach a romanized reading per token."""
    return is_cjk(lang) or is_korean(lang)


def _has_hangul(text: str) -> bool:
    for ch in text:
        c = ord(ch)
        if 0xAC00 <= c <= 0xD7AF:
            return True
        if 0x1100 <= c <= 0x11FF:
            return True
        if 0x3130 <= c <= 0x318F:
            return True
    return False


def clean_for_alignment(text: str) -> str:
    """Drop chars outside the wav2vec2 ja/zh model vocabulary."""
    if not text:
        return ""
    return "".join(ch for ch in text if ch not in _NOISE_CHARS_SET)


def _get_fugashi():
    global _fugashi_tagger
    if _fugashi_tagger is None:
        import fugashi
        _fugashi_tagger = fugashi.Tagger()
    return _fugashi_tagger


def _get_pykakasi():
    global _pykakasi_instance
    if _pykakasi_instance is None:
        import pykakasi
        _pykakasi_instance = pykakasi.kakasi()
    return _pykakasi_instance


def _ensure_jieba():
    global _jieba_inited
    if not _jieba_inited:
        import jieba
        jieba.initialize()
        _jieba_inited = True


def _get_korean_romanizer():
    global _korean_transliter
    if _korean_transliter is None:
        from hangul_romanize import Transliter
        from hangul_romanize.rule import academic
        _korean_transliter = Transliter(academic)
    return _korean_transliter


def _get_tojyutping():
    import ToJyutping
    return ToJyutping


def tokenize_japanese(text: str) -> list[str]:
    tagger = _get_fugashi()
    out: list[str] = []
    for t in tagger(text):
        s = getattr(t, "surface", None) or str(t)
        if s:
            out.append(s)
    return out


def _katakana_to_hiragana(text: str) -> str:
    """Lossless katakana→hiragana conversion. Long-mark ー and non-kana chars
    pass through unchanged."""
    out_chars: list[str] = []
    for ch in text:
        c = ord(ch)
        if 0x30A1 <= c <= 0x30F6:
            out_chars.append(chr(c - 0x60))
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def _morpheme_kana(t) -> str:
    """Best-effort UniDic kana reading for a fugashi morpheme (or empty)."""
    feature = getattr(t, "feature", None)
    if feature is None:
        return ""
    for attr in ("kana", "pron", "kanaBase", "pronBase"):
        v = getattr(feature, attr, None)
        if v and v != "*":
            return v
    return ""


def tokenize_japanese_with_reading(text: str) -> list[tuple[str, str]]:
    """Return ``[(surface, hiragana_reading), ...]`` per fugashi morpheme.

    The hiragana reading is the chars to feed the slplab hiragana CTC model;
    concatenating it across all tokens yields the alignment-text for the
    whole input. Tokens with no kana representation (ASCII, numerals,
    symbols) get an empty reading and are subsequently treated as punct by
    :func:`attribute_chars_to_tokens` / :func:`merge_punct`.
    """
    if not text:
        return []
    tagger = _get_fugashi()
    out: list[tuple[str, str]] = []
    for t in tagger(text):
        surface = getattr(t, "surface", None) or str(t)
        if not surface:
            continue
        kana = _morpheme_kana(t)
        if not kana:
            out.append((surface, ""))
            continue
        hira = _katakana_to_hiragana(kana)
        # Keep hiragana + long-mark only; slplab vocab is hiragana-based and
        # anything else (kanji that slipped through, latin, digits) would be
        # a wildcard that destabilises CTC alignment.
        hira_only = "".join(
            ch for ch in hira
            if 0x3040 <= ord(ch) <= 0x309F or ch == "ー"
        )
        out.append((surface, hira_only))
    return out


def tokenize_chinese(text: str) -> list[str]:
    _ensure_jieba()
    import jieba
    return [t for t in jieba.lcut(text, cut_all=False) if t]


def tokenize_korean(text: str) -> list[str]:
    return text.split()


def tokenize(text: str, lang: str) -> list[str]:
    """Word/morpheme tokenization. Token concatenation equals ``text`` for
    ja/zh; for ko it returns whitespace-separated eojeol (concatenation
    equals ``text`` only after collapsing inter-word spaces)."""
    if not text:
        return []
    if lang == "ja":
        return tokenize_japanese(text)
    if lang in ("zh", "yue"):
        return tokenize_chinese(text)
    if lang == "ko":
        return tokenize_korean(text)
    return [text]


def tokenize_for_alignment(text: str, lang: str) -> list[tuple[str, str]]:
    """Per-token ``(display_surface, alignment_chars)`` pairs.

    Concatenating the second element of every pair yields the full string
    fed to the wav2vec2 aligner. The first element is what we want to show
    on screen and what :func:`reading` consumes for romanization.

    For ``ja`` the alignment chars are the hiragana reading of the morpheme
    (matches the slplab hiragana CTC vocab). For ``zh`` they are the token
    with ja/zh-vocab punctuation stripped (matches the kanji/hanzi CTC
    vocab). For other languages we fall back to a single (text, cleaned)
    pair so callers stay uniform.
    """
    if not text:
        return []
    if lang == "ja":
        return tokenize_japanese_with_reading(text)
    if lang in ("zh", "yue"):
        return [(t, clean_for_alignment(t)) for t in tokenize_chinese(text)]
    return [(text, clean_for_alignment(text))]


def to_alignment_text(text: str, lang: str) -> str:
    """Concatenate :func:`tokenize_for_alignment` outputs into a single
    string suitable for the wav2vec2 aligner. For ``ja`` this is the all-
    hiragana version of ``text``; for ``zh`` it strips out-of-vocab punct."""
    if lang == "ja":
        return "".join(r for _, r in tokenize_japanese_with_reading(text))
    return clean_for_alignment(text)


def align_model_for(lang: str):
    """Override wav2vec2 align model for languages where the WhisperX
    default is poorly suited. Returns ``None`` to mean 'use default'."""
    if lang == "ja":
        return JA_ALIGN_MODEL
    if lang == "yue":
        return ZH_ALIGN_MODEL
    return None


def align_lang_code(lang: str) -> str:
    """WhisperX ``language_code`` for the wav2vec2 aligner.

    Cantonese reuses the Chinese code so WhisperX applies its no-space
    (per-character) word grouping and Han-character alignment path — the same
    treatment ``zh`` gets. WhisperX only uses this code to pick the word-split
    behaviour and (when no ``model_name`` is given) the default model, so the
    app keeps tracking ``yue`` separately for jieba tokenization and Jyutping
    readings."""
    return "zh" if lang == "yue" else lang


def reading(text: str, lang: str):
    """Romanized reading: pykakasi Hepburn (ja), tone-mark pinyin (zh),
    Jyutping (yue), Revised Romanization (ko)."""
    if not text:
        return None
    if not clean_for_alignment(text):
        return None
    if lang == "yue":
        try:
            pairs = _get_tojyutping().get_jyutping_list(text)
            # Drop Jyutping tone digits — bare numbers read poorly as karaoke
            # syllables (e.g. "hoi2 fut3" -> "hoi fut").
            parts = ["".join(ch for ch in jp if not ch.isdigit()) for _, jp in pairs if jp]
            r = " ".join(p for p in parts if p).strip()
            return r or None
        except Exception:
            return None
    if lang == "ja":
        try:
            chunks = _get_pykakasi().convert(text)
            r = "".join(c.get("hepburn", "") for c in chunks).strip()
            return r or None
        except Exception:
            return None
    if lang == "zh":
        try:
            from pypinyin import pinyin, Style
            chunks = pinyin(text, style=Style.TONE, heteronym=False, errors="ignore")
            parts = [c[0] for c in chunks if c and c[0]]
            r = " ".join(parts).strip()
            return r or None
        except Exception:
            return None
    if lang == "ko":
        if not _has_hangul(text):
            return None
        try:
            r = _get_korean_romanizer().translit(text).strip()
            return r or None
        except Exception:
            return None
    return None


def attribute_chars_to_tokens(
    tokens: list[str],
    chars_with_ts: list[dict],
    fallback_start=None,
    fallback_end=None,
    cleaned_lengths: list[int] | None = None,
) -> list[dict]:
    """Map per-character timestamps onto tokens.

    ``chars_with_ts`` is the WhisperX char-level alignment output for the
    text obtained by ``clean_for_alignment("".join(tokens))`` — or, when
    ``cleaned_lengths`` is supplied, by some caller-provided transformation
    (e.g. fugashi's hiragana reading per morpheme) whose per-token char
    counts are passed explicitly. Each token's timing window is taken from
    the first/last char it contains; tokens with zero alignment-chars are
    emitted with ``_punct: True`` and no timestamps so the caller can fold
    them into a neighbour via :func:`merge_punct`.
    """
    if cleaned_lengths is None:
        cleaned_lengths = [len(clean_for_alignment(t)) for t in tokens]
    expected = sum(cleaned_lengths)
    actual = len(chars_with_ts)

    if expected != actual:
        ts_starts = [c.get("start") for c in chars_with_ts if c.get("start") is not None]
        ts_ends = [c.get("end") for c in chars_with_ts if c.get("end") is not None]
        seg_start = ts_starts[0] if ts_starts else fallback_start
        seg_end = ts_ends[-1] if ts_ends else fallback_end
        if seg_start is None:
            seg_start = 0.0
        if seg_end is None or seg_end <= seg_start:
            seg_end = seg_start + 0.1
        timed_tokens = max(1, sum(1 for length in cleaned_lengths if length > 0))
        step = (seg_end - seg_start) / timed_tokens
        out: list[dict] = []
        idx = 0
        for tok, length in zip(tokens, cleaned_lengths):
            if length == 0:
                out.append({"word": tok, "_punct": True})
                continue
            s = seg_start + idx * step
            e = seg_start + (idx + 1) * step
            out.append({"word": tok, "start": s, "end": e, "estimated": True})
            idx += 1
        return out

    out: list[dict] = []
    cursor = 0
    for tok, length in zip(tokens, cleaned_lengths):
        if length == 0:
            out.append({"word": tok, "_punct": True})
            continue
        slice_chars = chars_with_ts[cursor:cursor + length]
        cursor += length
        ts_starts = [c.get("start") for c in slice_chars if c.get("start") is not None]
        ts_ends = [c.get("end") for c in slice_chars if c.get("end") is not None]
        scores = [c.get("score") for c in slice_chars if c.get("score") is not None]

        entry: dict = {"word": tok}
        if ts_starts and ts_ends:
            entry["start"] = ts_starts[0]
            entry["end"] = ts_ends[-1]
            if scores:
                entry["score"] = sum(scores) / len(scores)
        else:
            entry["estimated"] = True
        out.append(entry)
    return out


def merge_punct(entries: list[dict]) -> list[dict]:
    """Fold punctuation-only tokens into the adjacent timed token's text.

    Glues trailing punctuation onto the previous word, leading punctuation
    onto the following word. The displayable on-screen ``word`` keeps the
    original punctuation; timing/reading stay with the timed token.
    """
    out: list[dict] = []
    pending_prefix: list[str] = []
    for e in entries:
        if e.get("_punct"):
            if out:
                out[-1]["word"] = out[-1]["word"] + e["word"]
            else:
                pending_prefix.append(e["word"])
            continue
        cleaned = {k: v for k, v in e.items() if k != "_punct"}
        if pending_prefix:
            cleaned["word"] = "".join(pending_prefix) + cleaned["word"]
            pending_prefix = []
        out.append(cleaned)
    if pending_prefix and out:
        out[-1]["word"] = out[-1]["word"] + "".join(pending_prefix)
    return out


def attach_reading(entries: list[dict], lang: str) -> None:
    """Attach a ``reading`` field to each entry that has displayable text."""
    for e in entries:
        if "word" not in e:
            continue
        r = reading(e["word"], lang)
        if r:
            e["reading"] = r
