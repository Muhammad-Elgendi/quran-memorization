# Realtime Continuous Recitation — WebSocket Spec

**Status:** Implemented (v1 lightweight — PCM, silence VAD, partials off by default)  
**Phase:** 2  
**Companion:** `implementation-spec.md` (Phase 1 REST `/assess` remains authoritative for single-ayah offline assessment)  
**Version:** 1.1  
**Last updated:** 2026-08-13

---

## 1. Purpose

Provide a **realtime, continuous memorization session** over WebSocket where the user starts at a chosen ayah and keeps reciting. The server:

1. Continuously receives audio while the mic is open.
2. Detects ayah completion (or stable partial progress).
3. Transcribes and assesses against the **current expected ayah**.
4. On pass (or explicit skip/fail policy), **advances to the next ayah**.
5. Repeats until the client stops the session or an end-of-range / end-of-surah condition is met.

This is the streaming counterpart to:

```http
POST /api/memorization/assess
```

Phase 1 is **record → upload → one result**. Phase 2 is **listen → analyze → advance → continue**.

---

## 2. Goals and non-goals

### 2.1 Goals

| ID | Goal |
|----|------|
| R1 | WebSocket endpoint for a long-lived memorization session |
| R2 | Client streams mic audio in chunks; server streams incremental + ayah-final feedback |
| R3 | Auto-advance to the next ayah when the current ayah is accepted |
| R4 | Continue until the user stops, hits a configured end ayah, or reaches end of surah |
| R5 | Reuse Phase 1 services: `QuranService`, `SpeechRecognizer`, `MemorizationAssessor`, normalizer |
| R6 | Same assessment semantics as REST (score, pass/fail, missing/extra/wrong, alignment) |
| R7 | Client-agnostic protocol (Vue now; Flutter later) |
| R8 | Graceful stop, reconnect policy, and clear error frames |

### 2.2 Non-goals (this phase)

- Tajweed scoring (madd, ghunnah, etc.)
- Persistent user accounts / cloud progress DB (optional local session summary only)
- Fine-tuned Quran ASR (Phase 3) — still Moonshine (or any `SpeechRecognizer`) behind the interface
- Full-duplex audio playback of reference recitation
- Multi-user / shared sessions
- Replacing REST `/assess` — both coexist

### 2.3 Non-negotiables (inherit from Phase 1)

1. Never mutate stored Quran text; normalize only comparison copies.
2. STT only through `SpeechRecognizer` — no model imports in the WebSocket router.
3. Assessment uses sequence alignment (`MemorizationAssessor`), not naive zip-by-index.
4. Clients send audio + session config; Arabic/assessment logic stays on the backend.
5. Corpus remains `data/quran.json` via `QuranService`.

---

## 3. User experience (product behavior)

### 3.1 Happy path

```text
User selects start (surah, ayah) [optional end ayah]
        │
        ▼
Client opens WebSocket → sends session.start
        │
        ▼
Server replies session.ready with expected ayah text
        │
        ▼
User speaks continuously
        │
        ├─► Server emits partial.transcript / partial.alignment (optional UX)
        │
        ├─► Server detects ayah boundary or enough evidence of completion
        │         │
        │         ▼
        │   ayah.result (score, passed, mistakes, …)
        │         │
        │         ├── passed (or auto-advance policy) → session.advance
        │         │         │
        │         │         ▼
        │         │   next expected ayah; continue listening
        │         │
        │         └── failed → session.waiting (user retries same ayah)
        │                   or session.advance if fail_policy = continue
        │
        ▼
User stops → client sends session.stop → server sends session.summary → close
```

### 3.2 What “realtime” means here

| Mode | Latency target | Behavior |
|------|----------------|----------|
| **Partial** | ~0.5–2 s after each audio window | Provisional transcript / word highlights for the *current* ayah |
| **Ayah-final** | After boundary detection or silence | Full `MemorizationAssessor` result; advance decision |
| **Advance** | Immediate after accept | New expected ayah pushed; UI scrolls/highlights next |

Realtime does **not** mean word-perfect tajweed streaming. It means continuous listen + analyze + advance without a new HTTP upload per ayah.

### 3.3 Stop conditions

The session ends when any of:

