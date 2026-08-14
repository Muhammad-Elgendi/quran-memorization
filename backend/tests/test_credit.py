"""Unit tests for multi-utterance contiguous credit merge (spec C1–C7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.assessor import (
    credit_cursor_from_mask,
    empty_credit_mask,
    merge_credit,
)
from app.services.normalizer import tokenize
from app.services.quran_service import QuranService

FIXTURE = Path(__file__).parent / "fixtures" / "quran_sample.json"

# Ayah 2:3 token shape (normalized) — used as the screenshot worked example.
AYAH_2_3 = "الذين يؤمنون بالغيب ويقيمون الصلوة ومما رزقنهم ينفقون"


@pytest.fixture
def quran_service() -> QuranService:
    return QuranService(FIXTURE)


def _mask_with_prefix(n: int, cursor: int) -> list[bool]:
    return [i < cursor for i in range(n)]


def test_c1_full_ayah_from_empty():
    tokens = tokenize(AYAH_2_3)
    merge = merge_credit(AYAH_2_3, AYAH_2_3, empty_credit_mask(AYAH_2_3))
    assert merge.complete
    assert merge.credit_cursor == len(tokens)
    assert merge.cumulative_coverage == 1.0


def test_c2_suffix_after_prefix_credit_completes():
    tokens = tokenize(AYAH_2_3)
    mask = _mask_with_prefix(len(tokens), 5)
    suffix = " ".join(tokens[5:])
    merge = merge_credit(AYAH_2_3, suffix, mask)
    assert merge.complete
    assert merge.credit_cursor == 8
    assert merge.hypothesis in {"H-resume", "H-suffix", "H-full"}


def test_c3_mid_ayah_suffix_without_prefix_stays_zero():
    tokens = tokenize(AYAH_2_3)
    suffix = " ".join(tokens[5:])
    merge = merge_credit(AYAH_2_3, suffix, empty_credit_mask(AYAH_2_3))
    assert not merge.complete
    assert merge.credit_cursor == 0
    assert merge.cumulative_coverage == 0.0


def test_c4_wrong_token_at_cursor_no_holes():
    tokens = tokenize(AYAH_2_3)
    mask = _mask_with_prefix(len(tokens), 5)
    merge = merge_credit(AYAH_2_3, "باطل كلام خطأ", mask)
    assert merge.credit_cursor == 5
    assert not any(merge.credit_mask[5:])
    assert merge.has_mismatch_at_cursor


def test_c5_overlap_resay_completes():
    tokens = tokenize(AYAH_2_3)
    mask = _mask_with_prefix(len(tokens), 5)
    overlap = " ".join(tokens[3:])
    merge = merge_credit(AYAH_2_3, overlap, mask)
    assert merge.complete
    assert merge.credit_cursor == 8


def test_c6_skip_ahead_does_not_fill_hole():
    tokens = tokenize(AYAH_2_3)
    mask = _mask_with_prefix(len(tokens), 4)
    skip = " ".join(tokens[5:])
    merge = merge_credit(AYAH_2_3, skip, mask)
    assert merge.credit_cursor == 4
    assert not merge.complete


def test_c7_fuzzy_dagger_alef_style_match(quran_service: QuranService):
    """Same match rules as assessor — simple Arabic credits when fuzzy-equal."""
    expected = quran_service.get_ayah(1, 2)["text"]
    recognized = "الحمد لله رب العلمين"
    merge = merge_credit(expected, recognized, empty_credit_mask(expected))
    assert merge.complete
    assert merge.credit_cursor == len(tokenize(expected))


def test_credit_cursor_helper():
    assert credit_cursor_from_mask([]) == 0
    assert credit_cursor_from_mask([True, True, False]) == 2
    assert credit_cursor_from_mask([True, True, True]) == 3
