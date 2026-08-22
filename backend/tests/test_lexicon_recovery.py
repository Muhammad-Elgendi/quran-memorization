"""Ayah-constrained Heard recovery (uthmani-tanzeel-word-matching-spec §9.1)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.memorization import create_router as create_memorization_router
from app.services.assessor import MemorizationAssessor
from app.services.quran_service import QuranService
from app.services.speech_service import MockSpeechRecognizer
from app.services.stt_confidence import (
    apply_ayah_recovery,
    filter_transcription,
    recover_against_ayah,
    transcription_from_plain_text,
)

FIXTURE = Path(__file__).parent / "fixtures" / "quran_sample.json"

UTHMANI_1_1 = "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ"
TANZEEL_1_1 = "بسم الله الرحمن الرحيم"
TANZEEL_TASHKEEL_1_1 = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ"
SCREENSHOT_HEARD_1_1 = "الله الرحمن الرحيم"
AGGLUTINATED_1_1 = "بسمالله الرحمن الرحيم"
HALLUCINATION = "يا كلب دعياة لست عينا يا كلب دعيا كلا استعين"
HALLUCINATION_CONFS = [0.15, 0.12, 0.20, 0.18, 0.22, 0.14, 0.11, 0.19, 0.16, 0.88]


@pytest.fixture
def quran_service() -> QuranService:
    return QuranService(FIXTURE)


def _write_wav(path: Path, seconds: float = 1.0) -> Path:
    sr = 16000
    samples = np.zeros(int(sr * seconds), dtype=np.float32)
    sf.write(path, samples, sr)
    return path


def test_corpus_1_1_uthmani_unchanged(quran_service: QuranService):
    assert quran_service.get_ayah(1, 1)["text"] == UTHMANI_1_1


def test_l1_revives_dropped_bism_at_invocab_floor(quran_service: QuranService):
    expected = quran_service.get_ayah(1, 1)["text"]
    filtered = filter_transcription(
        TANZEEL_1_1,
        [0.62, 0.9, 0.9, 0.9],
        threshold=0.85,
        sequence_confidence=0.83,
    )
    assert filtered.text == SCREENSHOT_HEARD_1_1
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text.split()[0] == "بسم"
    assert recovered.text == TANZEEL_1_1
    kept = {word.text: word.kept for word in recovered.words}
    assert kept["بسم"] is True
    assert recovered.recovery_revived == ["بسم"]
    assert MemorizationAssessor().progress(expected, recovered.text) == pytest.approx(
        1.0
    )


def test_l2_exact_match_revives_below_invocab_floor(quran_service: QuranService):
    """Exact normalized hit revives even under invocab floor (Whisper under-conf)."""
    expected = quran_service.get_ayah(1, 1)["text"]
    filtered = filter_transcription(
        TANZEEL_1_1,
        [0.40, 0.9, 0.9, 0.9],
        threshold=0.85,
        sequence_confidence=0.78,
    )
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text.split()[0] == "بسم"
    assert recovered.recovery_revived == ["بسم"]
    assert MemorizationAssessor().progress(expected, recovered.text) == pytest.approx(
        1.0
    )


def test_near_miss_ism_revives_as_bism():
    """Lab 2026-08-20: Whisper اسم/إسم for بسم → rewrite onto expected."""
    expected = "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ الٓمٓ"
    filtered = filter_transcription(
        "اسم الله الرحمن الرحيم الم",
        [0.80, 0.99, 0.99, 0.99, 0.99],
        threshold=0.85,
        sequence_confidence=0.95,
    )
    assert "اسم" not in filtered.text.split()  # dropped by keep floor
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text.split()[0] == "بسم"
    assert "اسم" in recovered.recovery_revived
    assert MemorizationAssessor(threshold=0.85).assess(
        expected, recovered.text
    ).passed


def test_near_miss_kept_ism_rewritten():
    """High-confidence اسم (if kept) still rewrites to بسم for scoring."""
    expected = "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ"
    filtered = filter_transcription(
        "اسم الله الرحمن الرحيم",
        [0.90, 0.99, 0.99, 0.99],
        threshold=0.85,
        sequence_confidence=0.96,
    )
    assert filtered.text.split()[0] == "اسم"
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text.split()[0] == "بسم"
    assert MemorizationAssessor(threshold=0.85).assess(
        expected, recovered.text
    ).wrong_words == []


def test_near_miss_al_rewrites_to_alm():
    expected = "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ الٓمٓ"
    filtered = filter_transcription(
        "الر",
        [0.94],
        threshold=0.85,
        sequence_confidence=0.94,
    )
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text == "الم"


def test_low_sequence_exact_muqattaat_revived(quran_service: QuranService):
    """Lab 2026-08-20: gamma=1.0 dumps short letters at ~0.40; recover exact hit."""
    expected = quran_service.get_ayah(36, 1)["text"]
    filtered = filter_transcription(
        "يس",
        [0.40],
        threshold=0.85,
        sequence_confidence=0.40,
    )
    assert filtered.skipped_reason == "low_sequence"
    assert filtered.text == ""
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text == "يس"
    assert recovered.recovery_revived == ["يس"]
    assert recovered.skipped_reason is None


def test_l3_splits_agglutinated_bismillah(quran_service: QuranService):
    expected = quran_service.get_ayah(1, 1)["text"]
    filtered = filter_transcription(
        AGGLUTINATED_1_1,
        [0.9, 0.9, 0.9],
        threshold=0.85,
        sequence_confidence=0.9,
    )
    recovered = recover_against_ayah(expected, filtered)
    assert recovered.text.split() == TANZEEL_1_1.split()
    assert recovered.recovery_split
    assert recovered.recovery_split[0]["from"] == "بسمالله"
    assert recovered.recovery_split[0]["into"] == ["بسم", "الله"]
    assessor = MemorizationAssessor()
    assert assessor.progress(expected, recovered.text) == pytest.approx(1.0)
    result = assessor.assess(expected, recovered.text)
    equals = [op for op in result.alignment if op["op"] == "equal"]
    assert len(equals) == 4
    assert all(op["op"] == "equal" for op in result.alignment)


def test_l3b_near_miss_split_larayb_before_fuzzy_rayb():
    """``لريب`` must split to لا+ريب, not fuzzy-match ريب and skip لا."""
    expected = "ذَٰلِكَ ٱلْكِتَٰبُ لَا رَيْبَ فِيهِ هُدًى لِّلْمُتَّقِينَ"
    heard = "ذلك الكتاب لريب فيه هدى للمتقين"
    recovered = recover_against_ayah(
        expected, transcription_from_plain_text(heard)
    )
    assert "لا" in recovered.text.split()
    assert "ريب" in recovered.text.split()
    assert "لريب" not in recovered.text.split()
    assert recovered.recovery_split
    assert recovered.recovery_split[0]["from"] in {"لريب", "لَرَيْبَ"}
    assert recovered.recovery_split[0]["into"] == ["لا", "ريب"]
    from app.services.assessor import merge_credit

    merge = merge_credit(expected, recovered.text)
    assert merge.complete
    assert merge.has_mismatch_at_cursor is False


def test_l3c_rewrites_truncated_tail_prefix():
    """Cut-off finals ``رزق``/``ينف`` complete onto ``رزقنهم``/``ينفقون``."""
    expected = "ٱلَّذِينَ يُؤْمِنُونَ بِٱلْغَيْبِ وَيُقِيمُونَ ٱلصَّلَوٰةَ وَمِمَّا رَزَقْنَٰهُمْ يُنفِقُونَ"
    heard = "الذين يؤمنون بالغيب ويقيمون الصلوة ومما رزق ينف"
    recovered = recover_against_ayah(
        expected, transcription_from_plain_text(heard)
    )
    from app.services.normalizer import tokenize

    got = tokenize(recovered.text)
    exp = tokenize(expected)
    assert got[-2:] == exp[-2:]
    assert "رزق" not in got
    assert "ينف" not in got
    assert set(recovered.recovery_revived) >= {"رزق", "ينف"}
    from app.services.assessor import merge_credit

    assert merge_credit(expected, recovered.text).complete


def test_l4_hallucination_not_revived_on_1_5(quran_service: QuranService):
    expected = quran_service.get_ayah(1, 5)["text"]
    filtered = filter_transcription(
        HALLUCINATION,
        HALLUCINATION_CONFS,
        threshold=0.85,
        sequence_confidence=0.70,
    )
    recovered = recover_against_ayah(expected, filtered)
    assert "كلب" not in recovered.text
    assert "يا" not in recovered.text.split()
    assert "بسم" not in recovered.text.split()
    assert recovered.recovery_revived == []


def test_l5_tanzeel_tashkeel_full_basmala_progress(quran_service: QuranService):
    expected = quran_service.get_ayah(1, 1)["text"]
    transcription = transcription_from_plain_text(TANZEEL_TASHKEEL_1_1)
    recovered = recover_against_ayah(expected, transcription)
    assert MemorizationAssessor().progress(expected, recovered.text) == pytest.approx(
        1.0
    )


def test_l6_basim_fuzzy_matches_first_token(quran_service: QuranService):
    expected = quran_service.get_ayah(1, 1)["text"]
    transcription = transcription_from_plain_text("باسم الله الرحمن الرحيم")
    recovered = recover_against_ayah(expected, transcription)
    assert MemorizationAssessor().progress(expected, recovered.text) == pytest.approx(
        1.0
    )


def test_screenshot_heard_without_bism_not_invented(quran_service: QuranService):
    expected = quran_service.get_ayah(1, 1)["text"]
    transcription = transcription_from_plain_text(SCREENSHOT_HEARD_1_1)
    recovered = recover_against_ayah(expected, transcription)
    assert recovered.text == SCREENSHOT_HEARD_1_1
    assert recovered.recovery_revived == []
    assert MemorizationAssessor().progress(expected, recovered.text) == pytest.approx(
        0.75
    )


def test_flag_off_skips_recovery(quran_service: QuranService, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "STT_AYAH_LEXICON_RECOVERY", False)
    expected = quran_service.get_ayah(1, 1)["text"]
    filtered = filter_transcription(
        TANZEEL_1_1,
        [0.62, 0.9, 0.9, 0.9],
        threshold=0.85,
        sequence_confidence=0.83,
    )
    recovered = apply_ayah_recovery(expected, filtered)
    assert recovered.text == SCREENSHOT_HEARD_1_1


def test_low_sequence_junk_not_revived(quran_service: QuranService):
    """Hallucination-only low_sequence dump stays empty (no exact in-vocab hits)."""
    expected = quran_service.get_ayah(1, 5)["text"]
    dumped = filter_transcription(
        "يا كلب دعياة",
        [0.21, 0.18, 0.22],
        threshold=0.85,
    )
    assert dumped.skipped_reason == "low_sequence"
    recovered = recover_against_ayah(expected, dumped)
    assert recovered.text == ""
    assert recovered.recovery_revived == []
    assert recovered is dumped


def test_rest_revives_dropped_bism(quran_service: QuranService, tmp_path: Path):
    recognizer = MockSpeechRecognizer(
        transcript=TANZEEL_1_1,
        word_confidences=[0.62, 0.9, 0.9, 0.9],
        sequence_confidence=0.83,
    )
    app = FastAPI()
    app.include_router(
        create_memorization_router(quran_service, recognizer=recognizer)
    )
    client = TestClient(app)
    wav = _write_wav(tmp_path / "sample.wav", seconds=1.0)
    with wav.open("rb") as f:
        response = client.post(
            "/api/memorization/assess",
            data={"surah": "1", "ayah": "1", "threshold": "0.85"},
            files={"audio": ("sample.wav", f, "audio/wav")},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["recognized"].split()[0] == "بسم"
    assert body["raw_recognized"] == SCREENSHOT_HEARD_1_1
    assert body["recovery"]["revived"] == ["بسم"]
    kept = {word["text"]: word["kept"] for word in body["stt_words"]}
    assert kept["بسم"] is True
    equals = [op for op in body["alignment"] if op["op"] == "equal"]
    assert len(equals) == 4


def test_rest_s5_hallucination_still_suppressed(
    quran_service: QuranService, tmp_path: Path
):
    recognizer = MockSpeechRecognizer(
        transcript=HALLUCINATION,
        word_confidences=HALLUCINATION_CONFS,
        sequence_confidence=0.70,
    )
    app = FastAPI()
    app.include_router(
        create_memorization_router(quran_service, recognizer=recognizer)
    )
    client = TestClient(app)
    wav = _write_wav(tmp_path / "sample.wav", seconds=1.0)
    with wav.open("rb") as f:
        response = client.post(
            "/api/memorization/assess",
            data={"surah": "1", "ayah": "5", "threshold": "0.85"},
            files={"audio": ("sample.wav", f, "audio/wav")},
        )
    assert response.status_code == 200
    body = response.json()
    assert "كلب" not in body["recognized"]
    assert "يا" not in body["recognized"].split()
