# Continuous Mode: Mistake Warning Tone Spec

**Status:** Implemented (P0)  
**Phase:** 2.x (UX fix; protocol-compatible preferred)  
**Companion:**  
- `realtime-stream-spec.md` (§11.1: play warning tone on `ayah.result` if `warning`)  
- `implementation-spec.md` (G6 / Phase 1 oscillator beep)  
- `ayah-advance-fix-spec.md` (coverage gate → no `ayah.result` → no beep)  
- `continuous-vs-single-detection-spec.md` (U1/U2 fail modes)  
- `partials-evaluation-spec.md` (live `partial.alignment`)  
- `first-spec.md` (streaming “immediate tone” vision)  
**Version:** 1.0  
**Last updated:** 2026-08-14

---

## 1. Purpose

In **Continuous** mode, the user can recite with clear memorization mistakes and hear **no warning tone**, even though Single ayah mode already beeps on fail (~660 Hz, ~0.3 s).

This spec:

1. Explains why the Continuous tone path is silent today (protocol + browser audio)
2. Defines when a mistake is **discovered** (vs mid-utterance provisional noise)
3. Specifies **play the warning tone once** per discovery, without beep spam
4. Fixes audible playback while the mic capture graph is active
5. Lists acceptance tests and an explicit non-goal list

**Constraint:** Do not mutate stored Quran text. Do not lower pass / coverage thresholds to force more `ayah.result` frames. STT stays behind `SpeechRecognizer`. Prefer client-side tone playback (same as Phase 1); do not stream server audio for the beep.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Behavior | User recites current ayah **with mistakes** |
| Expectation | Short warning tone when mistakes are discovered (same family as Single-mode fail beep) |
| Actual | No tone (or unreliable tone) |
| Contrast | Single ayah REST fail → `playWarning()` works after upload |

Product intent (from `first-spec.md` streaming sketch and `realtime-stream-spec.md` §11.1):

> Mistake discovered → **tone once** → user notices where they deviated → retry / continue.

Today Continuous only *attempts* the tone on `ayah.result` with `warning || !passed`. That event often **never arrives** when the user is wrong, and when it does arrive the browser oscillator may still be **inaudible** under the live capture `AudioContext`.

---

## 3. Current behavior (code truth)

### 3.1 What already exists

| Layer | Behavior |
|-------|----------|
| Phase 1 Single | After `POST /assess`, if `warning \|\| !passed` → `playWarning()` |
| Continuous UI | On `ayah.result`, if `msg.warning \|\| !msg.passed` → same `playWarning()` |
| Tone implementation | New `AudioContext()` + oscillator 660 Hz, gain 0.2, stop after 0.3 s (`frontend/src/App.vue`) |
| Backend | `ayah.result` includes `passed`, `warning`, `wrong_words`, `alignment`, `score`, `coverage` |

### 3.2 Why Continuous stays silent

```text
PCM (mic on)
    │
    ├─ periodic STT → partial.alignment (provisional=true while coverage < 0.85)
    │                    │
    │                    └─ UI maps provisional mismatches to status "pending"
    │                       (highlight.js) — no locked "wrong", no tone hook
    │
    └─ silence / coverage finalize → run_assess()
           │
           ├─ coverage < STREAM_COVERAGE_THRESHOLD (0.85)
           │     silence_short → partials + session.listening  (NO ayah.result)
           │     silence       → abandon incomplete           (NO ayah.result)
           │     → playWarning never called
           │
           └─ coverage ≥ 0.85 → ayah.result
                 │
                 ├─ passed=true  → no tone (correct)
                 └─ passed=false → UI calls playWarning()
                       │
                       └─ NEW AudioContext() while capture graph holds another
                          context → often suspended / inaudible (mic still on)
```

| Gap ID | Root cause | User-visible effect |
|--------|------------|---------------------|
| **T1** | Coverage gate blocks `ayah.result` on incomplete / wrong takes | Mistakes + silence → no finalize → **no beep** |
| **T2** | Long-silence abandon clears UI without a fail result | User stopped mid-wrong ayah → **no beep** |
| **T3** | Live partials keep mismatches `pending` until final result | No “mistake discovered” signal during recitation |
| **T4** | `playWarning()` creates a **fresh** `AudioContext` under live capture | Even on real `ayah.result` fail, beep may be **silent** |
| **T5** | No debounce / once-per-attempt state | If we only fix early detection without rules → risk of beep spam |

