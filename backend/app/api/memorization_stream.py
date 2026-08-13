"""WebSocket realtime memorization stream — Phase 2."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from ..services.quran_service import QuranService
from ..services.speech_service import MoonshineArabicRecognizer, SpeechRecognizer
from ..services.stream_session import SessionState, StreamSession

logger = logging.getLogger(__name__)

# Process-local active sessions (in-memory; sticky/single-replica for K8s).
_active_sessions = 0
_active_lock = asyncio.Lock()


async def _acquire_slot() -> bool:
    global _active_sessions
    async with _active_lock:
        if _active_sessions >= settings.STREAM_MAX_CONCURRENT_SESSIONS:
            return False
        _active_sessions += 1
        return True


async def _release_slot() -> None:
    global _active_sessions
    async with _active_lock:
        _active_sessions = max(0, _active_sessions - 1)


def create_router(
    quran_service: QuranService,
    recognizer: SpeechRecognizer | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/memorization", tags=["Memorization Stream"])
    speech = recognizer or MoonshineArabicRecognizer()

    @router.websocket("/stream")
    async def memorization_stream(websocket: WebSocket) -> None:
        await websocket.accept()

        if not await _acquire_slot():
            await _send(
                websocket,
                {
                    "type": "error",
                    "session_id": None,
                    "ts": None,
                    "code": "busy",
                    "message": (
                        f"Too many concurrent stream sessions "
                        f"(max {settings.STREAM_MAX_CONCURRENT_SESSIONS})"
                    ),
                    "fatal": True,
                },
            )
            await websocket.close(code=1013)
            return

        session: StreamSession | None = None
        summary_sent = False

        try:
            while True:
                if session is not None and session.state != SessionState.CLOSED:
                    timeouts = session.check_timeouts()
                    if timeouts:
                        for ev in timeouts:
                            await _send(websocket, ev)
                            if ev.get("type") == "session.summary":
                                summary_sent = True
                        break

                try:
                    message = await asyncio.wait_for(
                        websocket.receive(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                if message.get("type") == "websocket.disconnect":
                    break

                if "bytes" in message and message["bytes"] is not None:
                    raw = message["bytes"]
                    if session is None:
                        await _send(
                            websocket,
                            {
                                "type": "error",
                                "session_id": None,
                                "code": "not_ready",
                                "message": "Send session.start before audio",
                                "fatal": False,
                            },
                        )
                        continue
                    events = session.on_audio_chunk(raw)
                    for ev in events:
                        if ev.get("type") == "_assess_trigger":
                            assess_events = await asyncio.to_thread(
                                session.run_assess,
                                reason=ev.get("reason", "silence"),
                            )
                            for aev in assess_events:
                                await _send(websocket, aev)
                                if aev.get("type") == "session.summary":
                                    summary_sent = True
                            if session.state == SessionState.CLOSED:
                                return
                        else:
                            await _send(websocket, ev)

                    # Rare, gated partials (default off).
                    if session.config.partials and session.should_emit_partial():
                        partial_events = await asyncio.to_thread(session.run_partial)
                        for pev in partial_events:
                            await _send(websocket, pev)
                    continue

                text = message.get("text")
                if text is None:
                    continue

                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    await _send(
                        websocket,
                        {
                            "type": "error",
                            "session_id": session.session_id if session else None,
                            "code": "invalid_config",
                            "message": "Invalid JSON control frame",
                            "fatal": False,
                        },
                    )
                    continue

                msg_type = payload.get("type")

                if msg_type == "ping":
                    await _send(
                        websocket,
                        {
                            "type": "pong",
                            "session_id": session.session_id if session else None,
                            "ts": payload.get("ts"),
                        },
                    )
                    continue

                if msg_type == "session.start":
                    if session is not None:
                        await _send(
                            websocket,
                            session.error_event(
                                "invalid_config",
                                "Session already started",
                                fatal=False,
                            ),
                        )
                        continue
                    built, err = StreamSession.validate_and_build(
                        quran_service, speech, payload
                    )
                    if err:
                        await _send(websocket, err)
                        if err.get("fatal"):
                            return
                        continue
                    session = built
                    assert session is not None
                    await _send(websocket, session.ready_event())
                    continue

                if session is None:
                    await _send(
                        websocket,
                        {
                            "type": "error",
                            "session_id": None,
                            "code": "not_ready",
                            "message": "Send session.start first",
                            "fatal": False,
                        },
                    )
                    continue

                session.touch()

                if msg_type == "session.stop":
                    reason = payload.get("reason", "user")
                    mapped = {
                        "user": "user_stop",
                        "pagehide": "pagehide",
                        "error": "client_error",
                    }.get(reason, "user_stop")
                    await _send(websocket, session.summary_event(mapped))
                    summary_sent = True
                    return

                if msg_type == "ayah.force_assess":
                    events = await asyncio.to_thread(
                        session.run_assess, reason="force"
                    )
                    for ev in events:
                        await _send(websocket, ev)
                        if ev.get("type") == "session.summary":
                            summary_sent = True
                    if session.state == SessionState.CLOSED:
                        return
                    continue

                if msg_type == "ayah.force_advance":
                    events = session.force_advance(
                        reason=str(payload.get("reason", "skip"))
                    )
                    for ev in events:
                        await _send(websocket, ev)
                        if ev.get("type") == "session.summary":
                            summary_sent = True
                    if session.state == SessionState.CLOSED:
                        return
                    continue

                if msg_type in {"session.pause", "session.resume"}:
                    # Optional v1.1 — acknowledge lightly without heavy work.
                    if msg_type == "session.pause":
                        session.state = SessionState.PAUSED
                    elif session.state == SessionState.PAUSED:
                        session.state = SessionState.LISTENING
                    continue

                await _send(
                    websocket,
                    session.error_event(
                        "invalid_config",
                        f"Unknown message type: {msg_type}",
                        fatal=False,
                    ),
                )

        except WebSocketDisconnect:
            logger.info(
                "stream disconnected session=%s",
                session.session_id if session else None,
            )
        except Exception:
            logger.exception(
                "stream error session=%s",
                session.session_id if session else None,
            )
            if session is not None and not summary_sent:
                try:
                    await _send(
                        websocket,
                        session.error_event(
                            "internal",
                            "Unexpected server error",
                            fatal=True,
                        ),
                    )
                except Exception:
                    pass
        finally:
            if session is not None and not summary_sent and session.state != SessionState.CLOSED:
                try:
                    await _send(websocket, session.summary_event("disconnect"))
                except Exception:
                    pass
            await _release_slot()
            try:
                await websocket.close()
            except Exception:
                pass

    return router


async def _send(websocket: WebSocket, event: dict[str, Any]) -> None:
    if event.get("type") == "_assess_trigger":
        return
    # Drop None ts if we used a bare error before session existed.
    if event.get("ts") is None:
        from datetime import datetime, timezone

        event = {
            **event,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    await websocket.send_json(event)
