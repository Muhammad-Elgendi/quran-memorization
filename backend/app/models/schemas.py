from typing import Any, Optional

from pydantic import BaseModel, Field


class Ayah(BaseModel):
    number: int
    text: str


class SurahSummary(BaseModel):
    number: int
    name: str
    english_name: Optional[str] = None
    ayah_count: int


class Surah(BaseModel):
    number: int
    name: str
    english_name: Optional[str] = None
    ayahs: list[Ayah]


class WrongWord(BaseModel):
    expected: str
    recognized: str
    similarity: float


class AssessmentResponse(BaseModel):
    score: float
    passed: bool
    warning: bool
    expected: str
    recognized: str
    missing_words: list[str]
    extra_words: list[str]
    wrong_words: list[dict[str, Any]]
    message: str
    alignment: list[dict[str, Any]] = Field(default_factory=list)
    sequence_confidence: Optional[float] = None
    stt_words: Optional[list[dict[str, Any]]] = None
    raw_recognized: Optional[str] = None
    recovery: Optional[dict[str, Any]] = None


class HealthResponse(BaseModel):
    status: str
    service: str
