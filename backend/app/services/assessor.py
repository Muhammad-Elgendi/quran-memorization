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


@dataclass
class CreditMergeResult:
    """Contiguous-prefix credit after merging one utterance."""

    credit_mask: list[bool]
    credit_cursor: int
    credit_total: int
    cumulative_coverage: float
    window_coverage: float
    complete: bool
    extended: bool
    hypothesis: str
    has_mismatch_at_cursor: bool
    matched_indices: set[int] = field(default_factory=set)


def credit_cursor_from_mask(credit_mask: list[bool]) -> int:
    """Smallest index k such that all C[0..k) are true; N if fully credited."""
    for i, credited in enumerate(credit_mask):
        if not credited:
            return i
    return len(credit_mask)


def empty_credit_mask(expected: str) -> list[bool]:
    return [False] * len(tokenize(expected))


def _matched_expected_indices(
    expected_words: list[str],
    recognized_words: list[str],
    word_match_threshold: float,
    *,
    index_offset: int = 0,
) -> set[int]:
    """Expected indices matched via equal or fuzzy replace (≥ word threshold)."""
    if not expected_words or not recognized_words:
        return set()
    matcher = SequenceMatcher(
        None, expected_words, recognized_words, autojunk=False
    )
    matched: set[int] = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i in range(i1, i2):
                matched.add(index_offset + i)
        elif tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for k in range(paired):
                similarity = (
                    fuzz.ratio(
                        expected_words[i1 + k],
                        recognized_words[j1 + k],
                    )
                    / 100.0
                )
                if similarity >= word_match_threshold:
                    matched.add(index_offset + i1 + k)
    return matched


def _extend_mask_from_cursor(
    credit_mask: list[bool],
    matched: set[int],
    cursor: int,
) -> tuple[list[bool], int]:
    """Credit contiguous matches from cursor; never fill holes."""
    new_mask = list(credit_mask)
    i = cursor
    n = len(new_mask)
    while i < n and i in matched:
        new_mask[i] = True
        i += 1
    return new_mask, i


def _is_credited_prefix_resay(
    expected_words: list[str],
    recognized_words: list[str],
    cursor: int,
    word_match_threshold: float,
) -> bool:
    """True when Heard only re-confirms already-credited prefix tokens."""
    if cursor <= 0 or not recognized_words:
        return False
    prefix = expected_words[:cursor]
    for start in range(len(prefix)):
        slice_words = prefix[start:]
        if not slice_words:
            continue
        matched = _matched_expected_indices(
            slice_words, recognized_words, word_match_threshold
        )
        # Full recognized span matches a trailing slice of the credited prefix.
        if len(matched) == len(slice_words) and len(recognized_words) <= len(
            slice_words
        ):
            # Every recognized token paired under the same rules.
            rec_matched = _matched_expected_indices(
                slice_words, recognized_words, word_match_threshold
            )
            if len(rec_matched) >= min(len(recognized_words), len(slice_words)):
                # Require the recognized sequence to align as a prefix of slice.
                head = slice_words[: len(recognized_words)]
                head_matched = _matched_expected_indices(
                    head, recognized_words, word_match_threshold
                )
                if len(head_matched) == len(head):
                    return True
    return False


def _strip_credited_prefix_resay(
    expected_words: list[str],
    recognized_words: list[str],
    cursor: int,
    word_match_threshold: float,
) -> list[str]:
    """Drop a leading Heard span that only re-says already-credited tokens."""
    if cursor <= 0 or not recognized_words:
        return recognized_words
    prefix = expected_words[:cursor]
    best_strip = 0
    for strip in range(1, len(recognized_words) + 1):
        chunk = recognized_words[:strip]
        for start in range(len(prefix)):
            slice_words = prefix[start:]
            if len(slice_words) != len(chunk):
                continue
            matched = _matched_expected_indices(
                slice_words, chunk, word_match_threshold
            )
            if len(matched) == len(slice_words):
                best_strip = strip
                break
    return recognized_words[best_strip:]


def _is_clean_prefix_extension(
    expected_words: list[str],
    recognized_words: list[str],
    start_cursor: int,
    new_cursor: int,
    word_match_threshold: float,
) -> bool:
    """True when Heard is only a proper prefix extension (no trailing wrong token)."""
    if new_cursor <= start_cursor or not recognized_words:
        return False
    target = expected_words[start_cursor:new_cursor]
    if not target:
        return False
    for start in range(len(recognized_words)):
        chunk = recognized_words[start:]
        if len(chunk) != len(target):
            continue
        matched = _matched_expected_indices(
            target, chunk, word_match_threshold
        )
        if len(matched) == len(target):
            return True
    return False