1. Client sends `session.stop` (user pressed Stop / closed mic intentionally).
2. Client closes the WebSocket.
3. Configured `end_surah` / `end_ayah` is completed successfully.
4. End of surah reached and `cross_surah` is false (API default). Continuous UI sends `cross_surah: true` with open end — see `cross-surah-advance-spec.md`.
5. Server idle timeout (no audio / no control messages).
6. Fatal error (`error` with `fatal: true`).
7. Corpus end with `cross_surah: true` (`quran_complete`).

---

## 4. Endpoint

```text
WS /api/memorization/stream
```

**Scheme:** `ws://` or `wss://` matching the API host.  
**Subprotocol:** none required in v1 (JSON text frames + binary audio).  
**Auth:** none in local Phase 2 (same as Phase 1). Document hook for future token query/header.

### 4.1 Connection URL examples

```text
ws://127.0.0.1:8000/api/memorization/stream
wss://app.example.com/api/memorization/stream
```

Vite / Ingress must upgrade WebSocket on the same path prefix as `/api`.

### 4.2 Frame types

| Direction | Frame | Content-Type / WS type |
|-----------|--------|-------------------------|
| Client → Server | Control / JSON | Text frame |
| Client → Server | Audio chunk | Binary frame |
| Server → Client | Events / JSON | Text frame |

Binary frames are **raw audio only**. All metadata is JSON text.

---

## 5. Session configuration

Sent once in `session.start` after connect (or as first message; server rejects audio before start).

