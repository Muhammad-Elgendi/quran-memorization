"""Speech recognition behind a swappable interface."""

from __future__ import annotations

import logging
import re
import tempfile
from abc import ABC, abstractmethod
from contextlib import nullcontext
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

WHISPER_SAMPLE_RATE = 16000
WHISPER_MAX_SECONDS = 30.0
WHISPER_MAX_SAMPLES = int(WHISPER_MAX_SECONDS * WHISPER_SAMPLE_RATE)

_WHISPER_ANGLE_TOKEN = re.compile(r"<\|[^|>]*\|>")
_WHISPER_LANG_TOKEN = re.compile(r"^<\|[a-z]{2,3}\|>$")
_OUTDATED_GENERATION_CONFIG = "generation config is outdated"


def strip_decoder_special_tokens(text: str) -> str:
    """Remove Whisper control / timestamp tokens that leak into decode text."""
    cleaned = _WHISPER_ANGLE_TOKEN.sub(" ", text or "")
    return " ".join(cleaned.split())


def clamp_whisper_audio(
    audio: np.ndarray, sr: int = WHISPER_SAMPLE_RATE
) -> np.ndarray:
    """Keep the last 30 s. Whisper's feature extractor would otherwise drop the tail."""
    samples = np.asarray(audio)
    max_samples = int(WHISPER_MAX_SECONDS * sr) if sr else WHISPER_MAX_SAMPLES
    if samples.size > max_samples:
        return samples[-max_samples:]
    return samples


def _token_content(value) -> str:
    if isinstance(value, str):
        return value
    return str(getattr(value, "content", value))


def _whisper_token_id(tokenizer, token: str) -> int | None:
    convert = getattr(tokenizer, "convert_tokens_to_ids", None)
    if convert is None:
        return None
    tid = convert(token)
    if tid is None:
        return None
    unk = getattr(tokenizer, "unk_token_id", None)
    if unk is not None and int(tid) == int(unk):
        return None
    return int(tid)


def _whisper_lang_to_id(tokenizer) -> dict[str, int]:
    mapping: dict[str, int] = {}
    decoder = getattr(tokenizer, "added_tokens_decoder", None)
    if isinstance(decoder, dict):
        for tid, token in decoder.items():
            content = _token_content(token)
            if _WHISPER_LANG_TOKEN.match(content):
                mapping[content] = int(tid)
    if "<|ar|>" not in mapping:
        arabic = _whisper_token_id(tokenizer, "<|ar|>")
        if arabic is not None:
            mapping["<|ar|>"] = arabic
    return mapping


def align_whisper_generation_config(model, processor) -> None:
    """Fill a 4.26-era Whisper config so ``generate(language=, task=)`` works.

    ``tarteel-ai/whisper-tiny-ar-quran`` ships no ``generation_config.json``.
    Transformers then synthesizes a skeleton without ``lang_to_id`` /
    ``task_to_id``, and ``generate(language="ar")`` raises ValueError.
    Token ids come from the already-loaded tokenizer (no extra Hub fetch).
    """
    gen = getattr(model, "generation_config", None)
    if gen is None:
        return
    tokenizer = getattr(processor, "tokenizer", processor)

    if not getattr(gen, "lang_to_id", None):
        lang_to_id = _whisper_lang_to_id(tokenizer)
        if lang_to_id:
            gen.lang_to_id = lang_to_id
            gen.is_multilingual = True

    if not getattr(gen, "task_to_id", None):
        task_to_id: dict[str, int] = {}
        transcribe = _whisper_token_id(tokenizer, "<|transcribe|>")
        translate = _whisper_token_id(tokenizer, "<|translate|>")
        if transcribe is not None:
            task_to_id["transcribe"] = transcribe
        if translate is not None:
            task_to_id["translate"] = translate
        if task_to_id:
            gen.task_to_id = task_to_id

    no_ts = _whisper_token_id(tokenizer, "<|notimestamps|>")
    if no_ts is not None and getattr(gen, "no_timestamps_token_id", None) is None:
        gen.no_timestamps_token_id = no_ts

    gen.forced_decoder_ids = None
    if getattr(gen, "max_length", None) is not None:
        gen.max_length = None
    # Pin language/task only after maps exist. Setting them on a skeleton
    # config makes generate(language=) raise, and the forced-ids fallback
    # still reads generation_config.language and needs lang_to_id.
    if getattr(gen, "lang_to_id", None) and getattr(gen, "task_to_id", None):
        gen.language = "ar"
        gen.task = "transcribe"


