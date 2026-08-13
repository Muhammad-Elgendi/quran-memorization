"""PCM ring buffer and simple energy VAD for streaming sessions."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import settings


@dataclass
class SegmentReady:
    """Utterance ready for ayah-final assessment."""

    samples: np.ndarray
    sample_rate: int
    duration_ms: float


class PcmRingBuffer:
    """Append-only float32 PCM buffer with max duration and overlap trim."""

    def __init__(
        self,
        sample_rate: int = 16000,
        max_seconds: float | None = None,
    ):
        self.sample_rate = sample_rate
        self.max_samples = int(
            (max_seconds if max_seconds is not None else settings.STREAM_MAX_BUFFER_S)
            * sample_rate
        )
        self._buf = np.zeros(0, dtype=np.float32)

    def __len__(self) -> int:
        return int(self._buf.shape[0])

    @property
    def duration_ms(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return 1000.0 * len(self) / self.sample_rate

    def append_pcm_s16le(self, raw: bytes) -> None:
        if not raw:
            return
        if len(raw) % 2:
            raw = raw[:-1]
        if not raw:
            return
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        self.append_samples(samples)

    def append_samples(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        self._buf = np.concatenate([self._buf, audio])
        if len(self._buf) > self.max_samples:
            self._buf = self._buf[-self.max_samples :]

    def clear(self, keep_overlap_ms: float = 0.0) -> None:
        if keep_overlap_ms <= 0 or len(self._buf) == 0:
            self._buf = np.zeros(0, dtype=np.float32)
            return
        keep = int(self.sample_rate * keep_overlap_ms / 1000.0)
        if keep <= 0:
            self._buf = np.zeros(0, dtype=np.float32)
            return
        self._buf = self._buf[-keep:].copy()

    def snapshot(self) -> np.ndarray:
        return self._buf.copy()


class EnergyVadSegmenter:
    """Silence-based utterance segmentation (Strategy A — MVP).

    Uses RMS energy only — no webrtcvad / extra deps — to keep CPU light.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        silence_ms: int | None = None,
        min_utterance_ms: int | None = None,
        rms_threshold: float | None = None,
        frame_ms: int = 20,
    ):
        self.sample_rate = sample_rate
        self.silence_ms = (
            settings.STREAM_SILENCE_MS if silence_ms is None else silence_ms
        )
        self.min_utterance_ms = (
            settings.STREAM_MIN_UTTERANCE_MS
            if min_utterance_ms is None
            else min_utterance_ms
        )
        self.rms_threshold = (
            settings.STREAM_VAD_RMS_THRESHOLD
            if rms_threshold is None
            else rms_threshold
        )
        self.frame_samples = max(1, int(sample_rate * frame_ms / 1000))
        self._speech_active = False
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._pending = np.zeros(0, dtype=np.float32)

    def reset(self) -> None:
        self._speech_active = False
        self._silence_ms = 0.0
        self._utterance_ms = 0.0
        self._pending = np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> SegmentReady | None:
        """Feed new PCM; return a segment when silence ends an utterance."""
        if samples.size == 0:
            return None
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        self._pending = np.concatenate([self._pending, audio])

        ready: SegmentReady | None = None
        while len(self._pending) >= self.frame_samples:
            frame = self._pending[: self.frame_samples]
            self._pending = self._pending[self.frame_samples :]
            frame_ms = 1000.0 * self.frame_samples / self.sample_rate
            rms = float(np.sqrt(np.mean(np.square(frame))))
            is_speech = rms >= self.rms_threshold

            if is_speech:
                self._speech_active = True
                self._silence_ms = 0.0
                self._utterance_ms += frame_ms
            elif self._speech_active:
                self._silence_ms += frame_ms
                self._utterance_ms += frame_ms
                if (
                    self._silence_ms >= self.silence_ms
                    and self._utterance_ms >= self.min_utterance_ms
                ):
                    # Caller holds the full attempt buffer; we only signal.
                    ready = SegmentReady(
                        samples=np.zeros(0, dtype=np.float32),
                        sample_rate=self.sample_rate,
                        duration_ms=self._utterance_ms,
                    )
                    self._speech_active = False
                    self._silence_ms = 0.0
                    self._utterance_ms = 0.0
                    break
        return ready