```json
{
  "type": "session.start",
  "start_surah": 1,
  "start_ayah": 1,
  "end_surah": null,
  "end_ayah": null,
  "threshold": 0.85,
  "fail_policy": "retry",
  "cross_surah": true,
  "audio": {
    "format": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1,
    "chunk_ms": 250
  },
  "partials": true,
  "auto_advance": true
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `start_surah` | int | required | 1–114 |
| `start_ayah` | int | required | Starting ayah number in surah |
| `end_surah` | int \| null | null | Optional inclusive end; null = until stop / surah end |
| `end_ayah` | int \| null | null | Required if `end_surah` set |
| `threshold` | float | `settings.DEFAULT_THRESHOLD` | Pass threshold 0.5–1.0 |
| `fail_policy` | string | `"retry"` | `"retry"` \| `"continue"` \| `"stop"` |
| `cross_surah` | bool | false (API) / true (Continuous UI) | If true, after last ayah of surah N go to 1 of N+1 |
| `audio.format` | string | `"pcm_s16le"` | Preferred; also allow `"webm_opus"` (see §7) |
| `audio.sample_rate` | int | 16000 | Must match Moonshine input |
| `audio.channels` | int | 1 | Mono only in v1 |
| `audio.chunk_ms` | int | 250 | Client send cadence hint (100–1000) |
| `partials` | bool | true | Emit `partial.*` events |
| `auto_advance` | bool | true | On pass, automatically move to next ayah |

### 5.1 Fail policy semantics

| Policy | On ayah fail |
|--------|----------------|
| `retry` | Stay on same ayah; emit `session.waiting`; keep listening for another attempt |
| `continue` | Emit fail result, then advance anyway (practice-through mode) |
| `stop` | Emit fail result + `session.summary`; close |

### 5.2 Range validation

On `session.start`, server must:

1. Resolve start ayah via `QuranService.get_ayah`; 404 → `error` + close.
2. If end set, validate end exists and is **at or after** start in corpus order.
3. Reject invalid threshold / audio format with `error` (`fatal: true`).

---

## 6. Protocol — message catalog

All JSON messages share:

```json
{
  "type": "<event.name>",
  "session_id": "<uuid>",
  "ts": "<ISO-8601>"
}
```

`session_id` is assigned by the server in `session.ready` and echoed thereafter.

### 6.1 Client → Server

#### `session.start`

See §5. First required message.

#### `session.stop`

```json
{
  "type": "session.stop",
  "reason": "user"
}
```

`reason`: `"user"` \| `"pagehide"` \| `"error"`.

#### `session.pause` / `session.resume` (optional v1.1)

Pause stops consuming audio for assessment (still may ACK); resume continues on the **same** current ayah.

#### `ayah.force_advance`

```json
{
  "type": "ayah.force_advance",
  "reason": "skip"
}
```

User skips current ayah without a passing score. Still recorded in summary as `skipped`.

#### `ayah.force_assess`

```json
{
  "type": "ayah.force_assess"
}
```

Force ayah-final assessment on the current audio buffer (e.g. user taps “Check now” without waiting for silence detection).

**Implementation notes (Continuous):**

- Always runs STT when the ring buffer holds at least `STREAM_MIN_UTTERANCE_MS` of audio. It does **not** short-circuit on the VAD/STT energy gate (quiet AGC-off / denoise levels must still be transcribed).
- If Heard is empty after STT (or the buffer is too short), the server emits a non-fatal `error` with `code: "no_speech"` and `session.listening` — **not** `ayah.result` with Score 0%. Empty audio is not a memorization fail.

#### `ping`

```json
{ "type": "ping" }
```

Server replies `pong`.

### 6.2 Server → Client

#### `session.ready`

```json
{
  "type": "session.ready",
  "session_id": "a3f1…",
  "current": {
    "surah": 1,
    "ayah": 1,
    "text": "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ",
    "index_in_session": 0
  },
  "config": {
    "threshold": 0.85,
    "fail_policy": "retry",
    "auto_advance": true,
    "partials": true,
    "audio": {
      "format": "pcm_s16le",
      "sample_rate": 16000,
      "channels": 1
    }
  }
}
```

#### `partial.transcript`

Provisional STT for the current window / rolling buffer (only if `partials: true`).

```json
{
  "type": "partial.transcript",
  "surah": 1,
  "ayah": 1,
  "recognized": "بسم الله",
  "stable": false
}
```

`stable: true` means the decoder believes this prefix won’t shrink (optional; may be always false in v1).

#### `partial.alignment`

Optional lightweight alignment of partial transcript vs expected (same shape subset as assessor alignment).

```json
{
  "type": "partial.alignment",
  "surah": 1,
  "ayah": 1,
  "alignment": [
    { "op": "equal", "expected": "بسم", "recognized": "بسم" },
    { "op": "equal", "expected": "الله", "recognized": "الله" },
    { "op": "delete", "expected": "الرحمن", "recognized": null }
  ],
  "progress": 0.33
}
```

`progress` ∈ [0, 1]: fraction of expected tokens matched so far (UX progress bar).

#### `ayah.result`

Final assessment for one attempt at the current ayah (same fields as REST `/assess`, plus session context).

```json
{
  "type": "ayah.result",
  "surah": 1,
  "ayah": 1,
  "attempt": 1,
  "score": 0.91,
  "passed": true,
  "warning": false,
  "expected": "…",
  "recognized": "…",
  "missing_words": [],
  "extra_words": [],
  "wrong_words": [],
  "alignment": [],
  "message": "Excellent. Your recitation closely matches the selected ayah.",
  "will_advance": true
}
```

#### `session.advance`

```json
{
  "type": "session.advance",
  "from": { "surah": 1, "ayah": 1 },
  "to": {
    "surah": 1,
    "ayah": 2,
    "text": "ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَٰلَمِينَ",
    "index_in_session": 1
  },
  "reason": "passed"
}
```

`reason`: `"passed"` \| `"continue_policy"` \| `"skip"`.

#### `session.waiting`

Emitted when fail_policy is `retry` and the ayah did not pass.

```json
{
  "type": "session.waiting",
  "surah": 1,
  "ayah": 1,
  "attempt": 1,
  "hint": "Retry the same ayah. Review highlighted words."
}
```

#### `session.summary`

```json
{
  "type": "session.summary",
  "session_id": "a3f1…",
  "started_at": "…",
  "ended_at": "…",
  "reason": "user_stop",
  "ayahs_completed": 7,
  "ayahs_passed": 6,
  "ayahs_failed": 1,
  "ayahs_skipped": 0,
  "results": [
    {
      "surah": 1,
      "ayah": 1,
      "best_score": 0.94,
      "passed": true,
      "attempts": 1
    }
  ]
}
```

#### `error`

```json
{
  "type": "error",
  "code": "invalid_audio",
  "message": "Unsupported audio format for this session",
  "fatal": true
}
```

Non-fatal errors (`fatal: false`) keep the session open (e.g. one bad chunk).

#### `pong`

```json
{ "type": "pong", "ts": "…" }
```

---

## 7. Audio streaming

### 7.1 Preferred format (v1): PCM

- **Format:** signed 16-bit little-endian mono PCM (`pcm_s16le`)
- **Sample rate:** 16000 Hz (matches Moonshine / Phase 1 `librosa.load(..., sr=16000)`)
- **Chunk size:** ~250 ms → 8000 samples → 16000 bytes per binary frame
- **No container** in the binary frame (raw PCM)

Client responsibilities:

1. Capture mic (`getUserMedia`)
2. Resample to 16 kHz mono if needed (AudioWorklet / OfflineAudioContext)
3. Send binary frames at ~`chunk_ms` cadence while recording

### 7.2 Alternate format: WebM/Opus

Browsers naturally produce WebM. Supporting WebM on the socket is optional in v1 because:

- Continuous Opus packets need a decoder pipeline (ffmpeg / opus) per session.
- PCM avoids re-encoding and matches STT input.

If `audio.format = "webm_opus"`:

- Binary frames are **segmented WebM clusters** or a single growing init+media stream (must be documented as either **independent clusters** or **append-only stream**).
- Server uses existing `prepare_audio` / ffmpeg path on rolling buffers.
- Higher latency; use only if PCM client path is blocked.

**Recommendation:** implement PCM first; keep WebM for REST `/assess` only until Flutter/web worklets are ready.

### 7.3 Server audio buffer

Per session:

```text
ring / append buffer of PCM
        │
        ▼
