"""Tests for realtime WebSocket stream session."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.memorization_stream import create_router as create_stream_router
from app.config import settings
from app.services.quran_service import QuranService
from app.services.speech_service import MockSpeechRecognizer
from app.services.stream_audio import EnergyVadSegmenter, PcmRingBuffer
from app.services.stream_session import SessionState, StreamSession, StreamSessionConfig

FIXTURE = Path(__file__).parent / "fixtures" / "quran_sample.json"


@pytest.fixture
def quran_service() -> QuranService:
    return QuranService(FIXTURE)


def _pcm_silence(ms: int, sr: int = 16000, amp: float = 0.0) -> bytes:
    n = int(sr * ms / 1000)
    samples = (np.zeros(n, dtype=np.float32) + amp)
    return (samples * 32767.0).astype("<i2").tobytes()


def _pcm_tone(ms: int, sr: int = 16000, amp: float = 0.2) -> bytes:
    n = int(sr * ms / 1000)
    t = np.arange(n, dtype=np.float32) / sr
    samples = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return (samples * 32767.0).astype("<i2").tobytes()


def _ingest(session: StreamSession, pcm: bytes) -> list[dict]:
    """Apply one PCM frame and run any VAD assess triggers (no periodic STT)."""
    out: list[dict] = []
    for ev in session.on_audio_chunk(pcm):
        if ev.get("type") == "_assess_trigger":
            out.extend(
                session.run_assess(
                    reason=ev.get("reason", "silence"),
                    recognized_hint=ev.get("recognized"),
                )
            )
        else:
            out.append(ev)
    return out


def _coverage_probe(session: StreamSession) -> list[dict]:
    """Periodic STT until coverage trigger or the stable-tick budget is spent."""
    ticks = max(1, int(settings.STREAM_COVERAGE_STABLE_TICKS)) + 1
    last: list[dict] = []
    for _ in range(ticks):
        session._last_periodic_at = 0.0
        session._last_probe_at = 0.0
        last = session.run_periodic_stt()
        if any(e.get("type") == "_assess_trigger" for e in last):
            return last
    return last


# --- Ring / VAD ----------------------------------------------------------


def test_pcm_ring_append_and_overlap():
    buf = PcmRingBuffer(sample_rate=16000, max_seconds=1.0)
    buf.append_pcm_s16le(_pcm_tone(500))
    assert buf.duration_ms == pytest.approx(500, abs=5)
    buf.clear(keep_overlap_ms=100)
    assert buf.duration_ms == pytest.approx(100, abs=5)


def test_vad_silence_triggers_after_speech():
    vad = EnergyVadSegmenter(
        sample_rate=16000,
        silence_ms=200,
        short_silence_ms=100,
        min_utterance_ms=100,
        rms_threshold=0.05,
    )
    speech = np.frombuffer(_pcm_tone(300, amp=0.3), dtype="<i2").astype(np.float32)
    speech = speech / 32768.0
    assert vad.feed(speech) is None
    silence = np.zeros(int(16000 * 0.15), dtype=np.float32)
    ready = vad.feed(silence)
    assert ready is not None
    assert ready.reason == "silence_short"


def test_vad_short_silence_before_long():
    vad = EnergyVadSegmenter(
        sample_rate=16000,
        silence_ms=500,
        short_silence_ms=150,
        min_utterance_ms=100,
        rms_threshold=0.05,
    )
    speech = np.frombuffer(_pcm_tone(300, amp=0.3), dtype="<i2").astype(np.float32) / 32768.0
    assert vad.feed(speech) is None
    short_pause = np.zeros(int(16000 * 0.2), dtype=np.float32)
    ready = vad.feed(short_pause)
    assert ready is not None
    assert ready.reason == "silence_short"
    # User resumes — short pause flag clears; next pause re-triggers short silence.
    assert vad.feed(speech) is None
    long_pause = np.zeros(int(16000 * 0.6), dtype=np.float32)
    ready = vad.feed(long_pause)
    assert ready is not None
    assert ready.reason == "silence_short"


def test_vad_continued_silence_reaches_long_after_short():
    """Short silence must not block the long-silence boundary on a continued pause."""
    vad = EnergyVadSegmenter(
        sample_rate=16000,
        silence_ms=400,
        short_silence_ms=150,
        min_utterance_ms=100,
        rms_threshold=0.05,
    )
    speech = np.frombuffer(_pcm_tone(300, amp=0.3), dtype="<i2").astype(np.float32)
    speech = speech / 32768.0
    assert vad.feed(speech) is None
    short_pause = np.zeros(int(16000 * 0.2), dtype=np.float32)
    ready = vad.feed(short_pause)
    assert ready is not None
    assert ready.reason == "silence_short"
    more_silence = np.zeros(int(16000 * 0.3), dtype=np.float32)
    ready = vad.feed(more_silence)
    assert ready is not None
    assert ready.reason == "silence"


# --- Session unit --------------------------------------------------------


def test_next_ayah_and_surah_end(quran_service: QuranService):
    assert quran_service.next_ayah(1, 1) == (1, 2)
    assert quran_service.next_ayah(1, 7) is None
    assert quran_service.next_ayah(1, 7, cross_surah=True) == (36, 1)


def test_session_pass_advance(quran_service: QuranService):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.5,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    ready = session.ready_event()
    assert ready["type"] == "session.ready"
    assert ready["current"]["ayah"] == 1

    session.buffer.append_pcm_s16le(_pcm_tone(600, amp=0.2))
    events = session.run_assess(reason="force")
    types = [e["type"] for e in events]
    assert "ayah.result" in types
    assert "session.advance" in types
    assert session.current_ayah == 2


def test_session_fail_continue_and_stop(quran_service: QuranService):
    recognizer = MockSpeechRecognizer(transcript="wrong words only")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.95,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert any(e["type"] == "ayah.result" and e["passed"] is False for e in events)
    assert any(e["type"] == "session.advance" and e["reason"] == "continue_policy" for e in events)

    cfg2 = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.95,
        fail_policy="stop",
        partials=False,
    )
    session2 = StreamSession(quran_service, recognizer, cfg2)
    session2.ready_event()
    session2.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events2 = session2.run_assess(reason="force")
    assert any(e["type"] == "session.summary" and e["reason"] == "fail_stop" for e in events2)


def test_long_silence_nonempty_low_coverage_emits_fail_result(
    quran_service: QuranService,
):
    """Long silence + wrong Heard below coverage → one ayah.result fail + waiting."""
    recognizer = MockSpeechRecognizer(transcript="الرحمن يضحين")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    results = [e for e in events if e["type"] == "ayah.result"]
    assert len(results) == 1
    result = results[0]
    assert result["passed"] is False
    assert result["warning"] is True
    assert result["trigger"] == "silence"
    assert result["coverage"] < settings.STREAM_COVERAGE_THRESHOLD
    assert session.current_ayah == 3
    assert session.attempt == 1
    assert any(e["type"] == "session.waiting" for e in events)
    assert session.buffer.duration_ms == pytest.approx(0, abs=1)


def test_long_silence_empty_heard_no_fail_result(quran_service: QuranService):
    """Quiet / empty Heard on long silence must abandon, not score a fail."""
    recognizer = MockSpeechRecognizer(transcript="")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.85,
        fail_policy="retry",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    assert not any(e["type"] == "ayah.result" for e in events)
    assert any(e["type"] == "session.listening" and e.get("cleared") for e in events)
    assert session.attempt == 0
    assert session.current_ayah == 1
    assert session.buffer.duration_ms == pytest.approx(0, abs=1)


def test_fail_result_includes_alignment_wrong_words(quran_service: QuranService):
    """Classic replace on long silence still ships highlight payload."""
    recognizer = MockSpeechRecognizer(transcript="الرحمن يضحين")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["passed"] is False
    assert result["alignment"]
    assert result["wrong_words"] or result["missing_words"]
    ops = {op["op"] for op in result["alignment"]}
    assert "replace" in ops or "delete" in ops


def test_session_silence_short_assesses_when_coverage_high(quran_service: QuranService):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.5,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence_short")
    assert any(e["type"] == "ayah.result" for e in events)
    assert any(e["type"] == "session.advance" for e in events)


def test_session_completion_probe_triggers_assess(quran_service: QuranService):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.5,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    probe = _coverage_probe(session)
    assert len(probe) == 1
    assert probe[0]["reason"] == "coverage"
    events = session.run_assess(
        reason="coverage",
        recognized_hint=probe[0]["recognized"],
    )
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["trigger"] == "coverage"
    assert "coverage" in result
    assert result["coverage"] >= 0.85


# STT-like simple Arabic (not Uthmani round-trip) — ayah-advance-fix-spec §14
FATIHAH_STT = {
    1: "بسم الله الرحمن الرحيم",
    2: "الحمد لله رب العالمين",
    3: "الرحمن الرحيم",
    4: "مالك يوم الدين",
    5: "اياك نعبد واياك نستعين",
    6: "اهدنا الصراط المستقيم",
    7: "صراط الذين انعمت عليهم غير المغضوب عليهم ولا الضالين",
}


def test_session_stt_like_1_2_coverage_probe_advances(quran_service: QuranService):
    """S1: Moonshine-like 1:2 must finalize via coverage and advance to 1:3."""
    recognizer = MockSpeechRecognizer(transcript=FATIHAH_STT[2])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=2,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    probe = _coverage_probe(session)
    assert any(e.get("type") == "_assess_trigger" for e in probe)
    trigger = next(e for e in probe if e.get("type") == "_assess_trigger")
    events = session.run_assess(
        reason="coverage",
        recognized_hint=trigger["recognized"],
    )
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["passed"] is True
    assert result["coverage"] >= 0.85
    assert any(e["type"] == "session.advance" for e in events)
    assert session.current_ayah == 3


def test_session_stt_like_1_2_silence_advances(quran_service: QuranService):
    """S2: silence / silence_short must not return [] on correct STT-like 1:2."""
    recognizer = MockSpeechRecognizer(transcript=FATIHAH_STT[2])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=2,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    for reason in ("silence", "silence_short"):
        session = StreamSession(quran_service, recognizer, cfg)
        session.ready_event()
        session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
        events = session.run_assess(reason=reason)
        assert events, f"{reason} must not return []"
        assert any(e["type"] == "ayah.result" and e["passed"] for e in events)
        assert any(e["type"] == "session.advance" for e in events)
        assert session.current_ayah == 3


def test_session_stt_like_1_2_mid_ayah_blocked(quran_service: QuranService):
    """S3: mid-ayah STT must not probe-finalize; short silence must not fail."""
    recognizer = MockSpeechRecognizer(transcript="الحمد لله")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=2,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_probe_at = 0.0
    probe = session.run_periodic_stt()
    assert not any(e.get("type") == "_assess_trigger" for e in probe)

    session.buffer.append_pcm_s16le(_pcm_tone(200, amp=0.2))
    short = session.run_assess(reason="silence_short")
    assert not any(e["type"] == "ayah.result" for e in short)
    assert session.current_ayah == 2
    assert session.attempt == 0
    assert session.buffer.duration_ms == pytest.approx(700, abs=20)


def test_session_fatihah_stt_like_auto_advances_through_range(
    quran_service: QuranService,
):
    """S4: scripted Fatihah 1→7 with STT-like lines, no force_assess."""
    lines = [FATIHAH_STT[i] for i in range(1, 8)]
    recognizer = MockSpeechRecognizer(transcripts=lines)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    summary = None
    for _ in range(7):
        session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
        events = session.run_assess(reason="silence")
        assert any(e["type"] == "ayah.result" and e["passed"] for e in events)
        for e in events:
            if e["type"] == "session.summary":
                summary = e
    assert summary is not None
    assert summary["reason"] == "range_complete"
    assert summary["ayahs_passed"] == 7


def test_session_unified_periodic_single_stt(quran_service: QuranService):
    """A4: partials + probe share one STT pass."""
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.5,
        fail_policy="continue",
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    assert session.uses_unified_periodic()
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    types = [e["type"] for e in events]
    assert "partial.transcript" in types
    assert "partial.alignment" in types
    assert recognizer.calls == 1
    alignment = next(e for e in events if e["type"] == "partial.alignment")
    assert alignment["progress"] >= 0.85
    probe = _coverage_probe(session)
    assert any(e.get("type") == "_assess_trigger" for e in probe)
    # Unchanged ring snapshot reuses the last decode (no second Whisper call).
    assert recognizer.calls == 1


def test_unified_periodic_caps_inference_window(
    quran_service: QuranService, monkeypatch
):
    """Unified periodic STT must not re-decode the full growing ring buffer."""
    monkeypatch.setattr(settings, "STREAM_PARTIAL_WINDOW_MS", 3000)
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.5,
        fail_policy="continue",
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(8000, amp=0.2))
    session._last_periodic_at = 0.0
    session.run_periodic_stt()
    assert recognizer.calls == 1
    # 3s @ 16 kHz = 48000 samples (allow small rounding).
    assert recognizer.last_audio_samples == pytest.approx(48000, abs=160)
    assert session.buffer.duration_ms == pytest.approx(8000, abs=20)


def test_silence_short_reuses_fresh_last_stt(quran_service: QuranService):
    """Breath pauses must not pay a second full-buffer Whisper."""
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    partial = " ".join(ayah1.split()[:2])
    recognizer = MockSpeechRecognizer(transcript=partial)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.5,
        fail_policy="continue",
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(1500, amp=0.2))
    session._last_periodic_at = 0.0
    session.run_periodic_stt()
    assert recognizer.calls == 1
    # One 250ms chunk of trailing silence/overlap — still reuse.
    session.on_audio_chunk(_pcm_tone(200, amp=0.05))
    events = session.run_assess(reason="silence_short")
    assert recognizer.calls == 1  # reused last periodic decode
    assert any(e.get("type") == "session.listening" for e in events)


def test_silence_redecodes_when_buffer_grew_with_speech(
    quran_service: QuranService,
):
    """New speech during STT must not finalize from a stale partial."""
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(
        transcripts=[ayah1.split()[0], ayah1],
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.5,
        fail_policy="continue",
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(2000, amp=0.2))
    session._last_periodic_at = 0.0
    session.run_periodic_stt()
    assert recognizer.calls == 1
    # Simulate more tilawah arriving while the previous decode was busy.
    session.on_audio_chunk(_pcm_tone(2500, amp=0.2))
    assert session.buffer.duration_ms > session._last_stt_buffer_ms + 250
    events = session.run_assess(reason="silence")
    assert recognizer.calls == 2
    assert any(e.get("type") == "ayah.result" for e in events)


def test_session_partial_progress_uses_suffix_coverage(
    quran_service: QuranService, monkeypatch
):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    partial = " ".join(ayah1.split()[:2])
    recognizer = MockSpeechRecognizer(transcript=partial)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    monkeypatch.setattr(
        "app.services.stream_session.settings.STREAM_COMPLETION_PROBE",
        False,
    )
    session._last_partial_at = 0.0
    events = session.run_periodic_stt()
    alignment = next(e for e in events if e["type"] == "partial.alignment")
    from app.services.assessor import MemorizationAssessor

    expected_progress = MemorizationAssessor().progress(ayah1, partial)
    assert alignment["progress"] == pytest.approx(round(expected_progress, 3))


def test_session_summary_includes_busy_and_stt_stats(quran_service: QuranService):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(start_surah=1, start_ayah=1, partials=False)
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.error_event("busy", "test busy", fatal=False)
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.run_assess(reason="force")
    summary = session.summary_event("user_stop")
    assert summary["busy_errors"] == 1
    assert summary["stt_ms_total"] >= 0
    assert summary["wall_ms"] >= 0


def test_benchmark_ttfr_coverage_probe(quran_service: QuranService, monkeypatch):
    """§7.1 scripted regression: TTFR via completion probe without silence wait."""
    monkeypatch.setattr(
        "app.services.stream_session.settings.STREAM_COMPLETION_PROBE", True
    )
    monkeypatch.setattr(
        "app.services.stream_session.settings.STREAM_COMPLETION_PROBE_MS", 100
    )
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=1,
        threshold=0.5,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()

    # Simulate 250 ms PCM chunks (speech then trailing silence).
    chunk_ms = 250
    speech_ms = 800
    silence_ms = 600
    for _ in range(speech_ms // chunk_ms):
        session.on_audio_chunk(_pcm_tone(chunk_ms, amp=0.25))
    last_speech_at = session.buffer.duration_ms
    for _ in range(silence_ms // chunk_ms):
        session.on_audio_chunk(_pcm_silence(chunk_ms))

    session._last_periodic_at = 0.0
    session._last_probe_at = 0.0
    probe = _coverage_probe(session)
    trigger = next(e for e in probe if e.get("type") == "_assess_trigger")
    assert trigger["reason"] == "coverage"
    events = session.run_assess(
        reason="coverage",
        recognized_hint=trigger["recognized"],
    )
    result = next(e for e in events if e["type"] == "ayah.result")
    ttfr_ms = session.buffer.duration_ms - last_speech_at
    assert result["passed"] is True
    assert result["trigger"] == "coverage"
    # Probe fires before long silence VAD — TTFR well under 2500 ms gate.
    assert ttfr_ms < 2500


def test_short_silence_low_coverage_no_fail_result(quran_service: QuranService):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    partial = " ".join(ayah1.split()[:2])
    recognizer = MockSpeechRecognizer(transcript=partial)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.85,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence_short")
    assert not any(e["type"] == "ayah.result" for e in events)
    assert any(e["type"] == "session.listening" and not e.get("cleared") for e in events)
    assert session.current_ayah == 1
    assert session.attempt == 0
    # Breaths keep audio; only long silence drops the attempt.
    assert session.buffer.duration_ms == pytest.approx(500, abs=20)


def test_session_long_silence_clears_buffer_so_retry_is_clean(
    quran_service: QuranService,
):
    """After an incomplete 1:3, long silence must not glue the next take onto it."""
    recognizer = MockSpeechRecognizer(
        transcripts=["الرحمن يضحين", FATIHAH_STT[3]],
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    first = session.run_assess(reason="silence")
    result = next(e for e in first if e["type"] == "ayah.result")
    assert result["passed"] is False
    assert any(e["type"] == "session.waiting" for e in first)
    assert session.buffer.duration_ms == pytest.approx(0, abs=1)
    assert session._last_stt is None

    session.buffer.append_pcm_s16le(_pcm_tone(600, amp=0.2))
    second = session.run_assess(reason="silence")
    result = next(e for e in second if e["type"] == "ayah.result")
    assert result["passed"] is True
    assert session.current_ayah == 4


def test_session_silence_assesses_when_coverage_high(quran_service: QuranService):
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah1)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.5,
        fail_policy="continue",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    assert any(e["type"] == "ayah.result" for e in events)
    assert any(e["type"] == "session.advance" for e in events)


def test_session_range_complete(quran_service: QuranService):
    ayah = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=1,
        threshold=0.5,
        fail_policy="continue",
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert any(e["type"] == "session.summary" and e["reason"] == "range_complete" for e in events)


def test_cross_surah_open_advances_to_next_surah(quran_service: QuranService):
    """S1: open range + cross_surah on last ayah of surah 1 → next surah:1."""
    ayah = quran_service.get_ayah(1, 7)["text"]
    expected_next = quran_service.next_ayah(1, 7, cross_surah=True)
    assert expected_next is not None
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=7,
        end_surah=None,
        end_ayah=None,
        threshold=0.5,
        fail_policy="retry",
        cross_surah=True,
        partials=False,
        auto_advance=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert not any(e["type"] == "session.summary" for e in events)
    advance = next(e for e in events if e["type"] == "session.advance")
    assert advance["from"] == {"surah": 1, "ayah": 7}
    assert advance["to"]["surah"] == expected_next[0]
    assert advance["to"]["ayah"] == expected_next[1]
    assert session.current_surah == expected_next[0]
    assert session.current_ayah == expected_next[1]


def test_cross_surah_closed_end_still_range_complete(quran_service: QuranService):
    """S2: closed end at last ayah of start surah → range_complete."""
    ayah = quran_service.get_ayah(1, 7)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=7,
        end_surah=1,
        end_ayah=7,
        threshold=0.5,
        fail_policy="retry",
        cross_surah=True,
        partials=False,
        auto_advance=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert any(
        e["type"] == "session.summary" and e["reason"] == "range_complete"
        for e in events
    )
    assert not any(e["type"] == "session.advance" for e in events)


def test_cross_surah_false_surah_complete(quran_service: QuranService):
    """S3: cross_surah=false, no end, last ayah → surah_complete."""
    ayah = quran_service.get_ayah(1, 7)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=7,
        threshold=0.5,
        fail_policy="retry",
        cross_surah=False,
        partials=False,
        auto_advance=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert any(
        e["type"] == "session.summary" and e["reason"] == "surah_complete"
        for e in events
    )


def test_cross_surah_multi_surah_closed_range(quran_service: QuranService):
    """S4: closed 1:7→36:1 with cross_surah; pass 1:7 advances, pass 36:1 ends."""
    next_ref = quran_service.next_ayah(1, 7, cross_surah=True)
    assert next_ref == (36, 1)
    ayah7 = quran_service.get_ayah(1, 7)["text"]
    ayah_next = quran_service.get_ayah(36, 1)["text"]
    recognizer = MockSpeechRecognizer(transcripts=[ayah7, ayah_next])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=7,
        end_surah=36,
        end_ayah=1,
        threshold=0.5,
        fail_policy="retry",
        cross_surah=True,
        partials=False,
        auto_advance=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert any(e["type"] == "session.advance" for e in events)
    assert session.current_surah == 36 and session.current_ayah == 1
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events2 = session.run_assess(reason="force")
    assert any(
        e["type"] == "session.summary" and e["reason"] == "range_complete"
        for e in events2
    )


def test_cross_surah_corpus_end_quran_complete(quran_service: QuranService):
    """S5: last ayah of last fixture surah + open + cross_surah → quran_complete."""
    last_surah = max(s["number"] for s in quran_service.data)
    last_ayah = quran_service.last_ayah_number(last_surah)
    assert quran_service.next_ayah(last_surah, last_ayah, cross_surah=True) is None
    ayah = quran_service.get_ayah(last_surah, last_ayah)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=last_surah,
        start_ayah=last_ayah,
        threshold=0.5,
        fail_policy="retry",
        cross_surah=True,
        partials=False,
        auto_advance=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert any(
        e["type"] == "session.summary" and e["reason"] == "quran_complete"
        for e in events
    )


def test_validate_rejects_bad_threshold(quran_service: QuranService):
    recognizer = MockSpeechRecognizer()
    session, err = StreamSession.validate_and_build(
        quran_service,
        recognizer,
        {"start_surah": 1, "start_ayah": 1, "threshold": 0.1},
    )
    assert session is None
    assert err["code"] == "invalid_config"


# --- WebSocket integration -----------------------------------------------


@pytest.fixture
def ws_client(quran_service: QuranService) -> TestClient:
    ayah1 = quran_service.get_ayah(1, 1)["text"]
    ayah2 = quran_service.get_ayah(1, 2)["text"]
    # Extra ayah1 copies: audio may arm a completion probe STT before force_assess.
    recognizer = MockSpeechRecognizer(transcripts=[ayah1, ayah1, ayah1, ayah2])
    app = FastAPI()
    app.include_router(create_stream_router(quran_service, recognizer=recognizer))
    return TestClient(app)


def test_ws_start_force_assess_advance_stop(ws_client: TestClient):
    with ws_client.websocket_connect("/api/memorization/stream") as ws:
        ws.send_json(
            {
                "type": "session.start",
                "start_surah": 1,
                "start_ayah": 1,
                "end_surah": 1,
                "end_ayah": 3,
                "threshold": 0.5,
                "fail_policy": "continue",
                "partials": False,
                "auto_advance": True,
                "audio": {
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "chunk_ms": 250,
                },
            }
        )
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        assert ready["current"]["ayah"] == 1

        ws.send_bytes(_pcm_tone(500, amp=0.25))
        ws.send_json({"type": "ayah.force_assess"})

        result = ws.receive_json()
        assert result["type"] == "ayah.result"
        assert result["passed"] is True

        advance = ws.receive_json()
        assert advance["type"] == "session.advance"
        assert advance["to"]["ayah"] == 2

        ws.send_json({"type": "session.stop", "reason": "user"})
        summary = ws.receive_json()
        assert summary["type"] == "session.summary"
        assert summary["reason"] == "user_stop"
        assert summary["ayahs_passed"] >= 1


def test_ws_unified_partials_emit_alignment(ws_client: TestClient):
    """partials:true + probe → one STT path emits partial.* then coverage assess."""
    with ws_client.websocket_connect("/api/memorization/stream") as ws:
        ws.send_json(
            {
                "type": "session.start",
                "start_surah": 1,
                "start_ayah": 1,
                "end_surah": 1,
                "end_ayah": 3,
                "threshold": 0.5,
                "fail_policy": "continue",
                "partials": True,
                "auto_advance": True,
                "audio": {
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "chunk_ms": 250,
                },
            }
        )
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        assert ready["config"]["partials"] is True

        # Enough audio + time for periodic tick: send chunks then force assess
        # if probe hasn't fired yet (TestClient is sync; force still validates path).
        ws.send_bytes(_pcm_tone(600, amp=0.25))
        ws.send_json({"type": "ayah.force_assess"})

        types = []
        for _ in range(6):
            msg = ws.receive_json()
            types.append(msg["type"])
            if msg["type"] == "session.advance":
                break
            if msg["type"] == "ayah.result" and not msg.get("will_advance"):
                break

        assert "ayah.result" in types
        # Partials may arrive before force_assess if periodic ran; either way OK.
        assert any(t in {"ayah.result", "partial.transcript", "partial.alignment"} for t in types)


def test_ws_rejects_audio_before_start(ws_client: TestClient):
    with ws_client.websocket_connect("/api/memorization/stream") as ws:
        ws.send_bytes(_pcm_tone(100))
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "not_ready"


def test_ws_ping_pong(ws_client: TestClient):
    with ws_client.websocket_connect("/api/memorization/stream") as ws:
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong"


# --- STT confidence filter (stt-confidence-filter-spec §10.2) -----------

SCREENSHOT_HEARD = "يا كلب دعياة لست عينا يا كلب دعيا كلا استعين"
SCREENSHOT_CONFS = [0.15, 0.12, 0.20, 0.18, 0.22, 0.14, 0.11, 0.19, 0.16, 0.88]


def _screenshot_session(quran_service: QuranService, **recog_kw):
    recognizer = MockSpeechRecognizer(
        transcript=SCREENSHOT_HEARD,
        word_confidences=SCREENSHOT_CONFS,
        sequence_confidence=0.70,
        **recog_kw,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=5,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    return session, recognizer


def test_sh1_partial_heard_drops_low_confidence_garbage(quran_service: QuranService):
    session, _ = _screenshot_session(quran_service)
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    transcripts = [e for e in events if e.get("type") == "partial.transcript"]
    for event in transcripts:
        heard = event["recognized"]
        assert "كلب" not in heard
        assert "يا" not in heard.split()
    if transcripts:
        assert "استعين" in transcripts[0]["recognized"]


def test_sh2_partial_progress_not_against_garbage_dump(quran_service: QuranService):
    session, _ = _screenshot_session(quran_service)
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    alignments = [e for e in events if e.get("type") == "partial.alignment"]
    if alignments:
        assert alignments[0]["progress"] <= 0.25
        insert_ops = [
            op for op in alignments[0]["alignment"] if op.get("op") == "insert"
        ]
        assert len(insert_ops) < 11


def test_sh3_no_speech_omits_partial_transcript(quran_service: QuranService):
    recognizer = MockSpeechRecognizer(
        transcript="",
        skipped_reason="no_speech",
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=5,
        threshold=0.85,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    assert not any(e.get("type") == "partial.transcript" for e in events)
    assert recognizer.calls == 1


def test_sh4_confident_1_2_still_advances(quran_service: QuranService):
    """S-H4 / G5: mock 1:2 at conf 1.0 still coverage-finalizes to 1:3."""
    tokens = FATIHAH_STT[2].split()
    recognizer = MockSpeechRecognizer(
        transcript=FATIHAH_STT[2],
        word_confidences=[1.0] * len(tokens),
        sequence_confidence=1.0,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=2,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    probe = _coverage_probe(session)
    trigger = next(e for e in probe if e.get("type") == "_assess_trigger")
    events = session.run_assess(
        reason="coverage",
        recognized_hint=trigger["recognized"],
    )
    assert any(e["type"] == "ayah.result" and e["passed"] for e in events)
    assert any(e["type"] == "session.advance" for e in events)
    assert session.current_ayah == 3


def test_sh5_high_conf_wrong_ayah_still_shown(quran_service: QuranService):
    wrong = FATIHAH_STT[4]
    recognizer = MockSpeechRecognizer(
        transcript=wrong,
        word_confidences=[0.95] * len(wrong.split()),
        sequence_confidence=0.95,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=5,
        threshold=0.85,
        fail_policy="retry",
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    transcript = next(e for e in events if e["type"] == "partial.transcript")
    assert transcript["recognized"] == wrong
    session.buffer.append_pcm_s16le(_pcm_tone(200, amp=0.2))
    final = session.run_assess(reason="force", recognized_hint=wrong)
    result = next(e for e in final if e["type"] == "ayah.result")
    assert result["recognized"] == wrong
    assert result["passed"] is False


def test_sh6_energy_gate_skips_stt_on_silent_buffer(quran_service: QuranService):
    recognizer = MockSpeechRecognizer(transcript=SCREENSHOT_HEARD)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=5,
        threshold=0.85,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_silence(500))
    session._last_periodic_at = 0.0
    assert recognizer.calls == 0
    events = session.run_periodic_stt()
    assert events == []
    assert recognizer.calls == 0


def test_sh7_slider_0_90_drops_word_at_0_88(quran_service: QuranService):
    """Garbage stays dropped at T=0.90; in-vocab استعين may revive (≥ invocab floor)."""
    recognizer = MockSpeechRecognizer(
        transcript=SCREENSHOT_HEARD,
        word_confidences=SCREENSHOT_CONFS,
        sequence_confidence=0.70,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=5,
        threshold=0.90,
        fail_policy="retry",
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    transcripts = [e for e in events if e.get("type") == "partial.transcript"]
    if transcripts:
        heard = transcripts[0]["recognized"]
        # In-vocab near-miss استعين≈نستعين may be revived (≥ STT_INVOCAB_FLOOR);
        # hallucination tokens stay dropped.
        assert "كلب" not in heard
        assert "يا" not in heard.split()
    else:
        assert transcripts == []


def test_force_empty_filtered_is_no_speech_not_zero_score(
    quran_service: QuranService,
):
    """Empty STT / quiet buffer must not paint Score 0% memorization fail."""
    recognizer = MockSpeechRecognizer(transcript="")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.85,
        fail_policy="retry",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="force")
    assert not any(e.get("type") == "ayah.result" for e in events)
    assert any(e.get("type") == "error" and e.get("code") == "no_speech" for e in events)
    assert any(e.get("type") == "session.listening" for e in events)
    assert session.state == SessionState.LISTENING


def test_force_quiet_pcm_still_runs_stt(quran_service: QuranService):
    """Check now must STT even when RMS is below the VAD speech floor."""
    recognizer = MockSpeechRecognizer(transcript=FATIHAH_STT[1])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=1,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    # amp 0.02 → RMS ~0.014, under STREAM_VAD_RMS_THRESHOLD (0.015)
    session.buffer.append_pcm_s16le(_pcm_tone(600, amp=0.02))
    events = session.run_assess(reason="force")
    assert recognizer.calls == 1
    assert any(e.get("type") == "ayah.result" and e.get("passed") for e in events)


def test_periodic_stt_accepts_quiet_speech(quran_service: QuranService):
    """Periodic path uses STREAM_STT_RMS_THRESHOLD, not the stricter VAD floor."""
    recognizer = MockSpeechRecognizer(transcript=FATIHAH_STT[1])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.85,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(600, amp=0.02))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    assert recognizer.calls == 1
    assert any(e.get("type") == "partial.transcript" for e in events)


# --- Uthmani ↔ Tanzeel / STT recovery (uthmani-tanzeel-word-matching-spec §9.2)


def test_s1_revived_bism_coverage_advances(quran_service: QuranService):
    recognizer = MockSpeechRecognizer(
        transcript="بسم الله الرحمن الرحيم",
        word_confidences=[0.62, 0.9, 0.9, 0.9],
        sequence_confidence=0.83,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    transcript = next(e for e in events if e["type"] == "partial.transcript")
    assert transcript["recognized"].split()[0] == "بسم"
    assert transcript["raw_recognized"] == "الله الرحمن الرحيم"
    assert transcript["recovery"]["revived"] == ["بسم"]
    kept = {word["text"]: word["kept"] for word in transcript["words"]}
    assert kept["بسم"] is True
    alignment = next(e for e in events if e["type"] == "partial.alignment")
    assert alignment["progress"] >= 0.85
    probe = _coverage_probe(session)
    trigger = next(e for e in probe if e.get("type") == "_assess_trigger")
    final = session.run_assess(
        reason="coverage",
        recognized_hint=trigger["recognized"],
    )
    result = next(e for e in final if e["type"] == "ayah.result")
    assert result["recognized"].split()[0] == "بسم"
    assert result["coverage"] >= 0.85
    assert result["passed"] is True
    assert any(e["type"] == "session.advance" for e in final)
    assert session.current_ayah == 2


def test_s2_screenshot_heard_does_not_invent_or_advance(quran_service: QuranService):
    recognizer = MockSpeechRecognizer(
        transcript="الله الرحمن الرحيم",
        word_confidences=[0.9, 0.9, 0.9],
        sequence_confidence=0.9,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    transcript = next(e for e in events if e["type"] == "partial.transcript")
    assert "بسم" not in transcript["recognized"].split()
    alignment = next(e for e in events if e["type"] == "partial.alignment")
    # Contiguous credit: skipping بسم keeps cumulative progress at 0.
    assert alignment["progress"] == pytest.approx(0.0)
    assert alignment.get("window_coverage", 0.0) == pytest.approx(0.75)
    assert not any(e.get("type") == "_assess_trigger" for e in events)
    session.buffer.append_pcm_s16le(_pcm_tone(200, amp=0.2))
    silence_events = session.run_assess(reason="silence")
    assert not any(e["type"] == "session.advance" for e in silence_events)
    result = next(e for e in silence_events if e["type"] == "ayah.result")
    assert result["passed"] is False
    assert "بسم" not in result["recognized"].split()
    assert session.current_ayah == 1
    assert session.buffer.duration_ms == pytest.approx(0, abs=1)


def test_s3_agglutinated_bismillah_advances_without_force(
    quran_service: QuranService,
):
    recognizer = MockSpeechRecognizer(
        transcript="بسمالله الرحمن الرحيم",
        word_confidences=[0.9, 0.9, 0.9],
        sequence_confidence=0.9,
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(500, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    transcript = next(e for e in events if e["type"] == "partial.transcript")
    assert transcript["recognized"].split() == ["بسم", "الله", "الرحمن", "الرحيم"]
    probe = _coverage_probe(session)
    trigger = next(e for e in probe if e.get("type") == "_assess_trigger")
    final = session.run_assess(
        reason="coverage",
        recognized_hint=trigger["recognized"],
    )
    result = next(e for e in final if e["type"] == "ayah.result")
    equals = [op for op in result["alignment"] if op["op"] == "equal"]
    assert len(equals) == 4
    assert any(e["type"] == "session.advance" for e in final)
    assert session.current_ayah == 2


# --- Continuous vs Single detection gap (continuous-vs-single-detection-spec §9)


def test_periodic_incomplete_1_3_does_not_finalize(quran_service: QuranService):
    """Periodic STT returning الرحمن يضحين (coverage 0.5) must not emit ayah.result."""
    recognizer = MockSpeechRecognizer(transcript="الرحمن يضحين")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(600, amp=0.2))
    session._last_periodic_at = 0.0
    events = session.run_periodic_stt()
    assert not any(e.get("type") == "_assess_trigger" for e in events)
    assert not any(e.get("type") == "ayah.result" for e in events)
    alignment = next(e for e in events if e["type"] == "partial.alignment")
    assert alignment["progress"] == pytest.approx(0.5)
    assert alignment["provisional"] is True
    assert session.current_ayah == 3


def test_complete_window_after_bad_partial_passes(quran_service: QuranService):
    """Later force on the same session with الرحمن الرحيم must pass and advance."""
    recognizer = MockSpeechRecognizer(
        transcripts=["الرحمن يضحين", FATIHAH_STT[3]],
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(600, amp=0.2))
    session._last_periodic_at = 0.0
    first = session.run_periodic_stt()
    assert not any(e.get("type") == "_assess_trigger" for e in first)
    session.buffer.append_pcm_s16le(_pcm_tone(600, amp=0.2))
    events = session.run_assess(reason="force")
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["passed"] is True
    assert any(e["type"] == "session.advance" for e in events)
    assert session.current_ayah == 4


def test_stt_busy_queues_next_tick(quran_service: QuranService):
    """H2: a busy flag must not drop a later complete snapshot."""
    recognizer = MockSpeechRecognizer(transcript=FATIHAH_STT[3])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.on_audio_chunk(_pcm_tone(600, amp=0.2))
    session.stt_busy = True
    assert session.should_run_periodic_stt() is False
    assert session._stt_queued is True
    session.stt_busy = False
    session._last_periodic_at = 10**9
    session._last_probe_at = 10**9
    assert session.should_run_periodic_stt() is True
    probe = _coverage_probe(session)
    trigger = next(e for e in probe if e.get("type") == "_assess_trigger")
    events = session.run_assess(
        reason="coverage",
        recognized_hint=trigger["recognized"],
    )
    assert any(e["type"] == "ayah.result" and e["passed"] for e in events)
    assert session.current_ayah == 4


def test_pcm_ingested_while_assessing(quran_service: QuranService):
    """H2: PCM that arrives during ayah-final STT must still enter the ring buffer."""
    recognizer = MockSpeechRecognizer(transcript=FATIHAH_STT[3])
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.state = SessionState.ASSESSING
    session.stt_busy = True
    before = session.buffer.duration_ms
    events = session.on_audio_chunk(_pcm_tone(250, amp=0.2))
    assert events == []
    assert session.buffer.duration_ms > before
    assert session._stt_queued is True


def test_overlap_only_after_fail_does_not_stt(quran_service: QuranService):
    """After ayah.result fail + retry, leftover overlap must not STT as 1:3."""
    recognizer = MockSpeechRecognizer(
        transcripts=["wrong tokens here", FATIHAH_STT[3]],
    )
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    failed = session.run_assess(reason="force")
    assert any(e["type"] == "ayah.result" and e["passed"] is False for e in failed)
    assert session.current_ayah == 3
    assert session.buffer.duration_ms == pytest.approx(0, abs=1)
    session._last_periodic_at = 0.0
    assert session.run_periodic_stt() == []
    assert recognizer.calls == 1


def test_incomplete_1_3_pause_emits_fail_result_not_stall(
    quran_service: QuranService,
):
    """After ≥800 ms pause on a 50% 1:3, ayah.result fail + waiting, not a silent stall."""
    recognizer = MockSpeechRecognizer(transcript="الرحمن يضحين")
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=3,
        end_surah=1,
        end_ayah=7,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=True,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    events = _ingest(session, _pcm_tone(600, amp=0.2))
    for _ in range(8):
        events.extend(_ingest(session, _pcm_silence(250)))
        if any(e.get("type") == "ayah.result" for e in events):
            break
    assert any(e["type"] == "ayah.result" and e["passed"] is False for e in events)
    assert any(e["type"] == "session.waiting" for e in events)
    assert session.current_ayah == 3
    assert session.buffer.duration_ms == pytest.approx(0, abs=1)


# --- Multi-utterance credit (specs/multi-utterance-credit-spec.md) --------


def test_s1_two_utterances_prefix_then_suffix_pass_advance(
    quran_service: QuranService,
):
    """S1: prefix then suffix → one passed ayah.result + session.advance."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    from app.services.normalizer import tokenize

    tokens = tokenize(ayah)
    prefix = " ".join(tokens[:2])
    suffix = " ".join(tokens[2:])
    recognizer = MockSpeechRecognizer(transcript=prefix)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=3,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    first = session.run_assess(reason="silence")
    assert not any(e["type"] == "ayah.result" for e in first)
    assert session._credit_cursor() == 2
    assert any(e["type"] == "session.listening" for e in first)

    recognizer.transcript = suffix
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    second = session.run_assess(reason="silence")
    result = next(e for e in second if e["type"] == "ayah.result")
    assert result["passed"] is True
    assert result["credit_complete"] is True
    assert result["credit_utterances"] == 2
    assert result["coverage"] == pytest.approx(1.0)
    assert any(e["type"] == "session.advance" for e in second)
    assert session.current_ayah == 2


