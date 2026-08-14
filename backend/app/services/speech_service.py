"""Speech recognition behind a swappable interface."""

from __future__ import annotations

import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

import numpy as np

from ..config import settings
from .stt_confidence import (
    Transcription,
    align_token_probs_to_decoded_words,
    calibrate_decoder_prob,
    empty_transcription,
    filter_transcription,
    transcription_from_plain_text,
    words_from_token_pieces,
)

logger = logging.getLogger(__name__)


def _collect_special_ids(model, processor) -> set[int]:
    ids: set[int] = set()
    tokenizer = getattr(processor, "tokenizer", processor)
    special = getattr(tokenizer, "all_special_ids", None)
    if special:
        ids.update(int(i) for i in special)
    config = getattr(model, "config", None)
    for obj in (tokenizer, config, processor):
        if obj is None:
            continue
        for attr in (
            "pad_token_id",
            "eos_token_id",
            "bos_token_id",
            "unk_token_id",
            "decoder_start_token_id",
        ):
            val = getattr(obj, attr, None)
            if val is not None:
                ids.add(int(val))
    return ids


def _pcm_to_float32(samples: np.ndarray) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / 32768.0
    return audio


class SpeechRecognizer(ABC):
    @abstractmethod
    def transcribe(
        self,
        audio_path: str,
        *,
        threshold: float | None = None,
    ) -> str:
        """Convert an audio recording to Arabic text (confidence-filtered)."""
        raise NotImplementedError

    def transcribe_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        *,
        threshold: float | None = None,
    ) -> str:
        """PCM path for streaming; default writes a temp WAV then transcribes."""
        audio = _pcm_to_float32(samples)

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp:
                temp_path = temp.name
            import soundfile as sf

            sf.write(temp_path, audio, sample_rate, subtype="PCM_16")
            return self.transcribe(temp_path, threshold=threshold)
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    def transcribe_detailed(
        self,
        audio_path: str,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        text = self.transcribe(audio_path, threshold=threshold)
        return transcription_from_plain_text(text)

    def transcribe_audio_detailed(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        text = self.transcribe_audio(
            samples, sample_rate, threshold=threshold
        )
        return transcription_from_plain_text(text)


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

    def transcribe(
        self,
        audio_path: str,
        *,
        threshold: float | None = None,
    ) -> str:
        return self.transcribe_detailed(
            audio_path, threshold=threshold
        ).text

    def transcribe_detailed(
        self,
        audio_path: str,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        self._load()

        import librosa

        audio, _ = librosa.load(audio_path, sr=16000, mono=True)
        return self._transcribe_samples_detailed(audio, 16000, threshold=threshold)

    def transcribe_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        *,
        threshold: float | None = None,
    ) -> str:
        """Direct PCM path — avoids a temp WAV round-trip for streaming."""
        return self.transcribe_audio_detailed(
            samples, sample_rate, threshold=threshold
        ).text

    def transcribe_audio_detailed(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        self._load()
        audio = _pcm_to_float32(samples)
        if sample_rate != 16000 and audio.size:
            import librosa

            audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
        return self._transcribe_samples_detailed(audio, 16000, threshold=threshold)

    def _transcribe_samples_detailed(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        import torch

        if not audio.size:
            return empty_transcription(skipped_reason="empty")

        inputs = self._processor(
            audio,
            sampling_rate=sample_rate,
            return_tensors="pt",
        )
        generate_kwargs = {
            **inputs,
            "max_new_tokens": settings.STT_MAX_NEW_TOKENS,
        }
        want_scores = bool(settings.STT_CONFIDENCE_FILTER)
        if want_scores:
            generate_kwargs["return_dict_in_generate"] = True
            generate_kwargs["output_scores"] = True

        with torch.no_grad():
            generated = self._model.generate(**generate_kwargs)

        if not want_scores:
            sequences = (
                generated.sequences
                if hasattr(generated, "sequences")
                else generated
            )
            raw = self._processor.batch_decode(
                sequences,
                skip_special_tokens=True,
            )[0].strip()
            return transcription_from_plain_text(raw, scored=False)

        sequences = generated.sequences
        raw = self._processor.batch_decode(
            sequences,
            skip_special_tokens=True,
        )[0].strip()
        scores = getattr(generated, "scores", None)
        if not scores:
            return filter_transcription(
                raw,
                None,
                threshold=threshold,
                sequence_confidence=0.0,
                scored=True,
            )

        transition = self._model.compute_transition_scores(
            sequences, scores, normalize_logits=True
        )
        token_probs = transition[0].exp().detach().cpu()
        gen_len = int(token_probs.shape[0])
        gen_ids = sequences[0, -gen_len:].detach().cpu().tolist()
        special_ids = _collect_special_ids(self._model, self._processor)

        content_ids: list[int] = []
        content_probs: list[float] = []
        for token_id, prob in zip(gen_ids, token_probs.tolist()):
            tid = int(token_id)
            if tid in special_ids:
                continue
            content_ids.append(tid)
            content_probs.append(float(prob))

        seq_conf = (
            (
                sum(calibrate_decoder_prob(p) for p in content_probs)
                / len(content_probs)
            )
            if content_probs
            else 0.0
        )
        surface = [part for part in raw.split() if part]
        confidences = self._word_confidences_for_surface(
            content_ids, content_probs, raw, surface
        )
        if confidences:
            confidences = [calibrate_decoder_prob(c) for c in confidences]
        result = filter_transcription(
            raw,
            confidences,
            threshold=threshold,
            sequence_confidence=seq_conf,
            scored=True,
        )
        if raw and not result.text:
            logger.info(
                "STT filtered empty skipped=%s seq=%.3f words=%s",
                result.skipped_reason,
                result.sequence_confidence,
                [(w.text, round(w.confidence, 3), w.kept) for w in result.words],
            )
        return result

    def _word_confidences_for_surface(
        self,
        content_ids: list[int],
        content_probs: list[float],
        raw: str,
        surface: list[str],
    ) -> list[float] | None:
        """Per-word min-token probs aligned to ``batch_decode`` whitespace words."""
        if not surface or not content_ids:
            return None
        tokenizer = getattr(self._processor, "tokenizer", self._processor)
        convert = getattr(tokenizer, "convert_ids_to_tokens", None)
        if convert is not None:
            token_strs = convert(content_ids)
            if isinstance(token_strs, str):
                token_strs = [token_strs]
            pairs = words_from_token_pieces(
                list(zip(token_strs, content_probs)), agg="mean"
            )
            if len(pairs) == len(surface):
                return [conf for _, conf in pairs]
        decode = getattr(tokenizer, "decode", None) or getattr(
            self._processor, "decode", None
        )
        if decode is None:
            return None
        pairs = align_token_probs_to_decoded_words(
            lambda ids: decode(ids, skip_special_tokens=True),
            content_ids,
            content_probs,
            surface_text=raw,
            agg="mean",
        )
        if len(pairs) == len(surface):
            return [conf for _, conf in pairs]
        return None


class MockSpeechRecognizer(SpeechRecognizer):
    """Deterministic recognizer for tests.

    ``transcript`` is used when the queue is empty. Pass ``transcripts`` for
    ordered replies across multiple STT calls in a stream session.

    When ``word_confidences`` is omitted, every token is 1.0 (existing tests).
    """

    def __init__(
        self,
        transcript: str = "",
        transcripts: Sequence[str] | None = None,
        word_confidences: Sequence[float] | None = None,
        sequence_confidence: float | None = None,
        skipped_reason: str | None = None,
    ):
        self.transcript = transcript
        self._queue = list(transcripts or [])
        self.word_confidences = (
            list(word_confidences) if word_confidences is not None else None
        )
        self.sequence_confidence = sequence_confidence
        self.skipped_reason = skipped_reason
        self.calls = 0

    def transcribe(
        self,
        audio_path: str,
        *,
        threshold: float | None = None,
    ) -> str:
        return self._next_detailed(threshold=threshold).text

    def transcribe_audio(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        *,
        threshold: float | None = None,
    ) -> str:
        return self._next_detailed(threshold=threshold).text

    def transcribe_detailed(
        self,
        audio_path: str,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        return self._next_detailed(threshold=threshold)

    def transcribe_audio_detailed(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        return self._next_detailed(threshold=threshold)

    def _next_detailed(self, *, threshold: float | None = None) -> Transcription:
        self.calls += 1
        raw = self._queue.pop(0) if self._queue else self.transcript
        scored = self.word_confidences is not None
        if self.skipped_reason:
            return empty_transcription(
                raw_text=raw or "",
                skipped_reason=self.skipped_reason,
                scored=scored,
            )
        return filter_transcription(
            raw,
            self.word_confidences,
            threshold=threshold,
            sequence_confidence=self.sequence_confidence,
            scored=scored,
        )