VAD / silence / duration windows
        │
        ├── partial window every N ms → STT partial → partial.* events
        │
        └── ayah-final trigger → STT full segment → MemorizationAssessor → ayah.result
```

Cleanup: discard consumed audio after ayah-final (keep small overlap, e.g. 200–500 ms, to avoid cutting the next ayah’s first word).

### 7.4 Limits

| Limit | Suggested default | Notes |
|-------|-------------------|-------|
| Max session duration | 30 min | Then `session.summary` + close |
| Max continuous buffer | 30–60 s | Drop oldest or force assess |
| Max binary frame size | 256 KiB | Reject oversized chunks |
| Idle timeout (no frames) | 60 s | Configurable |
| Max concurrent sessions / process | 1–2 on CPU-only | Document; queue or 503-equivalent `error` |

Reuse spirit of Phase 1 `MAX_UPLOAD_BYTES` / duration caps, but for **streaming windows**, not whole-file upload.

---

## 8. Ayah boundary detection

The hardest product decision: when is “this ayah” finished so we can assess and advance?

### 8.1 Strategies (implement in order)

#### Strategy A — Silence-based (MVP)

1. Run simple energy VAD on PCM (or `webrtcvad` if added as dependency).
2. While speech is active, accumulate into current attempt buffer.
3. After silence ≥ `silence_ms` (default **700–1000 ms**) and buffer ≥ `min_utterance_ms` (default **400 ms**), trigger ayah-final assess.
4. If score passes → advance; else retry/continue per policy.

**Pros:** Simple, works with continuous recitation pauses between ayahs.  
**Cons:** User who does not pause may merge two ayahs into one buffer.

#### Strategy B — Expected-length / coverage heuristic

1. Estimate expected duration from word count (rough ms/word) or from partial `progress`.
2. When `progress >= coverage_threshold` (e.g. 0.85) **and** silence ≥ shorter threshold (e.g. 400 ms), finalize.
3. If partial transcript already matches end tokens of the ayah, finalize earlier.

#### Strategy C — Forced client cue

`ayah.force_assess` / UI “Next” for users who recite without pauses.

#### Strategy D — Dual-ayah hypothesis (later)

If recognized text aligns better with ayah N+1 than N, auto-split or reassign — **out of scope for v1**.

### 8.2 Continuous recitation without pause

If the user does not pause between ayahs:

1. Prefer Strategy B (coverage) + rolling alignment.
2. When expected ayah is fully matched with high confidence, emit `ayah.result` **without** waiting for long silence, then start matching surplus recognized tokens against the **next** ayah (carry leftover transcript).
3. Document leftover carry as required for “keep going as long as the user is reciting.”

**v1 acceptance criterion:** with natural pauses between ayahs, auto-advance works; without pauses, force-assess or leftover carry must still make progress (may be v1.1).

### 8.3 Config knobs (server settings)

```text
STREAM_SILENCE_MS=800
STREAM_SHORT_SILENCE_MS=400
STREAM_MIN_UTTERANCE_MS=400
STREAM_PARTIAL_EVERY_MS=2000
STREAM_COMPLETION_PROBE=true
STREAM_COMPLETION_PROBE_MS=1000
STREAM_COVERAGE_THRESHOLD=0.85
STREAM_COVERAGE_STABLE_TICKS=2
STREAM_OVERLAP_MS=300
STREAM_PARTIALS_DEFAULT=true
STREAM_VAD_RMS_THRESHOLD=0.015
STREAM_STT_RMS_THRESHOLD=0.008
STREAM_IDLE_TIMEOUT_S=60
STREAM_MAX_SESSION_S=1800
```

- **VAD RMS** segments speech vs silence.
- **STT RMS** (lower) decides whether automatic periodic STT is worth calling. Check now ignores this gate (see `ayah.force_assess` above).
- **Stable ticks** require consecutive high-coverage probes before auto-finalize (avoids mid-word `ayah.result` on short ayahs). Coverage for the probe is **cumulative credit** when multi-utterance credit is on (see [`multi-utterance-credit-spec.md`](multi-utterance-credit-spec.md)).
- **Long silence below completion:** if Heard is a **failed attempt** (mismatch at credit cursor), emit `ayah.result` (typically fail) and apply `fail_policy` so the client can play the mistake tone. A **successful partial chunk** keeps credit and stays listening (no tone). Empty Heard still abandons without scoring. **Short silence** below completion never finalizes (breath between words). See [`continuous-mistake-tone-spec.md`](continuous-mistake-tone-spec.md).
- **Multi-utterance credit:** user may split an ayah across pauses; contiguous prefix credit must reach N before pass-advance. REST `/assess` unchanged.

---

## 9. Assessment & advance logic

### 9.1 Reuse Phase 1 assessor

For each ayah-final segment:

```text
expected = QuranService.get_ayah(surah, ayah)["text"]
recognized = SpeechRecognizer.transcribe(segment_wav_or_pcm)
result = MemorizationAssessor(threshold).assess(expected, recognized)
```

Same response fields as REST. Do **not** fork scoring logic into a second algorithm.

### 9.2 Advance rules

```text
if result.passed and auto_advance:
    emit ayah.result (will_advance=true)
    emit session.advance → set current = next_ayah()
    clear attempt buffer (keep overlap)
