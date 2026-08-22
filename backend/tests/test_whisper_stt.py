"""STT model swap — decode hygiene, 30 s clamp, generate kwargs (no HF download)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.services.speech_service import (
    WHISPER_MAX_SAMPLES,
    WhisperQuranRecognizer,
    _collect_special_ids,
    _is_special_decoder_id,
    _pcm_to_float32,
    align_whisper_generation_config,
    clamp_whisper_audio,
    strip_decoder_special_tokens,
)
from prefetch_model import DEFAULT_MODEL, repo_cache_dir


TARTEEL_REPO = "tarteel-ai/whisper-tiny-ar-quran"


# --- T-P prefetch / config ---


def test_tp1_default_model_is_tarteel():
    assert DEFAULT_MODEL == TARTEEL_REPO
    assert settings.STT_MODEL == TARTEEL_REPO


def test_tp2_repo_cache_dir_uses_tarteel_key():
    path = repo_cache_dir(TARTEEL_REPO)
    assert path.name == "models--tarteel-ai--whisper-tiny-ar-quran"


# --- T-S decode hygiene ---


def test_ts1_strips_whisper_prefix_keeps_arabic():
    raw = "<|startoftranscript|><|ar|><|transcribe|><|notimestamps|>بسم الله الرحمن الرحيم"
    assert strip_decoder_special_tokens(raw) == "بسم الله الرحمن الرحيم"


def test_ts2_strips_timestamp_tokens():
    assert strip_decoder_special_tokens("<|0.00|>الحمد<|0.64|>") == "الحمد"


def test_ts3_empty_after_strip():
    assert strip_decoder_special_tokens("<|ar|><|transcribe|>") == ""
    assert strip_decoder_special_tokens("   ") == ""
    assert strip_decoder_special_tokens("") == ""


def test_ts4_ordinary_arabic_unchanged():
    assert strip_decoder_special_tokens("الحمد لله") == "الحمد لله"


def test_ts5_collect_special_ids_and_timestamp_begin():
    tokenizer = SimpleNamespace(
        all_special_ids=[50257],
        additional_special_tokens_ids=[50259, 50358],
        timestamp_begin=50363,
        pad_token_id=50257,
        eos_token_id=50257,
        bos_token_id=50258,
        decoder_start_token_id=50258,
    )
    processor = SimpleNamespace(tokenizer=tokenizer)
    model = SimpleNamespace(
        config=SimpleNamespace(
            bos_token_id=50258,
            eos_token_id=50257,
            pad_token_id=50257,
            decoder_start_token_id=50258,
        )
    )
    special_ids = _collect_special_ids(model, processor)
    assert 50257 in special_ids
    assert 50258 in special_ids
    assert 50259 in special_ids
    assert 50358 in special_ids

    gen_ids = [50258, 50259, 100, 50363, 50364, 101, 50257]
    probs = [0.9] * len(gen_ids)
    content = [
        tid
        for tid, _ in zip(gen_ids, probs)
        if not _is_special_decoder_id(tid, special_ids, tokenizer)
    ]
    assert content == [100, 101]


# --- T-W window clamp ---


def test_tw1_short_audio_unchanged():
    audio = np.arange(100, dtype=np.float32)
    out = clamp_whisper_audio(audio)
    assert out.size == 100
    np.testing.assert_array_equal(out, audio)


def test_tw2_long_audio_keeps_tail_not_head():
    audio = np.arange(WHISPER_MAX_SAMPLES + 500, dtype=np.float32)
    out = clamp_whisper_audio(audio)
    assert out.size == WHISPER_MAX_SAMPLES
    np.testing.assert_array_equal(out, audio[-WHISPER_MAX_SAMPLES:])
    assert out[0] != audio[0]
    assert out[-1] == audio[-1]


def test_tw3_stereo_int16_goes_through_pcm_then_clamp():
    n = WHISPER_MAX_SAMPLES + 1000
    stereo = np.zeros((n, 2), dtype=np.int16)
    stereo[0] = [1000, 1000]
    stereo[-1] = [32000, 32000]
    audio = _pcm_to_float32(stereo)
    clamped = clamp_whisper_audio(audio)
    assert clamped.size == WHISPER_MAX_SAMPLES
    assert abs(clamped[-1]) > abs(clamped[0])


# --- T-G generate kwargs (fake model) ---


class _DummyProcessor:
    def __init__(self, leaked: str = "<|startoftranscript|><|ar|>بسم الله"):
        self.leaked = leaked
        self.decode_kwargs = None
        self.tokenizer = SimpleNamespace(
            all_special_ids=[0],
            additional_special_tokens_ids=[],
            convert_ids_to_tokens=None,
            decode=None,
        )

    def __call__(self, audio, sampling_rate=16000, return_tensors="pt"):
        features = np.asarray(audio, dtype=np.float32)
        return SimpleNamespace(
            input_features=features,
            attention_mask="MUST_NOT_REACH_GENERATE",
        )

    def batch_decode(self, sequences, skip_special_tokens=False, **kwargs):
        self.decode_kwargs = {
            "skip_special_tokens": skip_special_tokens,
            **kwargs,
        }
        return [self.leaked]


class _DummyModel:
    def __init__(self):
        self.calls: list[dict] = []
        self.config = SimpleNamespace(
            pad_token_id=0,
            eos_token_id=0,
            bos_token_id=1,
            decoder_start_token_id=1,
        )

    def generate(self, input_features=None, **kwargs):
        self.calls.append({"input_features": input_features, "kwargs": dict(kwargs)})
        sequences = np.array([[1, 10, 11, 0]])
        return SimpleNamespace(sequences=sequences, scores=None)


def _wired_recognizer(monkeypatch, *, confidence_filter: bool, leaked: str | None = None):
    rec = WhisperQuranRecognizer()
    processor = _DummyProcessor(leaked or "<|startoftranscript|><|ar|>بسم الله")
    model = _DummyModel()

    def fake_load() -> None:
        rec._processor = processor
        rec._model = model

    rec._load = fake_load
    rec._processor = processor
    rec._model = model
    monkeypatch.setattr(settings, "STT_CONFIDENCE_FILTER", confidence_filter)
    return rec, processor, model


def test_tg1_generate_features_not_attention_mask(monkeypatch):
    rec, _, model = _wired_recognizer(monkeypatch, confidence_filter=False)
    rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["input_features"] is not None
    assert "attention_mask" not in call["kwargs"]


def test_tg2_arabic_transcribe(monkeypatch):
    rec, _, model = _wired_recognizer(monkeypatch, confidence_filter=False)
    rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    kwargs = model.calls[0]["kwargs"]
    assert kwargs.get("language") == "ar"
    assert kwargs.get("task") == "transcribe"
    assert "forced_decoder_ids" not in kwargs


def test_tg3_max_new_tokens_and_score_flags_follow_filter(monkeypatch):
    rec_off, _, model_off = _wired_recognizer(monkeypatch, confidence_filter=False)
    rec_off.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    off = model_off.calls[0]["kwargs"]
    assert off["max_new_tokens"] == 64
    assert "output_scores" not in off
    assert "return_dict_in_generate" not in off

    rec_on, _, model_on = _wired_recognizer(monkeypatch, confidence_filter=True)
    rec_on.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    on = model_on.calls[0]["kwargs"]
    assert on["max_new_tokens"] == 64
    assert on["output_scores"] is True
    assert on["return_dict_in_generate"] is True


def test_tg4_batch_decode_skip_specials_then_sanitizer(monkeypatch):
    rec, processor, _ = _wired_recognizer(monkeypatch, confidence_filter=False)
    result = rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    assert processor.decode_kwargs["skip_special_tokens"] is True
    assert "<|" not in result.text
    assert "<|" not in result.raw_text
    assert result.text == "بسم الله"


def test_tg4_sanitizer_runs_when_confidence_filter_on(monkeypatch):
    rec, processor, _ = _wired_recognizer(monkeypatch, confidence_filter=True)
    result = rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    assert processor.decode_kwargs["skip_special_tokens"] is True
    assert "<|" not in (result.raw_text or "")
    assert "<|" not in (result.text or "")


def test_align_whisper_generation_config_from_tokenizer():
    class _Gen:
        forced_decoder_ids = [(1, 50259)]
        max_length = 448

    class _Tokenizer:
        added_tokens_decoder = {
            50259: SimpleNamespace(content="<|en|>"),
            50272: SimpleNamespace(content="<|ar|>"),
            50358: SimpleNamespace(content="<|translate|>"),
            50359: SimpleNamespace(content="<|transcribe|>"),
            50363: SimpleNamespace(content="<|notimestamps|>"),
        }

        def convert_tokens_to_ids(self, token):
            return {
                "<|en|>": 50259,
                "<|ar|>": 50272,
                "<|translate|>": 50358,
                "<|transcribe|>": 50359,
                "<|notimestamps|>": 50363,
            }.get(token)

    gen = _Gen()
    model = SimpleNamespace(generation_config=gen)
    processor = SimpleNamespace(tokenizer=_Tokenizer())
    align_whisper_generation_config(model, processor)
    assert gen.lang_to_id["<|ar|>"] == 50272
    assert gen.lang_to_id["<|en|>"] == 50259
    assert gen.task_to_id["transcribe"] == 50359
    assert gen.task_to_id["translate"] == 50358
    assert gen.is_multilingual is True
    assert gen.no_timestamps_token_id == 50363
    assert gen.language == "ar"
    assert gen.task == "transcribe"
    assert gen.forced_decoder_ids is None
    assert gen.max_length is None


def test_align_whisper_generation_config_skips_language_without_maps():
    class _Gen:
        forced_decoder_ids = [(1, 50259)]
        max_length = 448

    gen = _Gen()
    model = SimpleNamespace(generation_config=gen)
    processor = SimpleNamespace(tokenizer=SimpleNamespace())
    align_whisper_generation_config(model, processor)
    assert not hasattr(gen, "language")
    assert not hasattr(gen, "task")
    assert gen.forced_decoder_ids is None
    assert gen.max_length is None


def test_generate_forced_decoder_ids_fallback(monkeypatch):
    rec, processor, _ = _wired_recognizer(monkeypatch, confidence_filter=False)

    class _StrictModel(_DummyModel):
        def generate(self, input_features=None, **kwargs):
            if "language" in kwargs or "task" in kwargs:
                raise TypeError("unexpected kwargs")
            return super().generate(input_features, **kwargs)

    processor.get_decoder_prompt_ids = lambda language, task: [(1, 50259)]
    model = _StrictModel()
    rec._model = model
    rec._processor = processor

    def fake_load() -> None:
        rec._processor = processor
        rec._model = model

    rec._load = fake_load
    rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    kwargs = model.calls[0]["kwargs"]
    assert "language" not in kwargs
    assert "task" not in kwargs
    assert kwargs["forced_decoder_ids"] == [(1, 50259)]
    assert kwargs["max_new_tokens"] == 64


def test_generate_outdated_config_valueerror_fallback(monkeypatch):
    rec, processor, _ = _wired_recognizer(monkeypatch, confidence_filter=False)

    class _OutdatedModel(_DummyModel):
        def generate(self, input_features=None, **kwargs):
            if "language" in kwargs or "task" in kwargs:
                raise ValueError(
                    "The generation config is outdated and is thus not compatible "
                    "with the `language` argument to `generate`"
                )
            return super().generate(input_features, **kwargs)

    processor.get_decoder_prompt_ids = lambda language, task: [(1, 50272)]
    model = _OutdatedModel()
    rec._model = model
    rec._processor = processor

    def fake_load() -> None:
        rec._processor = processor
        rec._model = model

    rec._load = fake_load
    rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
    kwargs = model.calls[0]["kwargs"]
    assert "language" not in kwargs
    assert kwargs["forced_decoder_ids"] == [(1, 50272)]


def test_generate_other_valueerror_is_not_swallowed(monkeypatch):
    rec, processor, _ = _wired_recognizer(monkeypatch, confidence_filter=False)

    class _BrokenModel(_DummyModel):
        def generate(self, input_features=None, **kwargs):
            raise ValueError("tensor size mismatch")

    model = _BrokenModel()
    rec._model = model
    rec._processor = processor
    rec._load = lambda: None

    with pytest.raises(ValueError, match="tensor size mismatch"):
        rec.transcribe_audio_detailed(np.zeros(1600, dtype=np.float32), 16000)