**T1** is documented in `ayah-advance-fix-spec.md` §3.1: below coverage, silence paths return without beep. That was correct for “don’t false-advance,” but it also removed the only Continuous tone trigger.

---

## 4. Goals and non-goals

### 4.1 Goals

| ID | Goal |
|----|------|
| **G1** | When Continuous discovers a memorization **fail** for the current attempt, play the Phase 1 warning tone **once** |
| **G2** | Tone must be **audible while mic capture is running** (reuse / resume a live context) |
| **G3** | Prefer discovery on **committed** fail signals; avoid beeping on fleeting mid-word STT flicker |
| **G4** | Same timbre as Single mode (660 Hz, ~0.3 s, modest gain) unless product later chooses a softer Continuous cue |
| **G5** | Works for `fail_policy` `retry` (default), `continue`, and `stop` |
| **G6** | Unit / UI tests cover “fail → tone once” and “provisional noise → no tone” |

### 4.2 Non-goals

- Server-generated PCM beep over the WebSocket
- Different tones per mistake type (wrong vs missing vs extra)
- Tajweed / melody feedback
- Auto-pausing the mic when a mistake is found
- Changing `STREAM_COVERAGE_THRESHOLD`, `DEFAULT_THRESHOLD`, or assessor scoring to create more fails
- Beeping on every red highlight flicker during a single utterance
- Playing a “success” chime on pass (out of scope; optional later)

---

## 5. Definitions

### 5.1 Mistake discovered

A mistake is **discovered** when the session has a **committed** signal that the current ayah attempt is not acceptable as a pass — not merely that a live partial window looks incomplete.

| Signal | Committed? | May trigger tone? |
|--------|------------|-------------------|
| `partial.alignment` with `provisional: true` | No | **No** (default) |
| `partial.alignment` with locked wrong/missing after policy below | Optional (P1) | Only under §7.2 rules |
| `ayah.result` with `passed === false` (or `warning === true`) | Yes | **Yes** (P0) |
| `session.waiting` after a fail (`fail_policy=retry`) | Yes (follows result) | Tone already from result; do not double-beep |
| Coverage abandon / `session.listening` cleared with empty alignment | Incomplete, not scored fail | **No** for P0 (see P1 option) |
| `ayah.result` with `passed === true` | Pass | **No** |

### 5.2 “Once”

| Scope | Rule |
|-------|------|
| Per **ayah attempt** | At most **one** warning tone for a given `(surah, ayah, attempt)` fail |
| After `session.advance` | Counter resets for the new ayah |
| After retry clear (`session.waiting` / buffer clear on fail+retry) | Next attempt may beep again if it fails again |
| Rapid duplicate `ayah.result` | Second identical fail for same attempt must not beep again |

Implementation sketch: client keeps `lastWarnedKey = `${surah}:${ayah}:${attempt}``; call `playWarning()` only when key changes.

### 5.3 Tone parameters (reuse Phase 1)

| Param | Value |
|-------|--------|
| Frequency | 660 Hz |
| Duration | 0.3 s |
| Gain | ~0.2 (linear) |
| Wave | oscillator default (sine) |
| Ducking | Optional soft duck of silence only; **do not** mute mic PCM path |

---

## 6. Failure modes to fix (mapped)

| ID | Scenario | Required outcome |
|----|----------|------------------|
| **U-Tone-1** | User finishes a wrong ayah; coverage ≥ 0.85; `ayah.result` `passed=false` | Tone **once**; red highlights; `session.waiting` if retry |
| **U-Tone-2** | User finishes a wrong ayah; coverage &lt; 0.85; natural pause | Today: no `ayah.result`. P0 must still discover fail (§7.1) **or** document Check-now-only — **P0 chooses server emit** |
| **U-Tone-3** | User mid-recites; one provisional replace flicker then recovers | **No** tone |
| **U-Tone-4** | Mic on + fail result arrives | Tone **audible** (T4 fixed) |
| **U-Tone-5** | Pass then advance | **No** tone |
| **U-Tone-6** | Same fail attempt delivers result + waiting | **One** tone total |
| **U-Tone-7** | Single mode unchanged | Still beeps on REST fail |

---

## 7. Design

### 7.1 P0 — Guaranteed fail finalize + audible client tone (required)