elif not result.passed and fail_policy == "retry":
    emit ayah.result (will_advance=false)
    emit session.waiting
    clear attempt buffer
elif not result.passed and fail_policy == "continue":
    emit ayah.result (will_advance=true)
    emit session.advance (reason=continue_policy)
elif not result.passed and fail_policy == "stop":
    emit ayah.result
    emit session.summary
    close
```

### 9.3 Next ayah resolution

```text
def next_ayah(surah, ayah):
    if ayah < last_ayah_in_surah(surah):
        return surah, ayah + 1
    if cross_surah and surah < 114:
        return surah + 1, 1
    return None  # session complete
```

If `end_surah`/`end_ayah` configured and current equals end after pass → `session.summary` with `reason: "range_complete"`.

### 9.4 Partials vs final

| | Partial | Final |
|-|---------|-------|
| STT | Faster / smaller window; may be approximate | Full attempt buffer |
| Assessor | Optional light alignment for UX | Full `MemorizationAssessor` |
| Advance | Never | Only on final |

Partials must not change `current` ayah.

---

## 10. Backend architecture

### 10.1 New modules (proposed)

```text
backend/app/
├── api/
│   └── memorization_stream.py   # WebSocket route
├── services/
│   ├── stream_session.py        # Session state machine
│   ├── stream_audio.py          # PCM ring buffer, VAD, segmentation
│   ├── speech_service.py        # existing (+ optional transcribe_pcm)
│   └── assessor.py              # existing
```

Keep REST router in `memorization.py`; register WS on the same `/api/memorization` prefix or adjacent router included from `main.py`.

### 10.2 Session state machine

```text
CONNECTING → READY → LISTENING ⇄ ASSESSING → ADVANCING
                │                    │
                └──── PAUSED ────────┘
                │
                └→ STOPPING → CLOSED
