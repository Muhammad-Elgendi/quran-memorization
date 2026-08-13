"""Arabic normalization for comparison only — never mutates the corpus."""

import re
import unicodedata

# Arabic tashkeel / diacritics
TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")

# Quranic annotation marks (overlapping ranges kept explicit for clarity)
QURANIC_MARKS = re.compile(r"[\u06D6-\u06ED]")

# Arabic punctuation / Quran markers / tatweel handled separately
PUNCTUATION = re.compile(r"[،؛؟.!?\(\)\[\]\{\}<>\"'`«»ـ۞۩\u06DD\u06DE\u06E9]")


def normalize_arabic(text: str) -> str:
    """
    Normalize Arabic text for speech-to-text comparison.

    Does NOT modify the canonical Quran corpus — comparison representation only.
    Taa marbuta (ة) is left as-is in Phase 1.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = TASHKEEL.sub("", text)
    text = QURANIC_MARKS.sub("", text)
    text = text.replace("ـ", "")

    text = text.replace("ٱ", "ا")
    text = text.replace("أ", "ا")
    text = text.replace("إ", "ا")
    text = text.replace("آ", "ا")
    text = text.replace("ى", "ي")

    text = PUNCTUATION.sub(" ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def tokenize(text: str) -> list[str]:
    """Normalize then split on whitespace."""
    normalized = normalize_arabic(text)
    if not normalized:
        return []
    return normalized.split()
