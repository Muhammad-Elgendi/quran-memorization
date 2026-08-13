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

    MOONSHINE_MODEL: str = "UsefulSensors/moonshine-tiny-ar"

    # Realtime WebSocket stream (Phase 2) — tuned for low CPU.
    STREAM_SILENCE_MS: int = 800
    STREAM_MIN_UTTERANCE_MS: int = 400
    STREAM_PARTIAL_EVERY_MS: int = 2000
    STREAM_COVERAGE_THRESHOLD: float = 0.85
    STREAM_OVERLAP_MS: int = 300
    STREAM_IDLE_TIMEOUT_S: int = 60
    STREAM_MAX_SESSION_S: int = 1800
    STREAM_MAX_BUFFER_S: float = 45.0
    STREAM_MAX_FRAME_BYTES: int = 256 * 1024
    STREAM_MAX_CONCURRENT_SESSIONS: int = 2
    STREAM_PARTIALS_DEFAULT: bool = False
    STREAM_VAD_RMS_THRESHOLD: float = 0.015

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
