#!/usr/bin/env python3
"""Stress / latency benchmarks for Quran Memorization API.

Focus: WS /api/memorization/stream, plus REST /health and /assess smoke load.

Usage:
  python scripts/ws_stress_bench.py [--base http://127.0.0.1:8000] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import struct
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

SR = 16000


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def summarize(values: list[float], unit: str = "ms") -> dict[str, Any]:
    if not values:
        return {"n": 0, "unit": unit}
    return {
        "n": len(values),
        "unit": unit,
        "min": round(min(values), 2),
        "mean": round(statistics.mean(values), 2),
        "p50": round(pct(values, 50) or 0, 2),
        "p95": round(pct(values, 95) or 0, 2),
        "p99": round(pct(values, 99) or 0, 2),
        "max": round(max(values), 2),
    }


def pcm_tone(ms: int, amp: float = 0.25, freq: float = 440.0) -> bytes:
    n = int(SR * ms / 1000)
    out = bytearray(n * 2)
    for i in range(n):
        sample = int(max(-1.0, min(1.0, amp * math.sin(2 * math.pi * freq * i / SR))) * 32767)
        struct.pack_into("<h", out, i * 2, sample)
    return bytes(out)


def pcm_silence(ms: int) -> bytes:
    return b"\x00\x00" * int(SR * ms / 1000)


def wav_bytes_from_pcm(pcm: bytes, sr: int = SR) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def ws_url(base: str) -> str:
    base = base.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/api/memorization/stream"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/api/memorization/stream"
    return base + "/api/memorization/stream"


START_MSG = {
    "type": "session.start",
    "start_surah": 1,
    "start_ayah": 1,
    "end_surah": 1,
    "end_ayah": 3,
    "threshold": 0.5,
    "fail_policy": "continue",
    "cross_surah": False,
    "partials": False,
    "auto_advance": True,
    "audio": {
        "format": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
        "chunk_ms": 250,
    },
}


async def recv_until(
    ws: Any,
    wanted: set[str],
    timeout_s: float = 30.0,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], float]:
    """Receive until first message whose type is in wanted. Returns (msg, all, elapsed_ms)."""
    t0 = time.perf_counter()
    collected: list[dict[str, Any]] = []
    deadline = t0 + timeout_s
    while time.perf_counter() < deadline:
        remaining = deadline - time.perf_counter()
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, remaining))
        if isinstance(raw, (bytes, bytearray)):
            continue
        msg = json.loads(raw)
        collected.append(msg)
        if msg.get("type") in wanted:
            return msg, collected, (time.perf_counter() - t0) * 1000
        if msg.get("type") == "error" and msg.get("fatal"):
            return msg, collected, (time.perf_counter() - t0) * 1000
    return None, collected, (time.perf_counter() - t0) * 1000


# --- Scenarios ------------------------------------------------------------


async def scenario_health(base: str, n: int = 50) -> ScenarioResult:
    latencies: list[float] = []
    errors: list[str] = []
    async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = await client.get("/health")
                latencies.append((time.perf_counter() - t0) * 1000)
                if r.status_code != 200:
                    errors.append(f"status={r.status_code}")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
    return ScenarioResult(
        name="REST /health",
        ok=len(errors) == 0,
        detail=f"{n} sequential GETs",
        metrics={"latency": summarize(latencies), "errors": len(errors)},
        errors=errors[:5],
    )


async def scenario_ws_connect_ready(base: str, n: int = 30) -> ScenarioResult:
    url = ws_url(base)
    connect_ms: list[float] = []
    ready_ms: list[float] = []
    errors: list[str] = []
    for i in range(n):
        t0 = time.perf_counter()
        try:
            async with websockets.connect(url, open_timeout=10, max_size=8 * 1024 * 1024) as ws:
                connect_ms.append((time.perf_counter() - t0) * 1000)
                t1 = time.perf_counter()
                await ws.send(json.dumps(START_MSG))
                msg, _, _ = await recv_until(ws, {"session.ready", "error"}, timeout_s=15)
                ready_ms.append((time.perf_counter() - t1) * 1000)
                if not msg or msg.get("type") != "session.ready":
                    errors.append(f"iter{i}: got {msg}")
                await ws.send(json.dumps({"type": "session.stop", "reason": "user"}))
                await recv_until(ws, {"session.summary", "error"}, timeout_s=10)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"iter{i}: {exc}")
        await asyncio.sleep(0.05)  # let server release session slot
    return ScenarioResult(
        name="WS connect + session.ready (serial)",
        ok=len(errors) == 0 and len(ready_ms) >= max(1, n - 1),
        detail=f"{n} serial sessions (partials=false)",
        metrics={
            "connect_latency": summarize(connect_ms),
            "ready_latency": summarize(ready_ms),
            "errors": len(errors),
        },
        errors=errors[:5],
    )


async def scenario_ws_ping_pong(base: str, n: int = 200) -> ScenarioResult:
    url = ws_url(base)
    rtt: list[float] = []
    errors: list[str] = []
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            await ws.send(json.dumps(START_MSG))
            ready, _, _ = await recv_until(ws, {"session.ready", "error"}, timeout_s=15)
            if not ready or ready.get("type") != "session.ready":
                return ScenarioResult(
                    name="WS ping/pong RTT",
                    ok=False,
                    detail="failed to start session",
                    errors=[str(ready)],
                )
            for i in range(n):
                t0 = time.perf_counter()
                await ws.send(json.dumps({"type": "ping", "ts": i}))
                msg, _, _ = await recv_until(ws, {"pong", "error"}, timeout_s=5)
                rtt.append((time.perf_counter() - t0) * 1000)
                if not msg or msg.get("type") != "pong":
                    errors.append(f"ping{i}: {msg}")
            await ws.send(json.dumps({"type": "session.stop", "reason": "user"}))
            await recv_until(ws, {"session.summary"}, timeout_s=10)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
    return ScenarioResult(
        name="WS ping/pong RTT",
        ok=len(errors) == 0 and len(rtt) == n,
        detail=f"{n} pings on one live session",
        metrics={"rtt": summarize(rtt), "errors": len(errors)},
        errors=errors[:5],
    )


async def scenario_ws_audio_ingest(base: str, duration_s: float = 10.0, chunk_ms: int = 250) -> ScenarioResult:
    url = ws_url(base)
    chunk = pcm_tone(chunk_ms, amp=0.2)
    send_latencies: list[float] = []
    bytes_sent = 0
    errors: list[str] = []
    t_start = time.perf_counter()
    try:
        async with websockets.connect(url, open_timeout=10, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({**START_MSG, "partials": False}))
            ready, _, _ = await recv_until(ws, {"session.ready", "error"}, timeout_s=15)
            if not ready or ready.get("type") != "session.ready":
                return ScenarioResult(
                    name="WS PCM ingest throughput",
                    ok=False,
                    detail="session.start failed",
                    errors=[str(ready)],
                )
            n_chunks = int(duration_s * 1000 / chunk_ms)
            for i in range(n_chunks):
                t0 = time.perf_counter()
                await ws.send(chunk)
                send_latencies.append((time.perf_counter() - t0) * 1000)
                bytes_sent += len(chunk)
                # Real-time pacing
                target = t_start + (i + 1) * (chunk_ms / 1000.0)
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            elapsed = time.perf_counter() - t_start
            await ws.send(json.dumps({"type": "session.stop", "reason": "user"}))
            await recv_until(ws, {"session.summary", "error"}, timeout_s=15)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        elapsed = time.perf_counter() - t_start
    mbps = (bytes_sent * 8 / 1e6) / elapsed if elapsed > 0 else 0
    return ScenarioResult(
        name="WS PCM ingest throughput",
        ok=len(errors) == 0,
        detail=f"{duration_s:.0f}s realtime tone @ {chunk_ms}ms chunks",
        metrics={
            "bytes_sent": bytes_sent,
            "elapsed_s": round(elapsed, 2),
            "audio_bitrate_kbps": round(mbps * 1000, 1),
            "send_latency": summarize(send_latencies),
            "errors": len(errors),
        },
        errors=errors[:5],
    )


async def scenario_ws_force_assess(base: str, n: int = 8) -> ScenarioResult:
    """STT-heavy path: tone + ayah.force_assess → ayah.result."""
    url = ws_url(base)
    latencies: list[float] = []
    errors: list[str] = []
    for i in range(n):
        try:
            async with websockets.connect(url, open_timeout=15, max_size=8 * 1024 * 1024) as ws:
                await ws.send(json.dumps({**START_MSG, "partials": False}))
                ready, _, _ = await recv_until(ws, {"session.ready", "error"}, timeout_s=20)
                if not ready or ready.get("type") != "session.ready":
                    errors.append(f"iter{i}: start {ready}")
                    continue
                # ~1.5s speech-like energy so force_assess has enough buffer
                for _ in range(6):
                    await ws.send(pcm_tone(250, amp=0.3))
                    await asyncio.sleep(0.05)
                t0 = time.perf_counter()
                await ws.send(json.dumps({"type": "ayah.force_assess"}))
                msg, collected, _ = await recv_until(
                    ws, {"ayah.result", "error", "session.summary"}, timeout_s=60
                )
                latencies.append((time.perf_counter() - t0) * 1000)
                if not msg or msg.get("type") not in {"ayah.result", "error"}:
                    errors.append(f"iter{i}: unexpected {[c.get('type') for c in collected]}")
                elif msg.get("type") == "error":
                    # no_speech / busy still counts as completed round-trip
                    if msg.get("code") not in {"no_speech", "busy"}:
                        errors.append(f"iter{i}: {msg.get('code')} {msg.get('message')}")
                await ws.send(json.dumps({"type": "session.stop", "reason": "user"}))
                try:
                    await recv_until(ws, {"session.summary"}, timeout_s=15)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            errors.append(f"iter{i}: {exc}")
        await asyncio.sleep(0.2)
    return ScenarioResult(
        name="WS force_assess (STT) latency",
        ok=len(latencies) >= max(1, n // 2),
        detail=f"{n} serial force_assess with ~1.5s PCM tone (real Whisper)",
        metrics={"force_assess_to_result": summarize(latencies), "errors": len(errors)},
        errors=errors[:8],
    )


async def _one_concurrent_session(
    url: str,
    hold_s: float,
    with_audio: bool,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    out: dict[str, Any] = {
        "ok": False,
        "busy": False,
        "connect_ms": None,
        "ready_ms": None,
        "error": None,
    }
    try:
        async with websockets.connect(url, open_timeout=15, max_size=8 * 1024 * 1024) as ws:
            out["connect_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            t1 = time.perf_counter()
            await ws.send(json.dumps({**START_MSG, "partials": False}))
            msg, _, _ = await recv_until(ws, {"session.ready", "error"}, timeout_s=20)
            out["ready_ms"] = round((time.perf_counter() - t1) * 1000, 2)
            if msg and msg.get("type") == "error" and msg.get("code") == "busy":
                out["busy"] = True
                out["ok"] = True  # expected under overload
                return out
            if not msg or msg.get("type") != "session.ready":
                out["error"] = str(msg)
                return out
            out["ok"] = True
            end = time.perf_counter() + hold_s
            while time.perf_counter() < end:
                if with_audio:
                    await ws.send(pcm_tone(200, amp=0.2))
                else:
                    await ws.send(json.dumps({"type": "ping", "ts": time.time()}))
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                await asyncio.sleep(0.2)
            await ws.send(json.dumps({"type": "session.stop", "reason": "user"}))
            try:
                await recv_until(ws, {"session.summary"}, timeout_s=10)
            except Exception:  # noqa: BLE001
                pass
    except ConnectionClosed as exc:
        out["error"] = f"closed {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


async def scenario_ws_concurrency(
    base: str,
    clients: int,
    hold_s: float = 3.0,
    with_audio: bool = False,
) -> ScenarioResult:
    url = ws_url(base)
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[_one_concurrent_session(url, hold_s, with_audio) for _ in range(clients)]
    )
    wall = (time.perf_counter() - t0) * 1000
    ready_ok = [r for r in results if r.get("ok") and not r.get("busy") and r.get("ready_ms") is not None]
    busy = [r for r in results if r.get("busy")]
    failed = [r for r in results if not r.get("ok")]
    ready_lat = [float(r["ready_ms"]) for r in ready_ok]
    return ScenarioResult(
        name=f"WS concurrent sessions ×{clients}" + (" +audio" if with_audio else ""),
        ok=len(failed) == 0,
        detail=(
            f"simultaneous connect+start, hold {hold_s:.0f}s; "
            f"accepted={len(ready_ok)} busy={len(busy)} failed={len(failed)}"
        ),
        metrics={
            "wall_ms": round(wall, 2),
            "accepted": len(ready_ok),
            "busy_rejected": len(busy),
            "failed": len(failed),
            "ready_latency_accepted": summarize(ready_lat),
            "sample_errors": [r.get("error") for r in failed[:5] if r.get("error")],
        },
        errors=[str(r.get("error")) for r in failed[:5] if r.get("error")],
    )


async def scenario_rest_assess(base: str, n: int = 5) -> ScenarioResult:
    """REST /assess with synthetic WAV (triggers real STT)."""
    audio = wav_bytes_from_pcm(pcm_tone(1500, amp=0.3))
    latencies: list[float] = []
    statuses: list[int] = []
    errors: list[str] = []
    async with httpx.AsyncClient(base_url=base, timeout=120.0) as client:
        for i in range(n):
            t0 = time.perf_counter()
            try:
                r = await client.post(
                    "/api/memorization/assess",
                    data={"surah": "1", "ayah": "1", "threshold": "0.5"},
                    files={"audio": ("tone.wav", audio, "audio/wav")},
                )
                latencies.append((time.perf_counter() - t0) * 1000)
                statuses.append(r.status_code)
                if r.status_code not in {200, 400}:
                    errors.append(f"iter{i}: status={r.status_code} body={r.text[:120]}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"iter{i}: {exc}")
    return ScenarioResult(
        name="REST /assess (STT) latency",
        ok=len(latencies) >= max(1, n // 2),
        detail=f"{n} serial multipart assessments with 1.5s WAV tone",
        metrics={
            "latency": summarize(latencies),
            "status_codes": {str(s): statuses.count(s) for s in sorted(set(statuses))},
            "errors": len(errors),
        },
        errors=errors[:5],
    )


async def scenario_find_busy_limit(base: str, max_probe: int = 8) -> ScenarioResult:
    """Ramp concurrency until busy rejections appear (or max_probe)."""
    url = ws_url(base)
    curve: list[dict[str, Any]] = []
    first_busy_at: int | None = None
    for n in range(1, max_probe + 1):
        results = await asyncio.gather(
            *[_one_concurrent_session(url, hold_s=2.0, with_audio=False) for _ in range(n)]
        )
        accepted = sum(1 for r in results if r.get("ok") and not r.get("busy"))
        busy = sum(1 for r in results if r.get("busy"))
        failed = sum(1 for r in results if not r.get("ok"))
        curve.append({"clients": n, "accepted": accepted, "busy": busy, "failed": failed})
        if busy > 0 and first_busy_at is None:
            first_busy_at = n
        # Drain briefly so next ramp starts clean
        await asyncio.sleep(0.5)
    return ScenarioResult(
        name="WS concurrency ramp (find busy limit)",
        ok=True,
        detail=f"probed 1..{max_probe}; first busy at {first_busy_at}",
        metrics={"first_busy_at_clients": first_busy_at, "curve": curve},
    )


async def run_all(base: str) -> dict[str, Any]:
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    results: list[ScenarioResult] = []

    print("== Health ==")
    results.append(await scenario_health(base, n=50))
    print(json.dumps(asdict(results[-1]), indent=2))

    print("\n== WS serial connect/ready ==")
    results.append(await scenario_ws_connect_ready(base, n=20))
    print(json.dumps(asdict(results[-1]), indent=2))

    print("\n== WS ping/pong ==")
    results.append(await scenario_ws_ping_pong(base, n=200))
    print(json.dumps(asdict(results[-1]), indent=2))

    print("\n== WS PCM ingest ==")
    results.append(await scenario_ws_audio_ingest(base, duration_s=8.0, chunk_ms=250))
    print(json.dumps(asdict(results[-1]), indent=2))

    print("\n== WS concurrency ramp ==")
    results.append(await scenario_find_busy_limit(base, max_probe=6))
    print(json.dumps(asdict(results[-1]), indent=2))

    # Stress at/above default max (2) and a higher burst
    for n, audio in [(2, False), (2, True), (4, False), (8, False)]:
        print(f"\n== WS concurrent ×{n} audio={audio} ==")
        results.append(await scenario_ws_concurrency(base, clients=n, hold_s=4.0, with_audio=audio))
        print(json.dumps(asdict(results[-1]), indent=2))
        await asyncio.sleep(1.0)

    print("\n== WS force_assess STT ==")
    results.append(await scenario_ws_force_assess(base, n=6))
    print(json.dumps(asdict(results[-1]), indent=2))

    print("\n== REST /assess STT ==")
    results.append(await scenario_rest_assess(base, n=4))
    print(json.dumps(asdict(results[-1]), indent=2))

    return {
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": base,
        "ws_url": ws_url(base),
        "scenarios": [asdict(r) for r in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Stress-test Quran Memorization WS/REST")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--json",
        default=str(Path(__file__).resolve().parent / "ws_stress_bench_results.json"),
    )
    args = parser.parse_args()
    report = asyncio.run(run_all(args.base))
    out = Path(args.json)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