def _has_mismatch_at_cursor(
    expected_words: list[str],
    recognized_words: list[str],
    cursor: int,
    word_match_threshold: float,
) -> bool:
    """Committed wrong/skip at the next uncredited index (not a prefix re-say)."""
    if not recognized_words or cursor >= len(expected_words):
        return False
    if _is_credited_prefix_resay(
        expected_words, recognized_words, cursor, word_match_threshold
    ):
        return False
    suffix = expected_words[cursor:]
    matched = _matched_expected_indices(
        suffix, recognized_words, word_match_threshold, index_offset=cursor
    )
    if cursor in matched:
        return False
    matcher = SequenceMatcher(None, suffix, recognized_words, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if i1 > 0:
            continue
        if tag == "equal":
            return False
        if tag == "replace":
            return True
        if tag == "delete":
            return j2 > j1 or i2 > i1
        if tag == "insert":
            return True
    return True


def merge_credit(
    expected: str,
    recognized: str,
    credit_mask: list[bool] | None = None,
    *,
    word_match_threshold: float | None = None,
) -> CreditMergeResult:
    """Merge one utterance into a contiguous-prefix credit mask (spec §6)."""
    threshold = (
        settings.WORD_MATCH_THRESHOLD
        if word_match_threshold is None
        else word_match_threshold
    )
    expected_words = tokenize(expected)
    n = len(expected_words)
    if credit_mask is None or len(credit_mask) != n:
        credit_mask = [False] * n
    else:
        credit_mask = list(credit_mask)

    if n == 0:
        return CreditMergeResult(
            credit_mask=[],
            credit_cursor=0,
            credit_total=0,
            cumulative_coverage=1.0,
            window_coverage=0.0,
            complete=True,
            extended=False,
            hypothesis="empty",
            has_mismatch_at_cursor=False,
        )

    k = credit_cursor_from_mask(credit_mask)
    rec_words = tokenize(recognized)
    assessor = MemorizationAssessor(word_match_threshold=threshold)
    window_coverage = (
        assessor.progress(expected, recognized) if rec_words else 0.0
    )

    if not rec_words:
        coverage = k / n
        return CreditMergeResult(
            credit_mask=credit_mask,
            credit_cursor=k,
            credit_total=n,
            cumulative_coverage=coverage,
            window_coverage=0.0,
            complete=k == n,
            extended=False,
            hypothesis="empty",
            has_mismatch_at_cursor=False,
        )

    candidates: list[tuple[int, list[bool], set[int], str]] = []

    # Drop a leading re-say of already-credited tokens so H-full / H-suffix
    # cannot fuzzy-map them onto the next uncredited word (e.g. الرحمن→الرحيم).
    align_words = _strip_credited_prefix_resay(
        expected_words, rec_words, k, threshold
    )
    if not align_words:
        coverage = k / n
        prefix_resay = _is_credited_prefix_resay(
            expected_words, rec_words, k, threshold
        )
        return CreditMergeResult(
            credit_mask=credit_mask,
            credit_cursor=k,
            credit_total=n,
            cumulative_coverage=coverage,
            window_coverage=window_coverage,
            complete=k == n,
            extended=False,
            hypothesis="prefix-resay",
            has_mismatch_at_cursor=not prefix_resay and bool(rec_words),
            matched_indices=set(),
        )

    matched_full = _matched_expected_indices(
        expected_words, align_words, threshold
    )
    mask_full, cursor_full = _extend_mask_from_cursor(
        credit_mask, matched_full, k
    )
    candidates.append((cursor_full, mask_full, matched_full, "H-full"))

    best_suffix_cursor = k
    best_suffix_mask = credit_mask
    best_suffix_matched: set[int] = set()
    for start in range(len(align_words)):
        matched = _matched_expected_indices(
            expected_words, align_words[start:], threshold
        )
        mask_s, cursor_s = _extend_mask_from_cursor(credit_mask, matched, k)
        if cursor_s > best_suffix_cursor or (
            cursor_s == best_suffix_cursor
            and len(matched) > len(best_suffix_matched)
        ):
            best_suffix_cursor = cursor_s
            best_suffix_mask = mask_s
            best_suffix_matched = matched
    candidates.append(
        (best_suffix_cursor, best_suffix_mask, best_suffix_matched, "H-suffix")
    )

    if k < n:
        matched_resume = _matched_expected_indices(
            expected_words[k:],
            align_words,
            threshold,
            index_offset=k,
        )
        mask_r, cursor_r = _extend_mask_from_cursor(
            credit_mask, matched_resume, k
        )
        candidates.append((cursor_r, mask_r, matched_resume, "H-resume"))

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_cursor, best_mask, best_matched, best_hyp = candidates[0]
    if k > 0:
        full_cursor = next(c[0] for c in candidates if c[3] == "H-full")
        resume = next((c for c in candidates if c[3] == "H-resume"), None)
        if (
            resume is not None
            and resume[0] > k
            and full_cursor == k
            and resume[0] >= best_cursor
        ):
            best_cursor, best_mask, best_matched, best_hyp = resume

    extended = best_cursor > k
    clean_extension = _is_clean_prefix_extension(
        expected_words, align_words, k, best_cursor, threshold
    )
    prefix_resay = _is_credited_prefix_resay(
        expected_words, rec_words, k, threshold
    )
    # Fail path: non-empty Heard that is neither a clean extension, a full
    # completion, nor a benign re-say of already-credited words.
    mismatch = False
    if best_cursor < n and not prefix_resay:
        if extended and not clean_extension:
            mismatch = True
        elif not extended:
            mismatch = _has_mismatch_at_cursor(
                expected_words, align_words or rec_words, k, threshold
            )

    return CreditMergeResult(
        credit_mask=best_mask,
        credit_cursor=best_cursor,
        credit_total=n,
        cumulative_coverage=best_cursor / n,
        window_coverage=window_coverage,
        complete=best_cursor == n,
        extended=extended,
        hypothesis=best_hyp,
        has_mismatch_at_cursor=mismatch,
        matched_indices=best_matched,
    )


def alignment_from_credit(
    expected: str,
    credit_mask: list[bool],
    *,
    live_matched: set[int] | None = None,
) -> list[dict]:
    """Build UI alignment: credited ∪ live matches as equal; else delete."""
    expected_words = tokenize(expected)
    live = live_matched or set()
    alignment: list[dict] = []
    for i, word in enumerate(expected_words):
        if (i < len(credit_mask) and credit_mask[i]) or i in live:
            alignment.append(
                {"op": "equal", "expected": word, "recognized": word}
            )
        else:
            alignment.append(
                {"op": "delete", "expected": word, "recognized": None}
            )
    return alignment


def credit_complete_assessment(
    expected: str,
    *,
    threshold: float | None = None,
    credit_utterances: int = 1,
) -> AssessmentResult:
    """Passing AssessmentResult when multi-utterance credit filled the ayah."""
    assessor = MemorizationAssessor(threshold=threshold)
    # Option A: synthetic recognized = full expected → score ≈ 1.0.
    result = assessor.assess(expected, expected)
    pass_threshold = assessor.threshold
    score = max(pass_threshold, result.score)
    message = result.message
    if credit_utterances > 1:
        message = "Completed across multiple utterances."
    return AssessmentResult(
        score=score,
        passed=True,
        expected=expected,
        recognized=expected,
        missing_words=[],
        extra_words=[],
        wrong_words=[],
        message=message,
        warning=False,
        alignment=result.alignment,
    )


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

    def progress(self, expected: str, recognized: str) -> float:
        """Fraction of expected tokens matched (0–1), best over recognized suffixes."""
        expected_words = tokenize(expected)
        if not expected_words:
            return 0.0
        rec_words = tokenize(recognized)
        if not rec_words:
            return 0.0
        best = 0.0
        for start in range(len(rec_words)):
            ratio = self._token_match_ratio(expected_words, rec_words[start:])
            best = max(best, ratio)
        return best

    def _token_match_ratio(
        self,
        expected_words: list[str],
        recognized_words: list[str],
    ) -> float:
        """Fraction of expected tokens heard (exact or fuzzy replace).

        Mirrors ``_assess_direct`` pairing: ``equal`` opcodes count fully;
        ``replace`` pairs count when ``fuzz.ratio >= word_match_threshold``.
        Unpaired expected words in a replace slice stay unmatched; inserts
        do not increase coverage.
        """
        if not expected_words:
            return 0.0
        matcher = SequenceMatcher(
            None, expected_words, recognized_words, autojunk=False
        )
        matched = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                matched += i2 - i1
            elif tag == "replace":
                paired = min(i2 - i1, j2 - j1)
                for k in range(paired):
                    similarity = (
                        fuzz.ratio(
                            expected_words[i1 + k],
                            recognized_words[j1 + k],
                        )
                        / 100.0
                    )
                    if similarity >= self.word_match_threshold:
                        matched += 1
        return matched / len(expected_words)

    def assess(self, expected: str, recognized: str) -> AssessmentResult:
        rec_words = tokenize(recognized)
        if len(rec_words) <= 1:
            return self._assess_direct(expected, recognized)

        best = self._assess_direct(expected, recognized)
        for start in range(1, len(rec_words)):
            suffix = " ".join(rec_words[start:])
            candidate = self._assess_direct(expected, suffix)
            if candidate.score > best.score:
                best = candidate
        return best

    def _assess_direct(self, expected: str, recognized: str) -> AssessmentResult:
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
