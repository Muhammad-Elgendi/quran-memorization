"""Speech recognition behind a swappable interface."""

from abc import ABC, abstractmethod

from ..config import settings


class SpeechRecognizer(ABC):
    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """Convert an audio recording to Arabic text."""
        raise NotImplementedError


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

        inputs = self._processor(
            audio,
            sampling_rate=16000,
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
    """Deterministic recognizer for tests."""

    def __init__(self, transcript: str = ""):
        self.transcript = transcript

    def transcribe(self, audio_path: str) -> str:
        return self.transcript
