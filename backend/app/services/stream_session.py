"""Realtime memorization session state machine."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import numpy as np

from ..config import settings
from .assessor import MemorizationAssessor
from .quran_service import QuranService
from .speech_service import SpeechRecognizer
from .stream_audio import EnergyVadSegmenter, PcmRingBuffer

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    CONNECTING = "connecting"
    READY = "ready"
    LISTENING = "listening"
    ASSESSING = "assessing"
    ADVANCING = "advancing"
    PAUSED = "paused"
    STOPPING = "stopping"
    CLOSED = "closed"


ALLOWED_FAIL_POLICIES = frozenset({"retry", "continue", "stop"})
ALLOWED_AUDIO_FORMATS = frozenset({"pcm_s16le"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@dataclass
class AyahAttemptStats:
    surah: int
    ayah: int
    best_score: float = 0.0
    passed: bool = False
    attempts: int = 0
    skipped: bool = False


@dataclass
class StreamSessionConfig:
    start_surah: int
    start_ayah: int
    end_surah: int | None = None
    end_ayah: int | None = None
    threshold: float = field(default_factory=lambda: settings.DEFAULT_THRESHOLD)
    fail_policy: str = "continue"
    cross_surah: bool = False
    audio_format: str = "pcm_s16le"
    sample_rate: int = 16000
    channels: int = 1
    chunk_ms: int = 250
    partials: bool = field(default_factory=lambda: settings.STREAM_PARTIALS_DEFAULT)
    auto_advance: bool = True


class StreamSession:
    """One WebSocket memorization session."""

    def __init__(
        self,
        quran: QuranService,
        recognizer: SpeechRecognizer,
        config: StreamSessionConfig,
    ):
        self.quran = quran
        self.recognizer = recognizer
        self.config = config
        self.session_id = str(uuid.uuid4())
        self.state = SessionState.CONNECTING
        self.started_at = _utc_now()
        self.ended_at: datetime | None = None
        self.end_reason: str | None = None

        self.current_surah = config.start_surah
        self.current_ayah = config.start_ayah
        self.index_in_session = 0
        self.attempt = 0
        self.stt_busy = False
        self.last_activity = time.monotonic()
        self._last_partial_at = 0.0

        self.buffer = PcmRingBuffer(
            sample_rate=config.sample_rate,
            max_seconds=settings.STREAM_MAX_BUFFER_S,
        )
        self.vad = EnergyVadSegmenter(sample_rate=config.sample_rate)
        self._stats: dict[tuple[int, int], AyahAttemptStats] = {}
        self._ensure_stats(self.current_surah, self.current_ayah)

    # --- helpers ---------------------------------------------------------

    def _ensure_stats(self, surah: int, ayah: int) -> AyahAttemptStats:
        key = (surah, ayah)
        if key not in self._stats:
            self._stats[key] = AyahAttemptStats(surah=surah, ayah=ayah)
        return self._stats[key]

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def current_ayah_payload(self) -> dict[str, Any]:
        ayah = self.quran.get_ayah(self.current_surah, self.current_ayah)
        text = ayah["text"] if ayah else ""
        return {
            "surah": self.current_surah,
            "ayah": self.current_ayah,
            "text": text,
            "index_in_session": self.index_in_session,
        }

    def base_event(self, event_type: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "type": event_type,
            "session_id": self.session_id,
            "ts": _iso(_utc_now()),
        }
        payload.update(extra)
        return payload

    def ready_event(self) -> dict[str, Any]:
        self.state = SessionState.READY
        return self.base_event(
            "session.ready",
            current=self.current_ayah_payload(),
            config={
                "threshold": self.config.threshold,
                "fail_policy": self.config.fail_policy,
                "auto_advance": self.config.auto_advance,
                "partials": self.config.partials,
                "audio": {
                    "format": self.config.audio_format,
                    "sample_rate": self.config.sample_rate,
                    "channels": self.config.channels,
                },
            },
        )

    def summary_event(self, reason: str) -> dict[str, Any]:
        self.end_reason = reason
        self.ended_at = _utc_now()
        self.state = SessionState.CLOSED
        results = []
        passed = failed = skipped = completed = 0
        for key in sorted(self._stats.keys()):
            s = self._stats[key]
            if s.skipped:
                skipped += 1
            if s.attempts or s.skipped:
                completed += 1
            if s.passed:
                passed += 1
            elif s.attempts and not s.skipped:
                failed += 1
            results.append(
                {
                    "surah": s.surah,
                    "ayah": s.ayah,
                    "best_score": round(s.best_score, 4),
                    "passed": s.passed,
                    "attempts": s.attempts,
                    "skipped": s.skipped,
                }
            )
        return self.base_event(
            "session.summary",
            started_at=_iso(self.started_at),
            ended_at=_iso(self.ended_at),
            reason=reason,
            ayahs_completed=completed,
            ayahs_passed=passed,
            ayahs_failed=failed,
            ayahs_skipped=skipped,
            results=results,
        )

    def error_event(
        self,
        code: str,
        message: str,
        *,
        fatal: bool = True,
    ) -> dict[str, Any]:
        return self.base_event(
            "error",
            code=code,
            message=message,
            fatal=fatal,
        )

    # --- validation ------------------------------------------------------

    @classmethod
    def validate_and_build(
        cls,
        quran: QuranService,
        recognizer: SpeechRecognizer,
        raw: dict[str, Any],
    ) -> tuple[StreamSession | None, dict[str, Any] | None]:
        """Parse session.start; return (session, None) or (None, error_event)."""
        try:
            start_surah = int(raw["start_surah"])
            start_ayah = int(raw["start_ayah"])
        except (KeyError, TypeError, ValueError):
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_start",
                "message": "start_surah and start_ayah are required integers",
                "fatal": True,
            }

        if not quran.get_ayah(start_surah, start_ayah):
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "ayah_not_found",
                "message": f"Ayah {start_surah}:{start_ayah} not found",
                "fatal": True,
            }

        end_surah = raw.get("end_surah")
        end_ayah = raw.get("end_ayah")
        if end_surah is not None or end_ayah is not None:
            if end_surah is None or end_ayah is None:
                return None, {
                    "type": "error",
                    "session_id": None,
                    "ts": _iso(_utc_now()),
                    "code": "invalid_config",
                    "message": "end_surah and end_ayah must both be set",
                    "fatal": True,
                }
            end_surah = int(end_surah)
            end_ayah = int(end_ayah)
            if not quran.get_ayah(end_surah, end_ayah):
                return None, {
                    "type": "error",
                    "session_id": None,
                    "ts": _iso(_utc_now()),
                    "code": "ayah_not_found",
                    "message": f"End ayah {end_surah}:{end_ayah} not found",
                    "fatal": True,
                }
            if QuranService.corpus_order(end_surah, end_ayah) < QuranService.corpus_order(
                start_surah, start_ayah
            ):
                return None, {
                    "type": "error",
                    "session_id": None,
                    "ts": _iso(_utc_now()),
                    "code": "invalid_start",
                    "message": "end ayah must be at or after start ayah",
                    "fatal": True,
                }

        threshold = float(raw.get("threshold", settings.DEFAULT_THRESHOLD))
        if not 0.5 <= threshold <= 1.0:
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_config",
                "message": "threshold must be between 0.5 and 1.0",
                "fatal": True,
            }

        fail_policy = str(raw.get("fail_policy", "continue"))
        if fail_policy not in ALLOWED_FAIL_POLICIES:
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_config",
                "message": "fail_policy must be retry|continue|stop",
                "fatal": True,
            }

        audio = raw.get("audio") or {}
        audio_format = str(audio.get("format", "pcm_s16le"))
        if audio_format not in ALLOWED_AUDIO_FORMATS:
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_config",
                "message": "Only pcm_s16le is supported in v1",
                "fatal": True,
            }

        sample_rate = int(audio.get("sample_rate", 16000))
        channels = int(audio.get("channels", 1))
        if sample_rate != 16000 or channels != 1:
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_config",
                "message": "audio must be mono 16000 Hz",
                "fatal": True,
            }

        chunk_ms = int(audio.get("chunk_ms", 250))
        if not 100 <= chunk_ms <= 1000:
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_config",
                "message": "chunk_ms must be between 100 and 1000",
                "fatal": True,
            }

        cross_surah = bool(raw.get("cross_surah", False))
        if (
            end_surah is not None
            and not cross_surah
            and end_surah != start_surah
        ):
            return None, {
                "type": "error",
                "session_id": None,
                "ts": _iso(_utc_now()),
                "code": "invalid_config",
                "message": "end_surah differs from start_surah but cross_surah is false",
                "fatal": True,
            }

        # Partials default OFF for CPU; client may request true.
        partials = bool(raw.get("partials", settings.STREAM_PARTIALS_DEFAULT))
        auto_advance = bool(raw.get("auto_advance", True))

        cfg = StreamSessionConfig(
            start_surah=start_surah,
            start_ayah=start_ayah,
            end_surah=end_surah,
            end_ayah=end_ayah,
            threshold=threshold,
            fail_policy=fail_policy,
            cross_surah=cross_surah,
            audio_format=audio_format,
            sample_rate=sample_rate,
            channels=channels,
            chunk_ms=chunk_ms,
            partials=partials,
            auto_advance=auto_advance,
        )
        session = cls(quran, recognizer, cfg)
        return session, None

    # --- audio / assess --------------------------------------------------

    def accepts_audio(self) -> bool:
        return self.state in {SessionState.READY, SessionState.LISTENING}

    def on_audio_chunk(self, raw: bytes) -> list[dict[str, Any]]:
        """Ingest binary PCM; may return segment-ready signal events (empty usually)."""
        self.touch()
        if not self.accepts_audio():
            if self.state == SessionState.ASSESSING:
                return []
            return [
                self.error_event(
                    "not_ready",
                    "Session is not accepting audio",
                    fatal=False,
                )
            ]

        if len(raw) > settings.STREAM_MAX_FRAME_BYTES:
            return [
                self.error_event(
                    "invalid_audio",
                    "Binary frame exceeds max size",
                    fatal=False,
                )
            ]

        if self.state == SessionState.READY:
            self.state = SessionState.LISTENING

        self.buffer.append_pcm_s16le(raw)
        # Feed only the new chunk to VAD (decode once).
        if len(raw) % 2:
            raw = raw[:-1]
        if raw:
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            segment = self.vad.feed(samples)
            if segment is not None:
                return [{"type": "_assess_trigger", "reason": "silence"}]
        return []

    def should_emit_partial(self) -> bool:
        if not self.config.partials:
            return False
        if self.stt_busy or self.state != SessionState.LISTENING:
            return False
        if self.buffer.duration_ms < settings.STREAM_MIN_UTTERANCE_MS:
            return False
        now = time.monotonic()
        every = settings.STREAM_PARTIAL_EVERY_MS / 1000.0
        if now - self._last_partial_at < every:
            return False
        return True

    def mark_partial_emitted(self) -> None:
        self._last_partial_at = time.monotonic()

    def run_partial(self) -> list[dict[str, Any]]:
        """Optional light STT for UX. Skipped when busy / disabled."""
        if not self.should_emit_partial():
            if self.config.partials and self.stt_busy:
                return [
                    self.error_event(
                        "busy",
                        "Skipped partial; assessment in flight",
                        fatal=False,
                    )
                ]
            return []

        self.stt_busy = True
        self.mark_partial_emitted()
        t0 = time.perf_counter()
        try:
            samples = self.buffer.snapshot()
            # Cap partial window to last ~3s to keep STT cheap.
            max_samples = int(self.config.sample_rate * 3.0)
            if len(samples) > max_samples:
                samples = samples[-max_samples:]
            recognized = self.recognizer.transcribe_audio(
                samples, self.config.sample_rate
            )
            stt_ms = int((time.perf_counter() - t0) * 1000)
            events = [
                self.base_event(
                    "partial.transcript",
                    surah=self.current_surah,
                    ayah=self.current_ayah,
                    recognized=recognized or "",
                    stable=False,
                    stt_ms=stt_ms,
                )
            ]
            # Lightweight progress only — full assessor on finals.
            expected = self.quran.get_ayah(self.current_surah, self.current_ayah)
            if expected and recognized:
                assessor = MemorizationAssessor(threshold=self.config.threshold)
                result = assessor.assess(expected["text"], recognized)
                equal = sum(1 for a in result.alignment if a.get("op") == "equal")
                total = max(1, len(result.alignment))
                progress = min(1.0, equal / total)
                events.append(
                    self.base_event(
                        "partial.alignment",
                        surah=self.current_surah,
                        ayah=self.current_ayah,
                        alignment=result.alignment[:40],
                        progress=round(progress, 3),
                    )
                )
            return events
        except Exception:
            logger.exception("partial STT failed session=%s", self.session_id)
            return [
                self.error_event(
                    "stt_unavailable",
                    "Speech model unavailable",
                    fatal=False,
                )
            ]
        finally:
            self.stt_busy = False

    def run_assess(self, *, reason: str = "silence") -> list[dict[str, Any]]:
        """Ayah-final STT + MemorizationAssessor + advance policy."""
        if self.stt_busy:
            return [
                self.error_event(
                    "busy",
                    "Assessment already in progress",
                    fatal=False,
                )
            ]

        samples = self.buffer.snapshot()
        if samples.size < int(
            self.config.sample_rate * settings.STREAM_MIN_UTTERANCE_MS / 1000.0
        ):
            return []

        self.state = SessionState.ASSESSING
        self.stt_busy = True
        self.attempt += 1
        events: list[dict[str, Any]] = []
        audio_ms = int(1000.0 * len(samples) / self.config.sample_rate)
        t0 = time.perf_counter()

        try:
            recognized = self.recognizer.transcribe_audio(
                samples, self.config.sample_rate
            )
            stt_ms = int((time.perf_counter() - t0) * 1000)
        except Exception:
            logger.exception("final STT failed session=%s", self.session_id)
            self.stt_busy = False
            self.state = SessionState.LISTENING
            return [
                self.error_event(
                    "stt_unavailable",
                    "Speech model unavailable",
                    fatal=True,
                )
            ]

        expected_ayah = self.quran.get_ayah(self.current_surah, self.current_ayah)
        expected_text = expected_ayah["text"] if expected_ayah else ""
        assessor = MemorizationAssessor(threshold=self.config.threshold)
        result = assessor.assess(expected_text, recognized or "")

        stats = self._ensure_stats(self.current_surah, self.current_ayah)
        stats.attempts += 1
        stats.best_score = max(stats.best_score, result.score)
        if result.passed:
            stats.passed = True

        will_advance = False
        if result.passed and self.config.auto_advance:
            will_advance = True
        elif not result.passed and self.config.fail_policy == "continue":
            will_advance = True

        events.append(
            self.base_event(
                "ayah.result",
                surah=self.current_surah,
                ayah=self.current_ayah,
                attempt=self.attempt,
                score=result.score,
                passed=result.passed,
                warning=result.warning,
                expected=result.expected,
                recognized=result.recognized,
                missing_words=result.missing_words,
                extra_words=result.extra_words,
                wrong_words=result.wrong_words,
                alignment=result.alignment,
                message=result.message,
                will_advance=will_advance,
                audio_ms=audio_ms,
                stt_ms=stt_ms,
                trigger=reason,
            )
        )

        # Clear buffer after final (keep small overlap).
        self.buffer.clear(keep_overlap_ms=settings.STREAM_OVERLAP_MS)
        self.vad.reset()
        self.stt_busy = False

        if result.passed and self.config.auto_advance:
            events.extend(self._advance(reason="passed"))
        elif not result.passed and self.config.fail_policy == "retry":
            events.append(
                self.base_event(
                    "session.waiting",
                    surah=self.current_surah,
                    ayah=self.current_ayah,
                    attempt=self.attempt,
                    hint="Retry the same ayah. Review highlighted words.",
                )
            )
            self.state = SessionState.LISTENING
        elif not result.passed and self.config.fail_policy == "continue":
            events.extend(self._advance(reason="continue_policy"))
        elif not result.passed and self.config.fail_policy == "stop":
            events.append(self.summary_event("fail_stop"))
        else:
            # passed but auto_advance false
            self.state = SessionState.LISTENING

        return events

    def force_advance(self, reason: str = "skip") -> list[dict[str, Any]]:
        stats = self._ensure_stats(self.current_surah, self.current_ayah)
        stats.skipped = True
        self.buffer.clear(keep_overlap_ms=settings.STREAM_OVERLAP_MS)
        self.vad.reset()
        return self._advance(reason="skip" if reason == "skip" else reason)

    def _advance(self, reason: str) -> list[dict[str, Any]]:
        self.state = SessionState.ADVANCING
        from_ref = {"surah": self.current_surah, "ayah": self.current_ayah}

        # Range complete?
        if (
            self.config.end_surah is not None
            and self.config.end_ayah is not None
            and self.current_surah == self.config.end_surah
            and self.current_ayah == self.config.end_ayah
        ):
            return [self.summary_event("range_complete")]

        nxt = self.quran.next_ayah(
            self.current_surah,
            self.current_ayah,
            cross_surah=self.config.cross_surah,
        )
        if nxt is None:
            return [self.summary_event("surah_complete")]

        self.current_surah, self.current_ayah = nxt
        self.index_in_session += 1
        self.attempt = 0
        self._ensure_stats(self.current_surah, self.current_ayah)
        self.state = SessionState.LISTENING

        # If next is past configured end (cross_surah edge), stop.
        if self.config.end_surah is not None and self.config.end_ayah is not None:
            if QuranService.corpus_order(
                self.current_surah, self.current_ayah
            ) > QuranService.corpus_order(
                self.config.end_surah, self.config.end_ayah
            ):
                return [self.summary_event("range_complete")]

        event = self.base_event(
            "session.advance",
            to=self.current_ayah_payload(),
            reason=reason,
        )
        event["from"] = from_ref
        return [event]

    def check_timeouts(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now - self.last_activity > settings.STREAM_IDLE_TIMEOUT_S:
            return [self.summary_event("session_timeout")]
        elapsed = ( _utc_now() - self.started_at).total_seconds()
        if elapsed > settings.STREAM_MAX_SESSION_S:
            return [self.summary_event("session_timeout")]
        return []
