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