P0 closes **T1** and **T4** without inventing a second scoring system.

#### 7.1.1 Backend: emit a fail `ayah.result` when the user has clearly stopped on a wrong / incomplete attempt

**Problem:** Long silence with `coverage < 0.85` calls `_abandon_incomplete_attempt()` — no score, no beep. Short silence under coverage only refreshes partials.

**Decision (P0):** On **long silence** (`reason == "silence"`) when:

1. There is enough PCM for a final STT (existing min-utterance / energy rules), and  
2. Heard text is non-empty after confidence/recovery filters, and  
3. The window is a **failed attempt** — committed mismatch at the credit cursor / cannot cleanly extend contiguous credit (see [`multi-utterance-credit-spec.md`](multi-utterance-credit-spec.md) §7.5) — **or** (legacy / credit disabled) `coverage < STREAM_COVERAGE_THRESHOLD` **or** `assess().passed === false`,

→ run the normal assess path and emit `ayah.result` with `passed` / `warning` / alignment / wrong words, then apply `fail_policy` (retry → `session.waiting`, etc.).

**Narrowing (2026-08-15):** Long silence with a **successful partial chunk** (contiguous credit advanced, ayah not complete, no trailing wrong token) is **incomplete continuation** — **no** fail `ayah.result`, **no** mistake tone; keep credit and re-arm listening. Do not treat every below-coverage Heard as a fail.

**Keep short silence (`silence_short`) unchanged:** still do **not** finalize below coverage (breath between words must not fail the ayah).

**Keep empty Heard as non-error:** quiet mic / filtered-empty continues to use `_no_speech_events` / abandon-without-fail (no tone). That preserves the quiet-mic regression fix.

**Pass still requires full contiguous credit** (or legacy window coverage when multi-utterance credit is off): `MemorizationAssessor` character score is unchanged, but a long-silence result with incomplete credit is emitted as `passed=false` / `warning=true` even if `fuzz.ratio` ≥ threshold (e.g. Basmala missing `بسم` at score 0.90). That keeps the completion gate and still gives the client a fail frame to beep on.

```text
reason == silence
  ├─ empty Heard / no energy → abandon or listening (no tone)   [unchanged]
  ├─ non-empty Heard + coverage < threshold
  │     → assess() → ayah.result (usually passed=false) → fail_policy
  │     → client beeps once
  └─ coverage ≥ threshold → assess() as today
```

**Why long silence only:** Matches “user stopped; attempt is over.” Short pauses stay for completion of a correct ayah. This is the minimal protocol change that restores the beep when mistakes caused coverage never to reach 0.85.

**Optional field (recommended):** `ayah.result.trigger` already exists (`silence` / `coverage` / `force` / …). No new event type required for P0.

#### 7.1.2 Frontend: make `playWarning()` work during Continuous capture

**Problem:** `new AudioContext()` under Chromium often starts `suspended` without a fresh user gesture; capture already owns a running context.

**Decision:**

1. Extract `playWarningTone({ audioContext }?)` into a small module (e.g. `frontend/src/audio/warning-tone.js`).
2. Prefer the **live capture** `AudioContext` from `startPcmCapture` / `openAudioGraph` when mic is active.
3. If none: create/reuse a module-level context; `await context.resume()` before starting the oscillator.
4. Route oscillator → gain → **`audioContext.destination`** (speakers). Never connect into the PCM processor graph.
5. Idempotent stop: disconnect nodes after `ended`; ignore play if a tone is already playing (or schedule only one).

Wire Continuous `ayah.result` fail through the shared helper + **once-per-attempt** guard (§5.2).

Single mode should call the same helper (mic usually already stopped; helper still resumes if needed).

#### 7.1.3 Frontend: keep existing `ayah.result` hook

```text
on ayah.result:
  if (warning || !passed):
    if not alreadyWarned(surah, ayah, attempt):
      playWarningTone(...)
      markWarned(...)
```

Do **not** also beep on `session.waiting`.

### 7.2 P1 — Earlier “mistake locked” tone during live partials (optional)

Only after P0 is stable. Goal: closer to `first-spec.md` “immediate tone.”

**Commit rule for a live wrong word** (all must hold):

