from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.memorization import create_router as create_memorization_router
from app.api.quran import create_router as create_quran_router
from app.services.assessor import MemorizationAssessor
from app.services.normalizer import normalize_arabic, tokenize
from app.services.quran_service import QuranService
from app.services.speech_service import MockSpeechRecognizer

FIXTURE = Path(__file__).parent / "fixtures" / "quran_sample.json"


@pytest.fixture
def quran_service() -> QuranService:
    return QuranService(FIXTURE)


# --- Normalizer ---


def test_normalize_strips_diacritics():
    with_tashkeel = "بِسْمِ ٱللَّهِ"
    without = "بسم الله"
    assert normalize_arabic(with_tashkeel) == normalize_arabic(without)


def test_normalize_alef_variants():
    assert normalize_arabic("أحمد") == normalize_arabic("احمد")
    assert normalize_arabic("إيمان") == normalize_arabic("ايمان")
    assert normalize_arabic("آمن") == normalize_arabic("امن")
    assert "ٱ" not in normalize_arabic("ٱللَّه")


def test_normalize_empty_and_punctuation():
    assert normalize_arabic("") == ""
    assert normalize_arabic("؟") == ""
    tokens = tokenize("الله، غفور؟")
    assert tokens == ["الله", "غفور"]


# --- Assessor ---


def test_assess_exact_match():
    expected = "وَاللَّهُ غَفُورٌ رَّحِيمٌ"
    recognized = "والله غفور رحيم"
    result = MemorizationAssessor(threshold=0.85).assess(expected, recognized)
    assert result.passed
    assert result.score >= 0.85
    assert result.wrong_words == []


def test_assess_wrong_word():
    expected = "والله غفور رحيم"
    recognized = "والله غفور عليم"
    result = MemorizationAssessor(threshold=0.85).assess(expected, recognized)
    assert not result.passed or result.wrong_words
    assert any(w["expected"] == "رحيم" for w in result.wrong_words)


def test_assess_missing_tail():
    expected = "الحمد لله رب العالمين"
    recognized = "الحمد لله"
    result = MemorizationAssessor(threshold=0.85).assess(expected, recognized)
    assert result.missing_words
    assert result.score < 1.0


def test_assess_extra_words():
    expected = "الحمد لله"
    recognized = "الحمد لله رب العالمين"
    result = MemorizationAssessor(threshold=0.85).assess(expected, recognized)
    assert result.extra_words


def test_assess_threshold_boundary():
    expected = "الحمد لله"
    recognized = "الحمد لله"
    result = MemorizationAssessor(threshold=1.0).assess(expected, recognized)
    assert result.passed
    assert result.score == 1.0


def test_assess_restart_from_beginning():
    """Restarting mid-ayah should score the best suffix, not penalize duplicates."""
    expected = "الحمد لله رب العالمين"
    recognized = "الحمد لله الحمد لله رب العالمين"
    result = MemorizationAssessor(threshold=0.85).assess(expected, recognized)
    assert result.passed
    assert result.score >= 0.85


def test_progress_uses_best_suffix():
    assessor = MemorizationAssessor()
    expected = "الحمد لله رب العالمين"
    partial = "الحمد لله"
    full_with_restart = "الحمد لله الحمد لله رب العالمين"
    assert assessor.progress(expected, partial) == pytest.approx(2 / 4)
    assert assessor.progress(expected, full_with_restart) == pytest.approx(1.0)


# --- Coverage vs STT-like simple Arabic (ayah-advance-fix-spec §10.1) ---


def test_progress_fatihah_1_2_stt_like_full(quran_service: QuranService):
    """A1: dagger-alef seat mismatch must still count as coverage complete."""
    expected = quran_service.get_ayah(1, 2)["text"]
    recognized = "الحمد لله رب العالمين"
    assessor = MemorizationAssessor(threshold=0.85)
    assert assessor.progress(expected, recognized) == pytest.approx(1.0)


def test_assess_fatihah_1_2_stt_like_passes(quran_service: QuranService):
    """A2: same pair would pass the accuracy threshold once finalized."""
    expected = quran_service.get_ayah(1, 2)["text"]
    recognized = "الحمد لله رب العالمين"
    result = MemorizationAssessor(threshold=0.85).assess(expected, recognized)
    assert result.passed
    assert result.score >= 0.85
    assert result.wrong_words == []


def test_progress_fatihah_1_2_mid_ayah_blocked(quran_service: QuranService):
    """A3 / G4: partial recitation must stay below the coverage gate."""
    expected = quran_service.get_ayah(1, 2)["text"]
    recognized = "الحمد لله"
    progress = MemorizationAssessor().progress(expected, recognized)
    assert progress == pytest.approx(0.5)
    assert progress < 0.85


def test_progress_fatihah_1_4_stt_like(quran_service: QuranService):
    """A4: مالك vs ملك (dagger alef) counts as matched."""
    expected = quran_service.get_ayah(1, 4)["text"]
    assert MemorizationAssessor().progress(expected, "مالك يوم الدين") == pytest.approx(
        1.0
    )


def test_progress_fatihah_1_6_stt_like(quran_service: QuranService):
    """A5: الصراط vs الصرط counts as matched."""
    expected = quran_service.get_ayah(1, 6)["text"]
    assert MemorizationAssessor().progress(
        expected, "اهدنا الصراط المستقيم"
    ) == pytest.approx(1.0)


