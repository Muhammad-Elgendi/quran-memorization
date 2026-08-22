import tempfile
from pathlib import Path

import librosa
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import settings
from ..services.assessor import MemorizationAssessor
from ..services.audio import prepare_audio
from ..services.quran_service import QuranService
from ..services.speech_service import SpeechRecognizer, WhisperQuranRecognizer
from ..services.stt_confidence import (
    apply_ayah_recovery,
    recovery_debug_fields,
    stt_words_payload,
)

ALLOWED_SUFFIXES = {".webm", ".wav", ".ogg", ".mp3", ".m4a", ".flac"}


def create_router(
    quran_service: QuranService,
    recognizer: SpeechRecognizer | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/memorization", tags=["Memorization"])
    speech = recognizer or WhisperQuranRecognizer()

    @router.post("/assess")
    async def assess(
        surah: int = Form(...),
        ayah: int = Form(...),
        threshold: float = Form(None),
        audio: UploadFile = File(...),
    ):
        if threshold is None:
            threshold = settings.DEFAULT_THRESHOLD

        if not 0.5 <= threshold <= 1.0:
            raise HTTPException(
                status_code=400,
                detail="threshold must be between 0.5 and 1.0",
            )

        target = quran_service.get_ayah(surah, ayah)
        if not target:
            raise HTTPException(
                status_code=404,
                detail="Selected ayah does not exist",
            )

        suffix = Path(audio.filename or "recording.webm").suffix.lower()
        if suffix and suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported audio type: {suffix}",
            )
        if not suffix:
            suffix = ".webm"

        content = await audio.read()
        if not content:
            raise HTTPException(status_code=400, detail="Empty audio file")
        if len(content) > settings.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Audio exceeds max size of {settings.MAX_UPLOAD_BYTES} bytes",
            )

        temp_path: str | None = None
        analyzable_path: str | None = None
        owns_analyzable = False
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
                temp.write(content)
                temp_path = temp.name

            # Browser WebM/Opus is not readable by soundfile/librosa 1.x.
            try:
                analyzable_path, owns_analyzable = prepare_audio(temp_path)
                duration = librosa.get_duration(path=analyzable_path)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid audio file",
                ) from exc

            if duration < settings.MIN_AUDIO_SECONDS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Audio too short "
                        f"(min {settings.MIN_AUDIO_SECONDS}s)"
                    ),
                )
            if duration > settings.MAX_AUDIO_SECONDS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Audio too long "
                        f"(max {settings.MAX_AUDIO_SECONDS}s)"
                    ),
                )

            try:
                transcription = speech.transcribe_detailed(
                    analyzable_path, threshold=threshold
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Speech model unavailable",
                ) from exc

            transcription = apply_ayah_recovery(target["text"], transcription)
            assessor = MemorizationAssessor(threshold=threshold)
            result = assessor.assess(
                expected=target["text"],
                recognized=transcription.text or "",
            )

            payload = {
                "score": result.score,
                "passed": result.passed,
                "warning": result.warning,
                "expected": result.expected,
                "recognized": result.recognized,
                "missing_words": result.missing_words,
                "extra_words": result.extra_words,
                "wrong_words": result.wrong_words,
                "message": result.message,
                "alignment": result.alignment,
            }
            if transcription.scored and settings.STT_CONFIDENCE_FILTER:
                payload["sequence_confidence"] = round(
                    transcription.sequence_confidence, 4
                )
                payload["stt_words"] = stt_words_payload(transcription)
            payload.update(recovery_debug_fields(transcription))
            return payload
        finally:
            if owns_analyzable and analyzable_path:
                Path(analyzable_path).unlink(missing_ok=True)
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    return router
