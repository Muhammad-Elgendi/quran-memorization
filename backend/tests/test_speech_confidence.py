"""STT confidence filter — Heard word keep (stt-confidence-filter-spec §10.1)."""

from __future__ import annotations

import pytest

from app.services.stt_confidence import (
    align_token_probs_to_decoded_words,
    calibrate_decoder_prob,
    filter_transcription,
    trim_overgenerated_partial,
    words_from_token_pieces,
)


def test_c1_low_sequence_dumps_whole_decode():
    result = filter_transcription(
        "يا كلب نستعين",
        [0.21, 0.18, 0.92],
        threshold=0.85,
    )
    assert result.sequence_confidence == pytest.approx((0.21 + 0.18 + 0.92) / 3)
    assert result.sequence_confidence < 0.50
    assert result.text == ""
    assert result.skipped_reason == "low_sequence"
    assert all(not word.kept for word in result.words)
    assert "كلب" not in result.text


def test_c2_word_keep_at_accuracy_threshold():
    result = filter_transcription(
        "يا كلب نستعين",
        [0.21, 0.18, 0.92],
        threshold=0.85,
        sequence_confidence=0.70,
    )
    assert result.text == "نستعين"
    assert "يا" not in result.text
    assert "كلب" not in result.text
    kept = {word.text: word.kept for word in result.words}
    assert kept["يا"] is False
    assert kept["كلب"] is False
    assert kept["نستعين"] is True


def test_c3_high_confidence_identity():
    raw = "اياك نعبد واياك نستعين"
    result = filter_transcription(
        raw,
        [0.91, 0.88, 0.90, 0.93],
        threshold=0.85,
        sequence_confidence=0.90,
    )
    assert result.text == raw
    assert all(word.kept for word in result.words)
    assert result.skipped_reason is None


def test_c4_empty_raw():
    result = filter_transcription("", [], threshold=0.85)
    assert result.text == ""
    assert result.skipped_reason in {"empty", "low_sequence"}


def test_c5_filter_disabled_keeps_garbage():
    raw = "يا كلب نستعين"
    result = filter_transcription(
        raw,
        [0.21, 0.18, 0.92],
        threshold=0.85,
        enabled=False,
    )
    assert result.text == raw
    assert "كلب" in result.text


def test_c6_word_keep_boundary():
    dropped = filter_transcription(
        "نستعين",
        [0.849],
        threshold=0.85,
        sequence_confidence=0.90,
    )
    assert dropped.text == ""
    assert dropped.words[0].kept is False

    kept = filter_transcription(
        "نستعين",
        [0.85],
        threshold=0.85,
        sequence_confidence=0.90,
    )
    assert kept.text == "نستعين"
    assert kept.words[0].kept is True


def test_c7_floor_tracks_accuracy_threshold():
    same = [0.86]
    at_default = filter_transcription(
        "نستعين",
        same,
        threshold=0.85,
        sequence_confidence=0.90,
    )
    assert at_default.words[0].kept is True
    stricter = filter_transcription(
        "نستعين",
        same,
        threshold=0.90,
        sequence_confidence=0.90,
    )
    assert stricter.words[0].kept is False
    assert stricter.text == ""


def test_default_thresholds_unchanged():
    from app.config import settings

    assert settings.DEFAULT_THRESHOLD == 0.85
    assert settings.WORD_MATCH_THRESHOLD == 0.75
    assert settings.STREAM_COVERAGE_THRESHOLD == 0.85
    assert settings.STT_SEQUENCE_CONFIDENCE_MIN == 0.50
    assert settings.STT_DECODER_PROB_GAMMA == 0.12
    assert settings.STT_AYAH_LEXICON_RECOVERY is True
    assert settings.STT_INVOCAB_FLOOR == 0.55


def test_calibrate_tiny_softmax_onto_slider():
    """Lab exception: raw Tiny P is not Accuracy T."""
    assert calibrate_decoder_prob(0.12) < 0.85
    assert calibrate_decoder_prob(0.22) < 0.85
    assert calibrate_decoder_prob(0.28) >= 0.85
    assert calibrate_decoder_prob(0.88) >= 0.90
    kept = filter_transcription(
        "الرحمن الرحيم",
        [calibrate_decoder_prob(0.32), calibrate_decoder_prob(0.40)],
        threshold=0.85,
        sequence_confidence=calibrate_decoder_prob(0.36),
    )
    assert kept.text.split() == ["الرحمن", "الرحيم"]
    dumped = filter_transcription(
        "يا كلب",
        [calibrate_decoder_prob(0.15), calibrate_decoder_prob(0.18)],
        threshold=0.85,
        sequence_confidence=calibrate_decoder_prob(0.16),
    )
    assert dumped.text == ""


def test_token_pieces_min_subword_confidence():
    words = words_from_token_pieces(
        [("يا", 0.9), (" ", 1.0), ("كل", 0.4), ("ب", 0.95)]
    )
    by_text = {text: conf for text, conf in words}
    assert by_text["يا"] == pytest.approx(0.9)
    assert by_text["كلب"] == pytest.approx(0.4)


def test_sentencepiece_underline_is_word_boundary():
    words = words_from_token_pieces(
        [
            ("▁بسم", 0.9),
            ("▁الله", 0.91),
            ("▁الرحمن", 0.88),
            ("▁الرحيم", 0.92),
        ]
    )
    assert [text for text, _ in words] == ["بسم", "الله", "الرحمن", "الرحيم"]


def test_filter_keeps_spaces_from_full_decode():
    raw = "بسم الله الرحمن الرحيم"
    result = filter_transcription(
        raw,
        [0.9, 0.91, 0.88, 0.92],
        threshold=0.85,
        sequence_confidence=0.90,
    )
    assert result.text == raw
    assert result.text.split() == ["بسم", "الله", "الرحمن", "الرحيم"]


def test_align_prefix_decode_onto_spaced_surface():
    table = {1: "بسم", 2: "الله", 3: "الرحمن", 4: "الرحيم"}

    def decode(ids):
        return " ".join(table[i] for i in ids)

    pairs = align_token_probs_to_decoded_words(
        decode,
        [1, 2, 3, 4],
        [0.9, 0.8, 0.7, 0.95],
        surface_text="بسم الله الرحمن الرحيم",
    )
    assert [text for text, _ in pairs] == ["بسم", "الله", "الرحمن", "الرحيم"]
    assert pairs[1][1] == pytest.approx(0.8)
    assert pairs[2][1] == pytest.approx(0.7)


def test_overgen_guard_keeps_only_expected_like_words():
    expected = "إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ"
    raw = "يا كلب دعياة لست عينا يا كلب دعيا كلا استعين"
    trimmed = trim_overgenerated_partial(expected, raw)
    assert "كلب" not in trimmed
    assert "يا" not in trimmed.split()
