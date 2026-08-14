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
from .stream_audio import EnergyVadSegmenter, PcmRingBuffer, pcm_has_speech
from .stt_confidence import (
    Transcription,
    apply_ayah_recovery,
    recovery_debug_fields,
    stt_words_payload,
    transcription_from_plain_text,
    trim_overgenerated_partial,
)

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
_ASSESS_PRIORITY = {"force": 4, "silence": 3, "silence_short": 2, "coverage": 1}
INCOMPLETE_LISTEN_HINT = "Still listening — finish the ayah, or tap Check now."


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
        self._last_probe_at = 0.0
        self._last_periodic_at = 0.0
        self._busy_count = 0
        self._stt_ms_total = 0
        self._last_stt: Transcription | None = None
        self._coverage_streak = 0
        self._stt_queued = False
        self._pending_assess: dict[str, Any] | None = None

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
        wall_ms = int((self.ended_at - self.started_at).total_seconds() * 1000)
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
            busy_errors=self._busy_count,
            stt_ms_total=self._stt_ms_total,
            wall_ms=wall_ms,
        )

    def error_event(
        self,
        code: str,
        message: str,
        *,
        fatal: bool = True,
    ) -> dict[str, Any]:
        if code == "busy":
            self._busy_count += 1
        return self.base_event(
            "error",
            code=code,
            message=message,
            fatal=fatal,
        )

    def uses_unified_periodic(self) -> bool:
        """A4: one STT tick feeds partial UX + completion probe when both enabled."""
        return self.config.partials and settings.STREAM_COMPLETION_PROBE

    def _record_stt_ms(self, stt_ms: int) -> None:
        self._stt_ms_total += max(0, stt_ms)

    def _reset_stream_decode_state(self) -> None:
        self._last_stt = None
        self._coverage_streak = 0
        self._stt_queued = False
        self._pending_assess = None

    def push_pending_assess(self, trigger: dict[str, Any]) -> None:
        """Keep the highest-priority pending finalize (latest wins ties)."""
        reason = str(trigger.get("reason") or "silence")
        incoming = _ASSESS_PRIORITY.get(reason, 0)
        if self._pending_assess is None:
            self._pending_assess = dict(trigger)
            return
        held = _ASSESS_PRIORITY.get(
            str(self._pending_assess.get("reason") or "silence"), 0
        )
        if incoming >= held:
            self._pending_assess = dict(trigger)

    def pop_pending_assess(self) -> dict[str, Any] | None:
        job = self._pending_assess
        self._pending_assess = None
        if not job:
            return None
        # Audio arrived while STT was busy — don't trust a stale coverage hint.
        if job.get("reason") != "force" and self._stt_queued:
            job = {**job, "recognized": None}
        return job

    def has_stt_work(self) -> bool:
        return self._pending_assess is not None or self.should_run_periodic_stt()

    def _listening_event(self, *, cleared: bool, hint: str | None = None) -> dict[str, Any]:
        return self.base_event(
            "session.listening",
            surah=self.current_surah,
            ayah=self.current_ayah,
            incomplete=True,
            cleared=cleared,
            hint=hint or INCOMPLETE_LISTEN_HINT,
        )

    def _pcm_has_stt_energy(self, samples) -> bool:
        return pcm_has_speech(
            samples,
            self.config.sample_rate,
            rms_threshold=settings.STREAM_STT_RMS_THRESHOLD,
        )

    def _no_speech_events(self, *, cleared: bool) -> list[dict[str, Any]]:
        """Check now / finalize heard nothing — not a memorization fail."""
        self.stt_busy = False
        self.state = SessionState.LISTENING
        return [
            self.error_event(
                "no_speech",
                "No speech detected — recite the ayah, then tap Check now.",
                fatal=False,
            ),
            self._listening_event(
                cleared=cleared,
                hint="No speech detected — finish the ayah, or tap Check now.",
            ),
        ]

    def _abandon_incomplete_attempt(self) -> list[dict[str, Any]]:
        """Long silence with empty / no-speech Heard: drop audio so a retry is clean."""
        self.buffer.clear(keep_overlap_ms=0.0)
        self._reset_stream_decode_state()
        self.vad.reset()
        self.stt_busy = False
        self.state = SessionState.LISTENING
        return [
            self.base_event(
                "partial.transcript",
                surah=self.current_surah,
                ayah=self.current_ayah,
                recognized="",
                stable=False,
            ),
            self.base_event(
                "partial.alignment",
                surah=self.current_surah,
                ayah=self.current_ayah,
                alignment=[],
                progress=0.0,
                provisional=True,
            ),
            self._listening_event(cleared=True),
        ]

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
        return self.state in {
            SessionState.READY,
            SessionState.LISTENING,
            SessionState.ASSESSING,
        }

    def on_audio_chunk(self, raw: bytes) -> list[dict[str, Any]]:
        """Ingest binary PCM; may return segment-ready signal events (empty usually)."""
        self.touch()
        if not self.accepts_audio():
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

        assessing = self.state == SessionState.ASSESSING
        if self.state == SessionState.READY:
            self.state = SessionState.LISTENING

        self.buffer.append_pcm_s16le(raw)
        if self.stt_busy or assessing:
            self._stt_queued = True
        # Don't nest a VAD finalize while ayah-final STT is already running.
        if assessing:
            return []
        # Feed only the new chunk to VAD (decode once).
        if len(raw) % 2:
            raw = raw[:-1]
        if raw:
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            segment = self.vad.feed(samples)
            if segment is not None:
                return [
                    {
                        "type": "_assess_trigger",
                        "reason": segment.reason,
                    }
                ]
        return []

    def should_run_periodic_stt(self) -> bool:
        """Gate for completion probe, partials, or unified A4 tick."""
        if self.stt_busy or self.state != SessionState.LISTENING:
            if self.stt_busy:
                self._stt_queued = True
            return False
        if self.buffer.duration_ms <= settings.STREAM_OVERLAP_MS + 1:
            return False
        if self.buffer.duration_ms < settings.STREAM_MIN_UTTERANCE_MS:
            return False

        want_partial = self.config.partials
        want_probe = settings.STREAM_COMPLETION_PROBE
        if not want_partial and not want_probe:
            return False

        queued = self._stt_queued
        if queued:
            return True

        now = time.monotonic()
        if self.uses_unified_periodic():
            every = (
                min(
                    settings.STREAM_PARTIAL_EVERY_MS,
                    settings.STREAM_COMPLETION_PROBE_MS,
                )
                / 1000.0
            )
            return now - self._last_periodic_at >= every
        if want_probe:
            every = settings.STREAM_COMPLETION_PROBE_MS / 1000.0
            return now - self._last_probe_at >= every
        every = settings.STREAM_PARTIAL_EVERY_MS / 1000.0
        return now - self._last_partial_at >= every

    def _mark_periodic_emitted(self) -> None:
        now = time.monotonic()
        self._stt_queued = False
        if self.uses_unified_periodic():
            self._last_periodic_at = now
        elif settings.STREAM_COMPLETION_PROBE:
            self._last_probe_at = now
        else:
            self._last_partial_at = now

    def _expected_text(self) -> str:
        ayah = self.quran.get_ayah(self.current_surah, self.current_ayah)
        return ayah["text"] if ayah else ""

    def _recover_heard(self, transcription: Transcription) -> Transcription:
        return apply_ayah_recovery(self._expected_text(), transcription)

    def _partial_display_text(
        self,
        recognized: str,
        expected_text: str,
    ) -> str:
        text = (recognized or "").strip()
        if not text:
            return ""
        return trim_overgenerated_partial(expected_text, text)

    def _partial_events_from_recognized(
        self,
        recognized: str,
        *,
        stt_ms: int,
        transcription: Transcription | None = None,
    ) -> list[dict[str, Any]]:
        expected = self.quran.get_ayah(self.current_surah, self.current_ayah)
        expected_text = expected["text"] if expected else ""
        display = self._partial_display_text(recognized, expected_text)
        if not display:
            return []

        transcript_event = self.base_event(
            "partial.transcript",
            surah=self.current_surah,
            ayah=self.current_ayah,
            recognized=display,
            stable=False,
            stt_ms=stt_ms,
        )
        if (
            transcription is not None
            and transcription.scored
            and settings.STT_CONFIDENCE_FILTER
        ):
            transcript_event["sequence_confidence"] = round(
                transcription.sequence_confidence, 4
            )
            display_words = display.split()
            di = 0
            words_out: list[dict[str, Any]] = []
            for word in transcription.words:
                payload = {
                    "text": word.text,
                    "confidence": round(word.confidence, 4),
                    "kept": word.kept,
                }
                if word.kept:
                    if di < len(display_words) and word.text == display_words[di]:
                        di += 1
                    else:
                        payload["kept"] = False
                words_out.append(payload)
            transcript_event["words"] = words_out
        if transcription is not None:
            transcript_event.update(recovery_debug_fields(transcription))

        events: list[dict[str, Any]] = [transcript_event]
        if expected_text:
            assessor = MemorizationAssessor(threshold=self.config.threshold)
            progress = assessor.progress(expected_text, display)
            result = assessor.assess(expected_text, display)
            provisional = progress < settings.STREAM_COVERAGE_THRESHOLD
            events.append(
                self.base_event(
                    "partial.alignment",
                    surah=self.current_surah,
                    ayah=self.current_ayah,
                    alignment=result.alignment[:40],
                    progress=round(progress, 3),
                    provisional=provisional,
                )
            )
        return events

    def run_periodic_stt(self) -> list[dict[str, Any]]:
        """Periodic STT: probe-only, partial-only, or unified A4 (one pass, both outputs)."""
        if not self.should_run_periodic_stt():
            return []

        samples = self.buffer.snapshot()
        min_samples = int(
            self.config.sample_rate * settings.STREAM_MIN_UTTERANCE_MS / 1000.0
        )
        if samples.size < min_samples:
            return []

        unified = self.uses_unified_periodic()
        want_partial = self.config.partials
        want_probe = settings.STREAM_COMPLETION_PROBE

        # Partial-only arm (A2): cap window to last ~3s to keep CPU down.
        if want_partial and not want_probe and not unified:
            max_samples = int(self.config.sample_rate * 3.0)
            if len(samples) > max_samples:
                samples = samples[-max_samples:]

        if not self._pcm_has_stt_energy(samples):
            self._mark_periodic_emitted()
            return []

        self.stt_busy = True
        self._mark_periodic_emitted()
        t0 = time.perf_counter()
        try:
            transcription = self.recognizer.transcribe_audio_detailed(
                samples,
                self.config.sample_rate,
                threshold=self.config.threshold,
            )
            stt_ms = int((time.perf_counter() - t0) * 1000)
            self._record_stt_ms(stt_ms)
        except Exception:
            logger.exception("periodic STT failed session=%s", self.session_id)
            return [
                self.error_event(
                    "stt_unavailable",
                    "Speech model unavailable",
                    fatal=False,
                )
            ]
        finally:
            self.stt_busy = False

        transcription = self._recover_heard(transcription)
        self._last_stt = transcription
        recognized = transcription.text or ""

        events: list[dict[str, Any]] = []
        if want_partial:
            events.extend(
                self._partial_events_from_recognized(
                    recognized,
                    stt_ms=stt_ms,
                    transcription=transcription,
                )
            )

        if want_probe:
            expected = self.quran.get_ayah(self.current_surah, self.current_ayah)
            coverage = 0.0
            if expected and recognized:
                assessor = MemorizationAssessor(threshold=self.config.threshold)
                coverage = assessor.progress(expected["text"], recognized)
            if expected and recognized and coverage >= settings.STREAM_COVERAGE_THRESHOLD:
                self._coverage_streak += 1
                needed = max(1, int(settings.STREAM_COVERAGE_STABLE_TICKS))
                if self._coverage_streak >= needed:
                    events.append(
                        {
                            "type": "_assess_trigger",
                            "reason": "coverage",
                            "recognized": recognized,
                            "coverage": coverage,
                        }
                    )
            else:
                self._coverage_streak = 0
        return events

    def should_probe_completion(self) -> bool:
        """Backward-compatible alias; prefer should_run_periodic_stt()."""
        if self.uses_unified_periodic() or self.config.partials:
            return self.should_run_periodic_stt()
        return (
            settings.STREAM_COMPLETION_PROBE and self.should_run_periodic_stt()
        )

    def run_completion_probe(self) -> list[dict[str, Any]]:
        """Backward-compatible alias; prefer run_periodic_stt()."""
        if self.uses_unified_periodic() or self.config.partials:
            return self.run_periodic_stt()
        if not settings.STREAM_COMPLETION_PROBE:
            return []
        return self.run_periodic_stt()

    def should_emit_partial(self) -> bool:
        """Backward-compatible alias; prefer should_run_periodic_stt()."""
        if self.uses_unified_periodic():
            return False
        return self.config.partials and self.should_run_periodic_stt()

    def run_partial(self) -> list[dict[str, Any]]:
        """Backward-compatible alias; prefer run_periodic_stt()."""
        if self.uses_unified_periodic():
            return []
        if not self.config.partials:
            return []
        return self.run_periodic_stt()

    def run_assess(
        self,
        *,
        reason: str = "silence",
        recognized_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ayah-final STT + MemorizationAssessor + advance policy."""
        if self.stt_busy:
            self.push_pending_assess(
                {"reason": reason, "recognized": recognized_hint}
            )
            self._stt_queued = True
            return []

        samples = self.buffer.snapshot()
        if samples.size < int(
            self.config.sample_rate * settings.STREAM_MIN_UTTERANCE_MS / 1000.0
        ):
            if reason == "force":
                return self._no_speech_events(cleared=False)
            return []

        # Energy gate is for automatic paths only. Check now always STTs —
        # quiet AGC-off / denoise audio often sits under the VAD RMS floor
        # and was previously scored as empty 0% without calling the model.
        if (
            reason != "force"
            and recognized_hint is None
            and not self._pcm_has_stt_energy(samples)
        ):
            if reason == "silence_short":
                self.stt_busy = False
                self.state = SessionState.LISTENING
                return [self._listening_event(cleared=False)]
            if reason == "silence":
                return self._abandon_incomplete_attempt()
            self.vad.reset()
            return []

        self.state = SessionState.ASSESSING
        self.stt_busy = True
        events: list[dict[str, Any]] = []
        audio_ms = int(1000.0 * len(samples) / self.config.sample_rate)
        transcription: Transcription | None = None

        if recognized_hint is not None:
            stt_ms = 0
            if self._last_stt is not None and self._last_stt.text == recognized_hint:
                transcription = self._last_stt
            else:
                transcription = self._recover_heard(
                    transcription_from_plain_text(recognized_hint)
                )
            recognized = (transcription.text if transcription else recognized_hint) or ""
        else:
            t0 = time.perf_counter()
            try:
                transcription = self.recognizer.transcribe_audio_detailed(
                    samples,
                    self.config.sample_rate,
                    threshold=self.config.threshold,
                )
                transcription = self._recover_heard(transcription)
                recognized = transcription.text or ""
                self._last_stt = transcription
                stt_ms = int((time.perf_counter() - t0) * 1000)
                self._record_stt_ms(stt_ms)
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

        coverage = (
            assessor.progress(expected_text, recognized or "")
            if expected_text
            else 0.0
        )

        heard = (recognized or "").strip()

        # Short pause after a complete ayah → score right away (~400 ms).
        # Keep the buffer: a breath between words is not a restart.
        # Do not re-arm VAD short-silence here — continued pause must reach
        # long silence so incomplete ayahs are not stuck at a 50% partial.
        if reason == "silence_short" and expected_text:
            if coverage < settings.STREAM_COVERAGE_THRESHOLD:
                self.stt_busy = False
                self.state = SessionState.LISTENING
                events = []
                if self.config.partials and heard:
                    events.extend(
                        self._partial_events_from_recognized(
                            recognized or "",
                            stt_ms=stt_ms,
                            transcription=transcription,
                        )
                    )
                events.append(self._listening_event(cleared=False))
                return events

        # Empty Heard is "no speech / filtered", not a memorization error.
        # Quiet mic / filtered-empty must not paint Score 0%.
        if not heard:
            if reason == "force":
                return self._no_speech_events(cleared=False)
            if reason == "silence":
                return self._abandon_incomplete_attempt()

        # Long silence + non-empty Heard below coverage: user stopped on a
        # wrong / incomplete take. Score it (ayah.result fail) so the client
        # can play the warning tone. Fail+retry still clears the buffer.

        self.attempt += 1
        result = assessor.assess(expected_text, recognized or "")
        # Coverage is the stream completion gate. A high character score on an
        # unfinished ayah (e.g. Basmala missing بسم) must not pass-advance.
        passed = result.passed
        warning = result.warning
        if (
            reason == "silence"
            and expected_text
            and coverage < settings.STREAM_COVERAGE_THRESHOLD
        ):
            passed = False
            warning = True

        stats = self._ensure_stats(self.current_surah, self.current_ayah)
        stats.attempts += 1
        stats.best_score = max(stats.best_score, result.score)
        if passed:
            stats.passed = True

        will_advance = False
        if passed and self.config.auto_advance:
            will_advance = True
        elif not passed and self.config.fail_policy == "continue":
            will_advance = True

        result_event = self.base_event(
            "ayah.result",
            surah=self.current_surah,
            ayah=self.current_ayah,
            attempt=self.attempt,
            score=result.score,
            passed=passed,
            warning=warning,
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
            coverage=round(coverage, 4),
        )
        if (
            transcription is not None
            and transcription.scored
            and settings.STT_CONFIDENCE_FILTER
        ):
            result_event["sequence_confidence"] = round(
                transcription.sequence_confidence, 4
            )
            result_event["stt_words"] = stt_words_payload(transcription)
        if transcription is not None:
            result_event.update(recovery_debug_fields(transcription))
        events.append(result_event)

        # Keep overlap only on pass-advance. Retry of a failed (esp. short)
        # ayah must not glue 300 ms of the previous take onto the next try.
        if passed:
            self.buffer.clear(keep_overlap_ms=settings.STREAM_OVERLAP_MS)
        elif self.config.fail_policy == "retry":
            self.buffer.clear(keep_overlap_ms=0.0)
            self._last_stt = None
        else:
            self.buffer.clear(keep_overlap_ms=settings.STREAM_OVERLAP_MS)
        self.vad.reset()
        self.stt_busy = False
        self._coverage_streak = 0
        self._stt_queued = False
        self._pending_assess = None

        if passed and self.config.auto_advance:
            events.extend(self._advance(reason="passed"))
        elif not passed and self.config.fail_policy == "retry":
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
        elif not passed and self.config.fail_policy == "continue":
            events.extend(self._advance(reason="continue_policy"))
        elif not passed and self.config.fail_policy == "stop":
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
            # Open + cross_surah means corpus end; otherwise surah wall.
            reason = (
                "quran_complete" if self.config.cross_surah else "surah_complete"
            )
            return [self.summary_event(reason)]

        self.current_surah, self.current_ayah = nxt
        self.index_in_session += 1
        self.attempt = 0
        self._coverage_streak = 0
        self._stt_queued = False
        self._pending_assess = None
        self._last_stt = None
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