def test_progress_fatihah_1_1_rahman_not_broken(quran_service: QuranService):
    """A6: must not regress الرحمن (blind U+0670→ا would break this)."""
    expected = quran_service.get_ayah(1, 1)["text"]
    assert MemorizationAssessor().progress(
        expected, "بسم الله الرحمن الرحيم"
    ) == pytest.approx(1.0)


def test_progress_fatihah_1_2_restart_suffix(quran_service: QuranService):
    """A7: best-suffix still finds the complete ayah after a restart."""
    expected = quran_service.get_ayah(1, 2)["text"]
    recognized = "الحمد لله الحمد لله رب العالمين"
    assert MemorizationAssessor().progress(expected, recognized) == pytest.approx(1.0)


def test_progress_fatihah_1_2_near_miss_documented(quran_service: QuranService):
    """A8: العليين≈العلمين (85.7%) counts at WORD_MATCH_THRESHOLD=0.75.

    A stricter COVERAGE_WORD_THRESHOLD (e.g. 0.90) would prefer progress < 0.85;
    that split is deferred (spec §7 / open question 2).
    """
    expected = quran_service.get_ayah(1, 2)["text"]
    progress = MemorizationAssessor().progress(expected, "الحمد لله رب العليين")
    assert progress == pytest.approx(1.0)


# --- QuranService ---


def test_quran_service_valid(quran_service: QuranService):
    surahs = quran_service.get_surahs()
    assert len(surahs) >= 1
    assert surahs[0]["number"] == 1
    ayah = quran_service.get_ayah(1, 1)
    assert ayah is not None
    assert "text" in ayah
    assert quran_service.get_ayah(36, 1) is not None


def test_quran_service_invalid(quran_service: QuranService):
    assert quran_service.get_surah(999) is None
    assert quran_service.get_ayah(1, 999) is None
    assert quran_service.get_range(999, 1, 2) == []
    rng = quran_service.get_range(1, 1, 2)
    assert len(rng) == 2


# --- API ---


@pytest.fixture
def api_client(quran_service: QuranService, tmp_path: Path) -> TestClient:
    recognizer = MockSpeechRecognizer(
        transcript="بسم الله الرحمن الرحيم",
    )
    app = FastAPI()
    app.include_router(create_quran_router(quran_service))
    app.include_router(
        create_memorization_router(quran_service, recognizer=recognizer)
    )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "test"}

    return TestClient(app)


def _write_wav(path: Path, seconds: float = 1.0) -> Path:
    sr = 16000
    samples = np.zeros(int(sr * seconds), dtype=np.float32)
    sf.write(path, samples, sr)
    return path


def test_list_surahs(api_client: TestClient):
    response = api_client.get("/api/quran/surahs")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert data[0]["ayah_count"] == 7


def test_assess_with_mock(api_client: TestClient, tmp_path: Path):
    wav = _write_wav(tmp_path / "sample.wav", seconds=1.0)
    with wav.open("rb") as f:
        response = api_client.post(
            "/api/memorization/assess",
            data={"surah": "1", "ayah": "1", "threshold": "0.5"},
            files={"audio": ("sample.wav", f, "audio/wav")},
        )
    assert response.status_code == 200
    body = response.json()
    assert "score" in body
    assert "passed" in body
    assert "wrong_words" in body


def test_assess_filters_low_confidence_garbage(
    quran_service: QuranService, tmp_path: Path
):
    recognizer = MockSpeechRecognizer(
        transcript="يا كلب دعياة لست عينا يا كلب دعيا كلا استعين",
        word_confidences=[0.15, 0.12, 0.20, 0.18, 0.22, 0.14, 0.11, 0.19, 0.16, 0.88],
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


def test_assess_empty_filtered_is_fail_200(
    quran_service: QuranService, tmp_path: Path
):
    recognizer = MockSpeechRecognizer(transcript="")
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
    assert body["passed"] is False
    assert body["recognized"] == ""


def test_assess_unknown_ayah(api_client: TestClient, tmp_path: Path):
    wav = _write_wav(tmp_path / "sample.wav")
    with wav.open("rb") as f:
        response = api_client.post(
            "/api/memorization/assess",
            data={"surah": "1", "ayah": "99"},
            files={"audio": ("sample.wav", f, "audio/wav")},
        )
    assert response.status_code == 404


def test_assess_empty_file(api_client: TestClient):
    response = api_client.post(
        "/api/memorization/assess",
        data={"surah": "1", "ayah": "1"},
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    assert response.status_code == 400


def test_assess_webm_via_ffmpeg(api_client: TestClient, tmp_path: Path):
    """Browser MediaRecorder sends WebM; librosa/soundfile cannot open it."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")

    wav = _write_wav(tmp_path / "sample.wav", seconds=1.0)
    webm = tmp_path / "recitation.webm"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav), "-c:a", "libopus", str(webm)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"ffmpeg cannot encode opus webm: {result.stderr[-200:]}")

    with webm.open("rb") as f:
        response = api_client.post(
            "/api/memorization/assess",
            data={"surah": "1", "ayah": "1", "threshold": "0.5"},
            files={"audio": ("recitation.webm", f, "audio/webm")},
        )
    assert response.status_code == 200
    assert "score" in response.json()
