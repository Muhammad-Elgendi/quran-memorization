"""WebSocket realtime memorization stream — Phase 2."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from ..services.quran_service import QuranService
from ..services.speech_service import SpeechRecognizer, WhisperQuranRecognizer
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
    speech = recognizer or WhisperQuranRecognizer()

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
        stt_task: asyncio.Task | None = None

        async def stt_worker() -> None:
            nonlocal summary_sent
            sess = session
            if sess is None:
                return
            try:
                while sess.state != SessionState.CLOSED:
                    job = sess.pop_pending_assess()
                    if job is not None:
                        events = await asyncio.to_thread(
                            sess.run_assess,
                            reason=str(job.get("reason") or "silence"),
                            recognized_hint=job.get("recognized"),
                        )
                    elif sess.should_run_periodic_stt():
                        events = await asyncio.to_thread(sess.run_periodic_stt)
                    else:
                        break
                    for ev in events:
                        if ev.get("type") == "_assess_trigger":
                            sess.push_pending_assess(ev)
                            continue
                        await _send(websocket, ev)
                        if ev.get("type") == "session.summary":
                            summary_sent = True
                    if sess.state == SessionState.CLOSED:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "stream STT worker failed session=%s",
                    sess.session_id,
                )

        def ensure_stt_worker() -> None:
            nonlocal stt_task
            if session is None or session.state == SessionState.CLOSED:
                return
            if stt_task is not None and not stt_task.done():
                return
            if not session.has_stt_work():
                return
            stt_task = asyncio.create_task(stt_worker())

        async def cancel_stt_worker() -> None:
            nonlocal stt_task
            if stt_task is None or stt_task.done():
                stt_task = None
                return
            stt_task.cancel()
            try:
                await stt_task
            except asyncio.CancelledError:
                pass
            stt_task = None

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
                    if session is not None:
                        ensure_stt_worker()
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
                            session.push_pending_assess(ev)
                        else:
                            await _send(websocket, ev)
                    ensure_stt_worker()
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
                    await cancel_stt_worker()
                    await _send(websocket, session.summary_event(mapped))
                    summary_sent = True
                    return

                if msg_type == "ayah.force_assess":
                    session.push_pending_assess(
                        {"type": "_assess_trigger", "reason": "force"}
                    )
                    ensure_stt_worker()
                    if stt_task is not None and not stt_task.done():
                        await stt_task
                    ensure_stt_worker()
                    if stt_task is not None and not stt_task.done():
                        await stt_task
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
            await cancel_stt_worker()
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