def _collect_special_ids(model, processor) -> set[int]:
    ids: set[int] = set()
    tokenizer = getattr(processor, "tokenizer", processor)
    special = getattr(tokenizer, "all_special_ids", None)
    if special:
        ids.update(int(i) for i in special)
    extra = getattr(tokenizer, "additional_special_tokens_ids", None)
    if extra:
        ids.update(int(i) for i in extra if i is not None)
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


def _is_special_decoder_id(
    token_id: int, special_ids: set[int], tokenizer
) -> bool:
    tid = int(token_id)
    if tid in special_ids:
        return True
    timestamp_begin = getattr(tokenizer, "timestamp_begin", None)
    if timestamp_begin is not None and tid >= int(timestamp_begin):
        return True
    return False


def _inference_context():
    try:
        import torch

        return torch.no_grad()
    except ImportError:
        return nullcontext()


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


class WhisperQuranRecognizer(SpeechRecognizer):
    """Tarteel Whisper Tiny AR Quran via Hugging Face Transformers."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.STT_MODEL
        self._model = None
        self._processor = None

    def _load(self) -> None:
        if self._model is not None:
            return

        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        self._processor = AutoProcessor.from_pretrained(self.model_name)
        self._model = AutoModelForSpeechSeq2Seq.from_pretrained(self.model_name)
        self._model.eval()
        align_whisper_generation_config(self._model, self._processor)

        logger.info("STT loaded %s", self.model_name)

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

    def _input_features(self, audio: np.ndarray, sample_rate: int):
        features = self._processor(
            audio,
            sampling_rate=sample_rate,
            return_tensors="pt",
        )
        if hasattr(features, "input_features"):
            return features.input_features
        return features["input_features"]

    def _generate(self, input_features, generate_kwargs: dict):
        try:
            return self._model.generate(input_features, **generate_kwargs)
        except (TypeError, ValueError) as exc:
            if (
                isinstance(exc, ValueError)
                and _OUTDATED_GENERATION_CONFIG not in str(exc).lower()
            ):
                raise
            fallback = {
                key: value
                for key, value in generate_kwargs.items()
                if key not in ("language", "task")
            }
            forced = self._processor.get_decoder_prompt_ids(
                language="ar", task="transcribe"
            )
            return self._model.generate(
                input_features,
                forced_decoder_ids=forced,
                **fallback,
            )

    def _batch_decode(self, sequences) -> str:
        raw = self._processor.batch_decode(
            sequences,
            skip_special_tokens=True,
        )[0].strip()
        return strip_decoder_special_tokens(raw)

    def _transcribe_samples_detailed(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        threshold: float | None = None,
    ) -> Transcription:
        if not audio.size:
            return empty_transcription(skipped_reason="empty")

        audio = clamp_whisper_audio(audio, sample_rate)
        input_features = self._input_features(audio, sample_rate)

        generate_kwargs: dict = {
            "max_new_tokens": settings.STT_MAX_NEW_TOKENS,
            "language": "ar",
            "task": "transcribe",
            "do_sample": False,
        }
        want_scores = bool(settings.STT_CONFIDENCE_FILTER)
        if want_scores:
            generate_kwargs["return_dict_in_generate"] = True
            generate_kwargs["output_scores"] = True

        with _inference_context():
            generated = self._generate(input_features, generate_kwargs)

        if not want_scores:
            sequences = (
                generated.sequences
                if hasattr(generated, "sequences")
                else generated
            )
            raw = self._batch_decode(sequences)
            return transcription_from_plain_text(raw, scored=False)

        sequences = generated.sequences
        raw = self._batch_decode(sequences)
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
        tokenizer = getattr(self._processor, "tokenizer", self._processor)
        special_ids = _collect_special_ids(self._model, self._processor)

        content_ids: list[int] = []
        content_probs: list[float] = []
        for token_id, prob in zip(gen_ids, token_probs.tolist()):
            if _is_special_decoder_id(token_id, special_ids, tokenizer):
                continue
            content_ids.append(int(token_id))
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
        """Per-word mean-token probs aligned to ``batch_decode`` whitespace words."""
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
