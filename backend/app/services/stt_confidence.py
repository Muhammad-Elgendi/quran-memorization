"""STT decoder-confidence filter — Heard word keep / hallucination dump.

Word keep floor is Accuracy *T* (session/request threshold, else DEFAULT_THRESHOLD).
Ayah-constrained recovery (agglutination split + in-vocab revive) runs after this
filter via ``apply_ayah_recovery``. Does not mutate Quran text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz import fuzz

from ..config import settings
from .normalizer import tokenize


@dataclass
class TranscriptWord:
    text: str
    confidence: float
    kept: bool


@dataclass
class Transcription:
    text: str
    raw_text: str
    words: list[TranscriptWord] = field(default_factory=list)
    sequence_confidence: float = 0.0
    skipped_reason: str | None = None
    scored: bool = False
    pre_recovery_text: str | None = None
    recovery_revived: list[str] = field(default_factory=list)
    recovery_split: list[dict] = field(default_factory=list)


def empty_transcription(
    *,
    raw_text: str = "",
    skipped_reason: str = "empty",
    sequence_confidence: float = 0.0,
    scored: bool = False,
    words: list[TranscriptWord] | None = None,
) -> Transcription:
    return Transcription(
        text="",
        raw_text=raw_text,
        words=words or [],
        sequence_confidence=sequence_confidence,
        skipped_reason=skipped_reason,
        scored=scored,
    )


def transcription_from_plain_text(
    text: str,
    *,
    scored: bool = False,
) -> Transcription:
    raw = (text or "").strip()
    words = [
        TranscriptWord(text=token, confidence=1.0, kept=True)
        for token in raw.split()
        if token
    ]
    return Transcription(
        text=raw,
        raw_text=raw,
        words=words,
        sequence_confidence=1.0 if words else 0.0,
        skipped_reason=None if words else "empty",
        scored=scored,
    )


# SentencePiece / GPT-2 word-start markers. Per-token decode often strips these
# instead of emitting a space, which glues Heard into one run-on string.
_WORD_START_MARKERS = ("\u2581", "Ġ")  # ▁, Ġ


def _piece_with_word_breaks(piece: str) -> str:
    text = piece or ""
    for marker in _WORD_START_MARKERS:
        if text.startswith(marker):
            return " " + text[len(marker) :]
        text = text.replace(marker, " ")
    return text


def calibrate_decoder_prob(p: float, *, gamma: float | None = None) -> float:
    """Map decoder softmax onto the Accuracy-slider scale.

    Lab (2026-08-14): raw *P* ≥ 0.85 emptied REST Heard and dropped الرحمن
    from 1:3 while الرحيم survived. Power map keeps typical speech and still
    drops the screenshot hallucination band (~0.12–0.22).
    """
    prob = min(1.0, max(0.0, float(p)))
    g = settings.STT_DECODER_PROB_GAMMA if gamma is None else float(gamma)
    if prob <= 0.0:
        return 0.0
    if g <= 0.0 or g == 1.0:
        return prob
    return float(prob**g)


def _agg_probs(probs: list[float], agg: str) -> float:
    if not probs:
        return 0.0
    if agg == "mean":
        return sum(probs) / len(probs)
    return min(probs)


def words_from_token_pieces(
    pieces: Sequence[tuple[str, float]],
    *,
    agg: str = "min",
) -> list[tuple[str, float]]:
    """Group decoded token pieces into words; confidence = min (or mean) subword prob."""
    words: list[tuple[str, float]] = []
    buf = ""
    probs: list[float] = []

    def flush() -> None:
        nonlocal buf, probs
        token = buf.strip()
        if token:
            words.append((token, _agg_probs(probs, agg) if probs else 0.0))
        buf = ""
        probs = []

    for piece, prob in pieces:
        if not piece:
            continue
        normalized = _piece_with_word_breaks(piece)
        i = 0
        while i < len(normalized):
            if normalized[i].isspace():
                if buf:
                    flush()
                i += 1
                continue
            j = i
            while j < len(normalized) and not normalized[j].isspace():
                j += 1
            buf += normalized[i:j]
            probs.append(float(prob))
            i = j
    if buf:
        flush()
    return words


def align_token_probs_to_decoded_words(
    decode_prefix,
    token_ids: Sequence[int],
    token_probs: Sequence[float],
    *,
    surface_text: str,
    agg: str = "min",
) -> list[tuple[str, float]]:
    """Map token probs onto whitespace words of ``surface_text`` (full decode)."""
    words = [part for part in (surface_text or "").split() if part]
    if not words:
        return []
    buckets: list[list[float]] = [[] for _ in words]
    acc: list[int] = []
    for tid, prob in zip(token_ids, token_probs):
        acc.append(int(tid))
        n = len(str(decode_prefix(acc) or "").split())
        if n <= 0:
            continue
        buckets[min(n - 1, len(words) - 1)].append(float(prob))
    return [
        (word, _agg_probs(probs, agg) if probs else 1.0)
        for word, probs in zip(words, buckets)
    ]


def filter_transcription(
    raw_text: str,
    word_confidences: Sequence[float] | None = None,
    *,
    threshold: float | None = None,
    seq_min: float | None = None,
    sequence_confidence: float | None = None,
    enabled: bool | None = None,
    scored: bool = True,
) -> Transcription:
    """Apply sequence + per-word keep rules. ``threshold`` is Accuracy *T*."""
    raw = (raw_text or "").strip()
    tokens = [part for part in raw.split() if part]
    confidences: list[float]
    if word_confidences is None:
        confidences = [1.0] * len(tokens)
    else:
        confidences = [float(c) for c in word_confidences[: len(tokens)]]
        if len(confidences) < len(tokens):
            confidences.extend([1.0] * (len(tokens) - len(confidences)))

    keep_floor = (
        settings.DEFAULT_THRESHOLD if threshold is None else float(threshold)
    )
    seq_floor = (
        settings.STT_SEQUENCE_CONFIDENCE_MIN if seq_min is None else float(seq_min)
    )
    filter_on = (
        settings.STT_CONFIDENCE_FILTER if enabled is None else bool(enabled)
    )

    if sequence_confidence is None:
        seq = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
    else:
        seq = float(sequence_confidence)

    if not filter_on:
        words = [
            TranscriptWord(text=token, confidence=conf, kept=True)
            for token, conf in zip(tokens, confidences)
        ]
        return Transcription(
            text=raw,
            raw_text=raw,
            words=words,
            sequence_confidence=seq,
            skipped_reason=None if tokens else "empty",
            scored=scored and word_confidences is not None,
        )

    if not tokens:
        return empty_transcription(
            raw_text=raw,
            skipped_reason="empty",
            sequence_confidence=seq,
            scored=scored,
        )

    if seq < seq_floor:
        words = [
            TranscriptWord(text=token, confidence=conf, kept=False)
            for token, conf in zip(tokens, confidences)
        ]
        return empty_transcription(
            raw_text=raw,
            skipped_reason="low_sequence",
            sequence_confidence=seq,
            scored=scored,
            words=words,
        )

    words = [
        TranscriptWord(text=token, confidence=conf, kept=conf >= keep_floor)
        for token, conf in zip(tokens, confidences)
    ]
    kept_text = " ".join(w.text for w in words if w.kept)
    if not kept_text:
        return Transcription(
            text="",
            raw_text=raw,
            words=words,
            sequence_confidence=seq,
            skipped_reason="empty",
            scored=scored,
        )
    return Transcription(
        text=kept_text,
        raw_text=raw,
        words=words,
        sequence_confidence=seq,
        skipped_reason=None,
        scored=scored,
    )


def trim_overgenerated_partial(
    expected: str,
    filtered_text: str,
    *,
    ratio: float | None = None,
    word_match_threshold: float | None = None,
) -> str:
    """Drop confident extras from live Heard when the decode is far too long."""
    text = (filtered_text or "").strip()
    if not text:
        return ""
    exp_tokens = tokenize(expected)
    rec_tokens = tokenize(text)
    exp_n = len(exp_tokens)
    rec_n = len(rec_tokens)
    over_ratio = (
        settings.STT_PARTIAL_MAX_OVERGEN_RATIO if ratio is None else float(ratio)
    )
    match_floor = (
        settings.WORD_MATCH_THRESHOLD
        if word_match_threshold is None
        else float(word_match_threshold)
    )
    limit = max(exp_n * over_ratio, exp_n + 2)
    if rec_n <= limit:
        return text
    kept: list[str] = []
    for word in text.split():
        norm_tokens = tokenize(word)
        if not norm_tokens:
            continue
        norm = norm_tokens[0]
        if any(
            fuzz.ratio(norm, exp) / 100.0 >= match_floor for exp in exp_tokens
        ):
            kept.append(word)
    return " ".join(kept)


def stt_words_payload(transcription: Transcription) -> list[dict]:
    return [
        {
            "text": word.text,
            "confidence": round(word.confidence, 4),
            "kept": word.kept,
        }
        for word in transcription.words
    ]


def recovery_debug_fields(transcription: Transcription) -> dict:
    """Lab/debug fields for partials and REST. Omit when recovery did nothing."""
    fields: dict = {}
    if transcription.pre_recovery_text is not None:
        fields["raw_recognized"] = transcription.pre_recovery_text
    if transcription.recovery_revived or transcription.recovery_split:
        fields["recovery"] = {
            "revived": list(transcription.recovery_revived),
            "split": list(transcription.recovery_split),
        }
    return fields


def _fuzzy_ok(left: str, right: str, match_floor: float) -> bool:
    if left == right:
        return True
    return fuzz.ratio(left, right) / 100.0 >= match_floor


def _copy_word(word: TranscriptWord, *, kept: bool | None = None) -> TranscriptWord:
    return TranscriptWord(
        text=word.text,
        confidence=word.confidence,
        kept=word.kept if kept is None else kept,
    )


def _try_split(token_norm: str, expected: list[str], start_i: int) -> list[str] | None:
    """Shortest cover of two or more consecutive expected tokens."""
    if start_i >= len(expected) or not token_norm:
        return None
    for end in range(start_i + 2, len(expected) + 1):
        joined = "".join(expected[start_i:end])
        if joined == token_norm:
            return expected[start_i:end]
        if not token_norm.startswith(joined):
            break
    return None


def _try_split_from(
    token_norm: str, expected: list[str], start_i: int
) -> tuple[int, list[str]] | None:
    for idx in range(start_i, len(expected) - 1):
        parts = _try_split(token_norm, expected, idx)
        if parts:
            return idx, parts
    return None


def _advance_cursor(
    token: str, expected: list[str], cursor: int, match_floor: float
) -> int:
    for idx in range(cursor, len(expected)):
        if _fuzzy_ok(token, expected[idx], match_floor):
            return idx + 1
    return cursor


def _longest_prefix_coverage(
    expected: list[str],
    kept_norm: list[str],
    match_floor: float,
) -> int:
    i = 0
    j = 0
    while i < len(expected) and j < len(kept_norm):
        if _fuzzy_ok(expected[i], kept_norm[j], match_floor):
            i += 1
            j += 1
        else:
            break
    return i


def _kept_norm_seq(words: Sequence[TranscriptWord]) -> list[str]:
    tokens: list[str] = []
    for word in words:
        if not word.kept:
            continue
        tokens.extend(tokenize(word.text))
    return tokens


def _split_agglutinated(
    words: list[TranscriptWord],
    expected: list[str],
    match_floor: float,
) -> tuple[list[TranscriptWord], list[dict]]:
    out: list[TranscriptWord] = []
    splits: list[dict] = []
    cursor = 0
    for word in words:
        if not word.kept:
            out.append(_copy_word(word))
            continue
        tokens = tokenize(word.text)
        if len(tokens) != 1:
            out.append(_copy_word(word))
            for token in tokens:
                cursor = _advance_cursor(token, expected, cursor, match_floor)
            continue
        token = tokens[0]
        if any(_fuzzy_ok(token, item, match_floor) for item in expected):
            out.append(_copy_word(word))
            cursor = _advance_cursor(token, expected, cursor, match_floor)
            continue
        found = _try_split_from(token, expected, cursor)
        if found is None:
            out.append(_copy_word(word))
            continue
        start, parts = found
        splits.append({"from": word.text, "into": list(parts)})
        for part in parts:
            out.append(
                TranscriptWord(
                    text=part,
                    confidence=word.confidence,
                    kept=True,
                )
            )
        cursor = start + len(parts)
    return out, splits


def _revive_dropped(
    words: list[TranscriptWord],
    expected: list[str],
    match_floor: float,
    conf_floor: float,
) -> tuple[list[TranscriptWord], list[str]]:
    next_i = _longest_prefix_coverage(
        expected, _kept_norm_seq(words), match_floor
    )
    revived: list[str] = []
    out: list[TranscriptWord] = []
    for word in words:
        if word.kept:
            out.append(_copy_word(word))
            continue
        if word.confidence < conf_floor:
            out.append(_copy_word(word))
            continue
        tokens = tokenize(word.text)
        if len(tokens) != 1:
            out.append(_copy_word(word))
            continue
        matched = False
        for idx in range(next_i, len(expected)):
            if _fuzzy_ok(tokens[0], expected[idx], match_floor):
                out.append(_copy_word(word, kept=True))
                revived.append(word.text)
                next_i = idx + 1
                matched = True
                break
        if not matched:
            out.append(_copy_word(word))
    return out, revived


def recover_against_ayah(
    expected_uthmani: str,
    transcription: Transcription,
    *,
    invocab_floor: float | None = None,
    word_match_threshold: float | None = None,
) -> Transcription:
    """Split agglutinated STT tokens and revive in-vocab drops against the ayah.

    Does not invent tokens that never appeared in the decode except by splitting
    an emitted surface. Does not mutate stored Quran text.
    """
    if transcription.skipped_reason == "low_sequence":
        return transcription
    expected = tokenize(expected_uthmani)
    if not expected:
        return transcription

    match_floor = (
        settings.WORD_MATCH_THRESHOLD
        if word_match_threshold is None
        else float(word_match_threshold)
    )
    floor = (
        settings.STT_INVOCAB_FLOOR if invocab_floor is None else float(invocab_floor)
    )

    words = [_copy_word(word) for word in transcription.words]
    if not words:
        surface = (transcription.text or "").split()
        words = [
            TranscriptWord(text=token, confidence=1.0, kept=True)
            for token in surface
            if token
        ]
        if not words:
            return transcription

    words, splits = _split_agglutinated(words, expected, match_floor)
    words, revived = _revive_dropped(words, expected, match_floor, floor)
    if revived:
        extra_words, extra_splits = _split_agglutinated(words, expected, match_floor)
        words = extra_words
        splits = splits + extra_splits

    kept_text = " ".join(word.text for word in words if word.kept)
    if not revived and not splits and kept_text == (transcription.text or ""):
        return transcription

    skipped = transcription.skipped_reason
    if kept_text:
        skipped = None
    elif skipped is None:
        skipped = "empty"

    return Transcription(
        text=kept_text,
        raw_text=transcription.raw_text,
        words=words,
        sequence_confidence=transcription.sequence_confidence,
        skipped_reason=skipped,
        scored=transcription.scored,
        pre_recovery_text=transcription.text,
        recovery_revived=revived,
        recovery_split=splits,
    )


def apply_ayah_recovery(
    expected_uthmani: str,
    transcription: Transcription,
) -> Transcription:
    """Flag-gated entry used by REST and the stream session (one implementation)."""
    if not settings.STT_AYAH_LEXICON_RECOVERY:
        return transcription
    if not (expected_uthmani or "").strip():
        return transcription
    return recover_against_ayah(expected_uthmani, transcription)
