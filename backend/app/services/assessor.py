"""Memorization assessment with sequence alignment."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from rapidfuzz import fuzz

from ..config import settings
from .normalizer import normalize_arabic, tokenize


@dataclass
class AssessmentResult:
    score: float
    passed: bool
    expected: str
    recognized: str
    missing_words: list[str]
    extra_words: list[str]
    wrong_words: list[dict]
    message: str
    warning: bool = False
    alignment: list[dict] = field(default_factory=list)


class MemorizationAssessor:
    def __init__(
        self,
        threshold: float | None = None,
        word_match_threshold: float | None = None,
    ):
        self.threshold = (
            settings.DEFAULT_THRESHOLD if threshold is None else threshold
        )
        self.word_match_threshold = (
            settings.WORD_MATCH_THRESHOLD
            if word_match_threshold is None
            else word_match_threshold
        )

    def assess(self, expected: str, recognized: str) -> AssessmentResult:
        expected_normalized = normalize_arabic(expected)
        recognized_normalized = normalize_arabic(recognized)

        expected_words = tokenize(expected)
        recognized_words = tokenize(recognized)

        score = fuzz.ratio(expected_normalized, recognized_normalized) / 100.0

        missing: list[str] = []
        extra: list[str] = []
        wrong: list[dict] = []
        alignment: list[dict] = []

        matcher = SequenceMatcher(
            None,
            expected_words,
            recognized_words,
            autojunk=False,
        )

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for idx in range(i2 - i1):
                    alignment.append(
                        {
                            "op": "equal",
                            "expected": expected_words[i1 + idx],
                            "recognized": recognized_words[j1 + idx],
                        }
                    )
            elif tag == "delete":
                for word in expected_words[i1:i2]:
                    missing.append(word)
                    alignment.append(
                        {"op": "delete", "expected": word, "recognized": None}
                    )
            elif tag == "insert":
                for word in recognized_words[j1:j2]:
                    extra.append(word)
                    alignment.append(
                        {"op": "insert", "expected": None, "recognized": word}
                    )
            elif tag == "replace":
                exp_slice = expected_words[i1:i2]
                rec_slice = recognized_words[j1:j2]
                paired = min(len(exp_slice), len(rec_slice))

                for k in range(paired):
                    exp_word = exp_slice[k]
                    rec_word = rec_slice[k]
                    similarity = fuzz.ratio(exp_word, rec_word) / 100.0
                    if similarity < self.word_match_threshold:
                        wrong.append(
                            {
                                "expected": exp_word,
                                "recognized": rec_word,
                                "similarity": similarity,
                            }
                        )
                        alignment.append(
                            {
                                "op": "replace",
                                "expected": exp_word,
                                "recognized": rec_word,
                                "similarity": similarity,
                            }
                        )
                    else:
                        alignment.append(
                            {
                                "op": "equal",
                                "expected": exp_word,
                                "recognized": rec_word,
                                "similarity": similarity,
                            }
                        )

                for word in exp_slice[paired:]:
                    missing.append(word)
                    alignment.append(
                        {"op": "delete", "expected": word, "recognized": None}
                    )
                for word in rec_slice[paired:]:
                    extra.append(word)
                    alignment.append(
                        {"op": "insert", "expected": None, "recognized": word}
                    )

        passed = score >= self.threshold

        if passed:
            message = (
                "Excellent. Your recitation closely matches the selected ayah."
            )
        else:
            message = (
                "There may be a memorization error. "
                "Please review the highlighted words."
            )

        return AssessmentResult(
            score=score,
            passed=passed,
            expected=expected,
            recognized=recognized,
            missing_words=missing,
            extra_words=extra,
            wrong_words=wrong,
            message=message,
            warning=not passed,
            alignment=alignment,
        )
