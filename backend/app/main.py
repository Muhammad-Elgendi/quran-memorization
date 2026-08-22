from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.memorization import create_router as create_memorization_router
from .api.memorization_stream import create_router as create_stream_router
from .api.quran import create_router as create_quran_router
from .config import QURAN_FILE, settings
from .services.quran_service import QuranService
from .services.speech_service import WhisperQuranRecognizer

quran_service = QuranService(QURAN_FILE)
# Share one recognizer instance so Whisper loads once for REST + WS.
speech_recognizer = WhisperQuranRecognizer()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load + warm Whisper before the first live partial (avoids ~12s cold generate).
    try:
        speech_recognizer._load()
    except Exception:
        pass
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Local Quran memorization assessment API. "
        "Client-agnostic: Vue now, Flutter or other clients later. "
        "REST /assess + WebSocket /stream."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(create_quran_router(quran_service))
app.include_router(
    create_memorization_router(quran_service, recognizer=speech_recognizer)
)
app.include_router(
    create_stream_router(quran_service, recognizer=speech_recognizer)
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": settings.APP_NAME,
    }