1. `partial.alignment` present  
2. `provisional === false` **or** a dedicated server flag `mistake_committed: true`  
3. At least one expected token is `replace` or `delete` at an index **strictly before** the current frontier (matched prefix length), i.e. a word the user has **already moved past**  
4. Same token index has been wrong/missing on **N consecutive** partials (recommend N=2)  
5. Once-per-attempt still applies (first committed mistake beeps; later words on same attempt stay silent)

**UI:** When committed, allow `wordsFromAlignment` to paint that token `wrong` / `missing` even if later tokens remain pending — do **not** paint the entire ayah red on the first flicker.

**Default for P1 ship:** off behind a client flag or only when `partials: true` and config `live_mistake_tone: true`.

### 7.3 P2 — Soft abandon cue (optional, probably skip)

A distinct softer cue when long silence abandons **empty** attempts is easy to confuse with memorization fail. **Out of default scope.** If product insists later, use a different frequency/duration and never call it “mistake tone.”

---

## 8. Protocol impact

### 8.1 P0 (preferred)

| Change | Detail |
|--------|--------|
| Events | Still `ayah.result` + existing fail_policy follow-ups |
| New types | None required |
| Semantics | Long silence + non-empty Heard below coverage **now scores** instead of silent abandon |
| Clients | Old clients that already beep on `!passed` start working once backend emits; still need AudioContext fix for audibility |

### 8.2 Compatibility note

Document in `docs/agent-context.md`: Continuous fail-on-stop below coverage is intentional for warning UX; short silence still will not fail an in-progress ayah.

Regression watch: users who pause mid-ayah for &gt; `STREAM_SILENCE_MS` with a partial wrong transcript will now get a fail result + tone + retry waiting. That is **desired** for memorization training. If breath pauses are longer than long-silence in some deployments, tune `STREAM_SILENCE_MS` — do **not** re-disable fail emit.

---

## 9. Implementation plan

### 9.1 Backend (`stream_session.py`)

1. Split `_abandon_incomplete_attempt` usage:
   - **Empty / no-speech** paths → keep abandon (clear UI, no fail).
   - **Non-empty Heard + long silence** → fall through to `assess()` + `ayah.result` + fail_policy (same as coverage-complete fails).
2. Preserve short-silence early return below coverage.
3. Ensure `attempt` increments once per such fail (existing assess path).
4. Clear buffer on fail+retry as today (no glued audio).
5. Tests in `backend/tests/test_stream.py`:
   - Long silence, Heard wrong, coverage 0.5 → exactly one `ayah.result` with `passed=false`
   - Long silence, empty Heard → no `ayah.result` (listening / abandon)
   - Short silence, coverage 0.5 → no `ayah.result`
   - Fail + retry → `session.waiting` after result

### 9.2 Frontend

1. Add `frontend/src/audio/warning-tone.js` (+ unit test with mocked `AudioContext`).
2. Pass capture `audioContext` into Continuous message handler / store a ref set in `startMic`.
3. Replace inline `playWarning()` in `App.vue`.
4. Add `warnedAttemptKey` reset on `session.advance`, `session.ready`, and session end.
5. Manual QA checklist in §11.

### 9.3 Docs

- Update `docs/agent-context.md` Continuous UX bullet: fail tone on discovered mistakes.
- Cross-link from `realtime-stream-spec.md` §11.1 to this spec.
- README one-liner if it still implies tone only for REST fails.

### 9.4 Out of scope for the first PR

- P1 live committed-mistake tone
- Success chime
- Configurable tone frequency in UI settings

---

## 10. Detailed client algorithm (P0)

```text
state:
  captureAudioContext = null
  warnedKey = null

on mic start:
  captureAudioContext = graph.audioContext

on mic stop / session end:
  captureAudioContext = null
  warnedKey = null

function maybeWarnFromResult(msg):
  if msg.passed and not msg.warning: return
  key = `${msg.surah}:${msg.ayah}:${msg.attempt}`
  if key === warnedKey: return
  warnedKey = key
  playWarningTone({ audioContext: captureAudioContext })

on message:
  ayah.result → maybeWarnFromResult(msg)
  session.advance / session.ready → warnedKey = null
```

`playWarningTone`:

```text
ctx = options.audioContext || sharedOrCreate()
if ctx.state === "suspended": await ctx.resume()
if tonePlaying: return
osc → gain(0.2) → ctx.destination
freq 660; stop at t+0.3; tonePlaying clear on end
```

---

## 11. Acceptance criteria

