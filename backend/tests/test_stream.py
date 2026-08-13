"""Tests for realtime WebSocket stream session."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.memorization_stream import create_router as create_stream_router
from app.services.quran_service import QuranService
from app.services.speech_service import MockSpeechRecognizer
from app.services.stream_audio import EnergyVadSegmenter, PcmRingBuffer
from app.services.stream_session import StreamSession, StreamSessionConfig

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
        min_utterance_ms=100,
        rms_threshold=0.05,
    )
    speech = np.frombuffer(_pcm_tone(300, amp=0.3), dtype="<i2").astype(np.float32)
    speech = speech / 32768.0
    assert vad.feed(speech) is None
    silence = np.zeros(int(16000 * 0.25), dtype=np.float32)
    ready = vad.feed(silence)
    assert ready is not None


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
    recognizer = MockSpeechRecognizer(transcripts=[ayah1, ayah2])
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