```

| State | Accepts audio? | Notes |
|-------|----------------|-------|
| READY | no | Waiting for first chunk after ready |
| LISTENING | yes | Buffering + partials |
| ASSESSING | queue or drop | Run STT+assess; avoid overlapping heavy jobs |
| ADVANCING | no (brief) | Emit advance; switch expected text |
| PAUSED | no | Optional |
| STOPPING/CLOSED | no | Summary + cleanup |

### 10.3 Concurrency & STT

Moonshine on CPU is heavy. Rules:

1. **One in-flight STT job per session** (queue the next window).
2. Prefer running STT in a thread/process pool so the ASGI event loop stays responsive (`asyncio.to_thread` or dedicated executor).
3. Partials may use a shorter audio slice; finals use the full utterance buffer.
4. Extend interface optionally:

```python
class SpeechRecognizer(ABC):
    def transcribe(self, audio_path: str) -> str: ...

    def transcribe_audio(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """Optional PCM path for streaming; default may write temp WAV."""
```

Default implementation can write a temp WAV and call `transcribe` to avoid breaking Phase 1.

### 10.4 Resource cleanup

On stop/close/error:

1. Cancel pending STT tasks.
2. Delete temp files.
3. Drop PCM buffers.
4. Emit `session.summary` if session had started (`session.ready` already sent).

---

## 11. Frontend integration (Vue)

### 11.1 UX flow

1. User picks start (and optional end) ayah — same selectors as today.
2. Mode toggle: **Single ayah (REST)** vs **Continuous (WebSocket)**.
3. Continuous mode:
   - Connect WS → `session.start`
   - Show current ayah text prominently
   - Start mic → stream PCM chunks
   - On `partial.alignment`: highlight words live
   - On `ayah.result`: show score toast; play the Phase 1 warning tone **once per attempt** if `warning` / `!passed`, using the **live capture** `AudioContext` so it is audible while the mic is on. Do not beep on `session.waiting`. See [`continuous-mistake-tone-spec.md`](continuous-mistake-tone-spec.md).
   - On `session.advance`: update displayed ayah; optional subtle transition
   - Stop button → `session.stop` → show summary panel

### 11.2 Client sketch

```js
const ws = new WebSocket(`${wsBase}/api/memorization/stream`);
ws.binaryType = "arraybuffer";

ws.onopen = () => {
  ws.send(JSON.stringify({
    type: "session.start",
    start_surah: surah,
    start_ayah: ayah,
    threshold: 0.85,
    fail_policy: "retry",
    audio: { format: "pcm_s16le", sample_rate: 16000, channels: 1, chunk_ms: 250 },
    partials: true,
    auto_advance: true,
  }));
};

ws.onmessage = (ev) => {
  if (typeof ev.data === "string") {
    const msg = JSON.parse(ev.data);
    // handle session.ready | partial.* | ayah.result | session.advance | …
  }
};

// AudioWorklet posts Int16Array chunks:
worklet.onmessage = (e) => {
  if (ws.readyState === WebSocket.OPEN) ws.send(e.data.buffer);
};
```

### 11.3 Proxy / Docker / K8s

- Vite proxy must support WS upgrade for `/api`.
- Ingress / nginx: enable WebSocket headers (`Upgrade`, `Connection`).
- Load balancers: sticky sessions if multi-replica (in-memory session state is local to the pod).

---

## 12. Error codes

| Code | Fatal | Meaning |
|------|-------|---------|
| `invalid_start` | yes | Bad surah/ayah / range |
| `ayah_not_found` | yes | QuranService miss |
| `invalid_config` | yes | threshold / format / channels |
| `not_ready` | no | Audio before `session.start` |
| `invalid_audio` | no/yes | Corrupt chunk; fatal if format mismatch |
| `stt_unavailable` | yes | Model failure (akin to HTTP 503) |
| `session_timeout` | yes | Idle or max duration |
| `busy` | no | Dropped partial because assess in flight |
| `internal` | yes | Unexpected server error |

---

## 13. Sequence diagrams

### 13.1 Pass and advance

```text
Client                          Server
  |--- session.start ------------>|
  |<-- session.ready -------------|
  |--- binary PCM ... ----------->|
  |<-- partial.transcript --------|
  |<-- partial.alignment ---------|
  |--- (silence / coverage) ----->|
  |<-- ayah.result (passed) ------|
  |<-- session.advance -----------|
  |--- binary PCM ... ----------->|  (next ayah)
  |--- session.stop ------------->|
  |<-- session.summary -----------|
  |          close                |
```

### 13.2 Fail and retry

```text
Client                          Server
  |--- PCM utterance ------------>|
  |<-- ayah.result (passed=false)-|
  |<-- session.waiting -----------|
  |--- PCM retry ---------------->|
  |<-- ayah.result (passed=true)--|
  |<-- session.advance -----------|
```

---

## 14. Testing plan

### 14.1 Unit

- Session state machine: start → assess pass → advance → end of range.
- Next-ayah / cross-surah / end-ayah boundaries.
- Fail policies `retry` / `continue` / `stop`.
- VAD silence segmentation on synthetic PCM.

### 14.2 Integration (pytest + Starlette TestClient WS)

- Connect, `session.start`, inject mock PCM / mock recognizer.
- `MockSpeechRecognizer` returns scripted transcripts per call.
- Assert order: `ready` → `ayah.result` → `advance` → `summary`.

### 14.3 Manual

- Recite Al-Fatihah continuously with short pauses; confirm auto-advance 1→7.
- Fail intentionally; confirm retry stays on same ayah.
- Stop mid-surah; summary counts match.
- Leave idle; timeout closes cleanly.

---

## 15. Observability

Log (structured) per session:

- `session_id`, start/end, ayah transitions, assess latency_ms, STT latency_ms, pass/fail.
- Do **not** log full Quran text at info level in production if privacy becomes relevant; debug OK locally.

Metrics (optional):

- active_sessions
- ayah_assess_total{passed}
- stt_latency_ms histogram

---

## 16. Security & abuse (local-first, still document)

- Max session length and frame size (§7.4).
- No auth in v1 local deploy; when exposed beyond localhost, require token and `wss://`.
- CORS does not apply to WS the same way; validate `Origin` if configured.

---

## 17. Implementation phases (suggested)

| Step | Deliverable |
|------|-------------|
| 2.0 | Spec accepted (this document) |
| 2.1 | WS connect + `session.start` / `ready` / `stop` / `summary` (no STT) |
| 2.2 | PCM binary ingest + ring buffer + silence segmentation |
| 2.3 | Hook `SpeechRecognizer` + `MemorizationAssessor` → `ayah.result` |
| 2.4 | Auto-advance + fail_policy |
| 2.5 | Partials (`partial.transcript` / `partial.alignment`) |
| 2.6 | Vue continuous mode UI + AudioWorklet PCM |
| 2.7 | Leftover-carry for pause-less recitation (if not already solid) |
| 2.8 | Ingress/Vite WS proxy docs; load test single-session latency |

REST `/api/memorization/assess` remains for one-shot practice and as a fallback when WS is unavailable.

---

## 18. Acceptance criteria

- [ ] Client can open `WS /api/memorization/stream`, start at a given ayah, and stream mic audio.
- [ ] Server assesses ayah-by-ayah and emits results compatible with Phase 1 assess fields.
- [ ] On pass with `auto_advance: true`, server moves to the next ayah and continues without reconnect.
- [ ] Session continues until user stop, configured range end, or surah end (`cross_surah: false`).
- [ ] Fail policy `retry` keeps the same ayah; `continue` advances; `stop` ends session.
- [ ] No mutation of `quran.json` text; STT only via `SpeechRecognizer`.
- [ ] Stopping yields a `session.summary` with per-ayah outcomes.
- [ ] Documented behavior for silence-based boundaries and force-assess escape hatch.

---

## 19. Open questions

1. **PCM vs WebM first on web:** AudioWorklet PCM is more work on the client but better for the server — confirm before 2.2.
2. **Partial STT cost:** Partials every 500 ms may overload CPU; may gate behind `partials: false` by default on weak hosts.
3. **Surplus transcript carry** across ayahs: required for true continuous tilawah; complexity vs silence-only MVP.
4. **Multi-replica sticky sessions:** required before K8s horizontal scale.
5. **Whether `ayah.result` should include timing** (`audio_ms`, `stt_ms`) for UI debugging.

---

## 20. Relation to existing docs

| Doc | Role |
|-----|------|
| `specs/implementation-spec.md` | Phase 1 REST MVP (authoritative for `/assess`) |
| `specs/first-spec.md` | Early design notes; streaming sketched informally |
| `specs/realtime-stream-spec.md` | **This file** — Phase 2 continuous WebSocket |
| `docs/agent-context.md` | Living ops/context; update when implementation lands |

When implementation begins, update `implementation-spec.md` §11.5 from “document only” to a pointer here, and mark Phase 2 items in `docs/agent-context.md` as in progress.
