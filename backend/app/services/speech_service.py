"""Speech recognition behind a swappable interface."""

from __future__ import annotations

import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

import numpy as np

from ..config import settings


class SpeechRecognizer(ABC):
    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """Convert an audio recording to Arabic text."""
        raise NotImplementedError

    def transcribe_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
    ) -> str:
        """PCM path for streaming; default writes a temp WAV then transcribes."""
        audio = np.asarray(samples, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / 32768.0

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp:
                temp_path = temp.name
            import soundfile as sf

            sf.write(temp_path, audio, sample_rate, subtype="PCM_16")
            return self.transcribe(temp_path)
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)


class MoonshineArabicRecognizer(SpeechRecognizer):
    """Moonshine Arabic Tiny via Hugging Face Transformers."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.MOONSHINE_MODEL
        self._model = None
        self._processor = None

    def _load(self) -> None:
        if self._model is not None:
            return

        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        self._processor = AutoProcessor.from_pretrained(self.model_name)
        self._model = AutoModelForSpeechSeq2Seq.from_pretrained(self.model_name)
        self._model.eval()

    def transcribe(self, audio_path: str) -> str:
        self._load()

        import librosa
        import torch

        audio, _ = librosa.load(audio_path, sr=16000, mono=True)
        return self._transcribe_samples(audio, 16000)

    def transcribe_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
    ) -> str:
        """Direct PCM path — avoids a temp WAV round-trip for streaming."""
        self._load()
        audio = np.asarray(samples, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / 32768.0
        if sample_rate != 16000 and audio.size:
            import librosa

            audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
        return self._transcribe_samples(audio, 16000)

    def _transcribe_samples(self, audio: np.ndarray, sample_rate: int) -> str:
        import torch

        inputs = self._processor(
            audio,
            sampling_rate=sample_rate,
            return_tensors="pt",
        )

        with torch.no_grad():
            generated_ids = self._model.generate(**inputs)

        result = self._processor.batch_decode(
            generated_ids,
            skip_special_tokens=True,
        )
        return result[0].strip()


class MockSpeechRecognizer(SpeechRecognizer):
    """Deterministic recognizer for tests.

    ``transcript`` is used when the queue is empty. Pass ``transcripts`` for
    ordered replies across multiple STT calls in a stream session.
    """

    def __init__(
        self,
        transcript: str = "",
        transcripts: Sequence[str] | None = None,
    ):
        self.transcript = transcript
        self._queue = list(transcripts or [])
        self.calls = 0

    def transcribe(self, audio_path: str) -> str:
        return self._next()

    def transcribe_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
    ) -> str:
        return self._next()

    def _next(self) -> str:
        self.calls += 1
        if self._queue:
            return self._queue.pop(0)
        return self.transcript