| ID | Criterion |
|----|-----------|
| A1 | Continuous: recite wrong ayah, pause past long silence, non-empty wrong Heard → **one** audible tone |
| A2 | Continuous: same fail does not beep twice for one `attempt` |
| A3 | Continuous: correct ayah pass → **no** tone; advances as today |
| A4 | Continuous: short mid-ayah breath (short silence) with low coverage → **no** fail tone |
| A5 | Continuous: quiet mic / empty Heard long silence → **no** mistake tone |
| A6 | Continuous: tone audible **with mic still on** |
| A7 | `fail_policy=retry` → tone + waiting; `continue` → tone + advance; `stop` → tone + summary |
| A8 | Single ayah REST fail still tones once |
| A9 | Automated stream tests cover A1/A4/A5 server events; frontend tone helper tested with mock context |

---

## 12. Test plan

### 12.1 Backend (pytest)

| Test | Setup | Expect |
|------|-------|--------|
| `test_long_silence_nonempty_low_coverage_emits_fail_result` | Mock STT wrong text; inject silence reason | `ayah.result` `passed=false`; then `session.waiting` if retry |
| `test_long_silence_empty_heard_no_fail_result` | Empty STT | No `ayah.result`; listening/abandon only |
| `test_short_silence_low_coverage_no_fail_result` | Wrong partial; short silence | No `ayah.result` |
| `test_fail_result_includes_alignment_wrong_words` | Classic replace case | Payload usable by UI highlights |

### 12.2 Frontend

| Test | Expect |
|------|--------|
| `warning-tone` resumes suspended context | `resume` called; oscillator started |
| `warning-tone` prefers provided capture context | No second context created when arg passed |
| `maybeWarn` dedupes same attempt | Second call no-op |
| Highlight regression | Provisional partials still pending (unchanged in P0) |

### 12.3 Manual lab (Al-Fatihah)

1. Continuous 1–7, threshold 85%, retry, mic on, partials on.  
2. On 1:2, deliberately say a wrong last word; stop and wait ~1 s.  
3. Expect: fail UI + **one beep** + retry same ayah.  
4. Recite 1:2 correctly → pass, advance, **no** beep.  
5. Repeat with mic still streaming (do not tap Stop) — beep must remain audible.  
6. Compare Single mode intentional fail — beep still works.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Long silence now fails partial takes users meant to continue | Only long silence; keep short silence non-final; document pause guidance in UI hint |
| Beep startles / bleeds into mic | Speakers-only destination; modest gain; never inject into PCM graph |
| Autoplay / suspended context policies | Reuse capture context unlocked by Start Recitation gesture |
| Double beep with future P1 | Shared `warnedKey` for attempt |
| Confusing abandon-with-clear vs fail | Empty Heard stays abandon; non-empty scores |

---

## 14. Decision summary

| Question | Decision |
|----------|----------|
| When is a Continuous mistake “discovered” for P0? | Committed `ayah.result` with fail/warning — including **long silence + non-empty Heard** below coverage |
| Play tone on provisional partial wrongs? | **No** in P0; optional P1 with lock rules |
| How many beeps? | **Once** per `(surah, ayah, attempt)` |
| How to play audio? | Client oscillator; **reuse capture AudioContext** when mic is on |
| New WS event? | **Not required** for P0 |
| Change pass threshold? | **No** |

---

## 15. Implementation checklist

- [x] Backend: long-silence non-empty low-coverage → `ayah.result` fail path  
- [x] Backend tests for silence / empty / short-silence matrix  
- [x] `warning-tone.js` + resume/reuse semantics  
- [x] `App.vue`: Continuous fail uses helper + once-per-attempt guard  
- [x] Single mode switched to same helper  
- [x] Docs: agent-context + cross-link realtime-stream §11.1  
- [ ] Manual lab A1–A8 signed off  

---

## 16. Open questions (resolve before/during P0 PR)

1. Should UI copy on `session.waiting` mention the tone (“Listen for the warning tone, then retry”)? Default: **no** new copy unless lab shows users miss the cue.  
2. Is 660 Hz too sharp over speakers next to a live mic? Lab may lower gain to 0.12 — keep frequency stable for brand consistency with Single.  
3. P1 live tone: ship behind flag or defer until Continuous detection gap (`continuous-vs-single-detection-spec.md`) is closed?