def test_s2_multi_utterance_auto_advance_next_ayah(quran_service: QuranService):
    """S2: after credit-complete pass, next ayah payload is correct."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    from app.services.normalizer import tokenize

    tokens = tokenize(ayah)
    recognizer = MockSpeechRecognizer(transcript=" ".join(tokens[:2]))
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=3,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.run_assess(reason="silence")
    recognizer.transcript = " ".join(tokens[2:])
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    advance = next(e for e in events if e["type"] == "session.advance")
    assert advance["to"]["surah"] == 1
    assert advance["to"]["ayah"] == 2
    assert advance["from"]["ayah"] == 1


def test_s3_prefix_long_silence_empty_retains_credit(quran_service: QuranService):
    """S3: prefix credited, then empty long silence → no fail; credit kept."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    from app.services.normalizer import tokenize

    tokens = tokenize(ayah)
    recognizer = MockSpeechRecognizer(transcript=" ".join(tokens[:2]))
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.85,
        fail_policy="retry",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.run_assess(reason="silence")
    assert session._credit_cursor() == 2

    recognizer.transcript = ""
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    assert not any(e["type"] == "ayah.result" for e in events)
    assert session._credit_cursor() == 2
    listening = next(e for e in events if e["type"] == "session.listening")
    assert listening["cleared"] is True
    assert listening["credit_cursor"] == 2


