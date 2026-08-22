from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Quran Memorization Assistant"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    QURAN_PATH: str = "data/quran.json"

    DEFAULT_THRESHOLD: float = 0.85
    WORD_MATCH_THRESHOLD: float = 0.75

    MIN_AUDIO_SECONDS: float = 0.5
    MAX_AUDIO_SECONDS: float = 20.0
    MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024

    STT_MODEL: str = "tarteel-ai/whisper-tiny-ar-quran"

    # Realtime WebSocket stream (Phase 2) — tuned for low CPU.
    STREAM_SILENCE_MS: int = 800
    STREAM_SHORT_SILENCE_MS: int = 400
    STREAM_MIN_UTTERANCE_MS: int = 400
    STREAM_PARTIAL_EVERY_MS: int = 2000
    # Periodic STT (partials + unified probe) only scores the last N ms so
    # Whisper latency stays bounded while the ring buffer grows.
    STREAM_PARTIAL_WINDOW_MS: int = 3000
    STREAM_COMPLETION_PROBE: bool = True
    STREAM_COMPLETION_PROBE_MS: int = 1000
    STREAM_COVERAGE_THRESHOLD: float = 0.85
    # Coverage probe must see ≥ this many consecutive high-coverage ticks
    # before auto-finalize (mid-utterance STT on short ayahs is unstable).
    STREAM_COVERAGE_STABLE_TICKS: int = 2
    # Multi-utterance word credit (Continuous): contiguous prefix across pauses.
    STREAM_MULTI_UTTERANCE_CREDIT: bool = True
    STREAM_CREDIT_KEEP_ON_FAIL: bool = True
    STREAM_CREDIT_CLEAR_ON_FAIL: bool = False
    STREAM_CREDIT_REQUIRE_CONTIGUOUS: bool = True
    STREAM_OVERLAP_MS: int = 300
    STREAM_IDLE_TIMEOUT_S: int = 60
    STREAM_MAX_SESSION_S: int = 1800
    STREAM_MAX_BUFFER_S: float = 45.0
    STREAM_MAX_FRAME_BYTES: int = 256 * 1024
    STREAM_MAX_CONCURRENT_SESSIONS: int = 2
    STREAM_PARTIALS_DEFAULT: bool = True
    STREAM_VAD_RMS_THRESHOLD: float = 0.015
    # STT energy gate (periodic + auto-assess). Lower than VAD so quiet mics
    # with AGC off / neural denoise still get transcribed.
    STREAM_STT_RMS_THRESHOLD: float = 0.008

    # STT confidence filter (Heard word-keep floor is Accuracy T / DEFAULT_THRESHOLD).
    STT_CONFIDENCE_FILTER: bool = True
    STT_SEQUENCE_CONFIDENCE_MIN: float = 0.50
    STT_MAX_NEW_TOKENS: int = 64
    STT_PARTIAL_MAX_OVERGEN_RATIO: float = 2.0
    # Decoder softmax → Accuracy slider. Moonshine lab used 0.12; Whisper Tiny
    # AR Quran L5 (2026-08-20): identity — gamma 0.12 inflated ~0.57 raw junk to
    # ~0.94 and kept hallucinations as Heard.
    STT_DECODER_PROB_GAMMA: float = 1.0
    # Ayah-constrained Heard recovery (Uthmani↔STT / agglutination / in-vocab revive).
    STT_AYAH_LEXICON_RECOVERY: bool = True
    STT_INVOCAB_FLOOR: float = 0.55

    # NoDecode: env values like "*" or "a,b" must not be JSON-parsed first.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["*"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value or value == "*":
                return ["*"]
            if value.startswith("["):
                import json

                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


settings = Settings()

# backend/ is the package root (parent of app/)
BACKEND_ROOT = Path(__file__).resolve().parent.parent
QURAN_FILE = (BACKEND_ROOT / settings.QURAN_PATH).resolve()