def test_s4_wrong_continuation_fails_keeps_credit_by_default(
    quran_service: QuranService,
):
    """S4: wrong Heard at cursor → fail + tone path; credit kept (default)."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    from app.services.normalizer import tokenize

    tokens = tokenize(ayah)
    recognizer = MockSpeechRecognizer(transcript=" ".join(tokens[:2]))
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        threshold=0.85,
        fail_policy="retry",
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.run_assess(reason="silence")
    assert session._credit_cursor() == 2

    recognizer.transcript = "باطل خطأ"
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["passed"] is False
    assert result["credit_cursor"] == 2
    assert session._credit_cursor() == 2
    assert any(e["type"] == "session.waiting" for e in events)


def test_s5_single_utterance_still_pass_advances(quran_service: QuranService):
    """S5: full ayah in one window still pass-advances."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["passed"] is True
    assert result.get("credit_complete") is True
    assert result.get("credit_utterances") == 1
    assert any(e["type"] == "session.advance" for e in events)


def test_s6_cumulative_complete_despite_low_window_coverage(
    quran_service: QuranService,
):
    """S6: suffix window alone is low coverage; cumulative complete → pass."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    from app.services.normalizer import tokenize
    from app.services.assessor import MemorizationAssessor

    tokens = tokenize(ayah)
    suffix = " ".join(tokens[2:])
    window = MemorizationAssessor().progress(ayah, suffix)
    assert window < settings.STREAM_COVERAGE_THRESHOLD

    recognizer = MockSpeechRecognizer(transcript=" ".join(tokens[:2]))
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.run_assess(reason="silence")
    recognizer.transcript = suffix
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    events = session.run_assess(reason="silence")
    result = next(e for e in events if e["type"] == "ayah.result")
    assert result["passed"] is True
    assert result["window_coverage"] < settings.STREAM_COVERAGE_THRESHOLD
    assert result["coverage"] == pytest.approx(1.0)


def test_s7_advance_resets_credit(quran_service: QuranService):
    """S7: after advance, new ayah credit cursor is 0."""
    ayah = quran_service.get_ayah(1, 1)["text"]
    recognizer = MockSpeechRecognizer(transcript=ayah)
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=3,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    session.run_assess(reason="silence")
    assert session.current_ayah == 2
    assert session._credit_cursor() == 0
    assert session._credit_utterances == 0


def test_s8_credit_disabled_legacy_window_only(
    quran_service: QuranService, monkeypatch
):
    """S8: STREAM_MULTI_UTTERANCE_CREDIT=false → suffix alone does not pass."""
    monkeypatch.setattr(settings, "STREAM_MULTI_UTTERANCE_CREDIT", False)
    ayah = quran_service.get_ayah(1, 1)["text"]
    from app.services.normalizer import tokenize

    tokens = tokenize(ayah)
    recognizer = MockSpeechRecognizer(transcript=" ".join(tokens[:2]))
    cfg = StreamSessionConfig(
        start_surah=1,
        start_ayah=1,
        end_surah=1,
        end_ayah=2,
        threshold=0.85,
        fail_policy="retry",
        auto_advance=True,
        partials=False,
    )
    session = StreamSession(quran_service, recognizer, cfg)
    session.ready_event()
    assert session._credit_mask == []
    session.buffer.append_pcm_s16le(_pcm_tone(500, amp=0.2))
    first = session.run_assess(reason="silence")
    # Legacy: incomplete non-empty → fail result (not credit retain).
    assert any(e["type"] == "ayah.result" and e["passed"] is False for e in first)
