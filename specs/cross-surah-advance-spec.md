# Continuous Mode: Cross-Surah Advance Spec

**Status:** Implemented  
**Phase:** 2.x (product behavior; protocol fields already exist)  
**Companion:**  
- `realtime-stream-spec.md` (§3.3 stop conditions, §5 `cross_surah`, §9.3 next ayah)  
- `ayah-advance-fix-spec.md` (within-surah auto-advance)  
- `continuous-vs-single-detection-spec.md` (Continuous session lifecycle)  
- `implementation-spec.md` (corpus / `QuranService`)  
**Version:** 1.0  
**Last updated:** 2026-08-14

---

## 1. Purpose

In **Continuous** mode, finishing the **last ayah of the current surah** ends the session and the UI prints **“Session ended.”** The mic / WebSocket stop accepting audio even though the user expected to keep going into the next surah.

This spec:

1. Reconstructs why the last ayah of a surah always terminates today’s Continuous session
2. Separates intentional **range complete** from unwanted **surah wall**
3. Specifies **advance to surah N+1, ayah 1** (keep listening) instead of `session.summary` + “Session ended”
4. Defines when a session **may** still end (Quran end, explicit end range, user stop, timeout)
5. Fixes the related UI failure: session no longer accepting audio after the surah boundary
6. Lists acceptance tests (backend + frontend) and non-goals

**Constraint:** Do not mutate stored Quran text. STT stays behind `SpeechRecognizer`. Reuse `QuranService.next_ayah(..., cross_surah=True)` — do not invent a second corpus navigator. Prefer enabling existing `cross_surah` semantics over new protocol types.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Setup | Surah selected (e.g. Al-Fatihah); **End ayah** = last ayah of that surah (UI default) |
| Flow | User passes ayahs in order until the **final ayah** of the surah |
| Expectation | Session advances to **next surah, ayah 1**; mic stays on; PCM keeps flowing |
| Actual | Status **“Session ended.”**; `session.summary`; session inactive; **UI no longer accepts audio** |

### 2.1 Related symptom — “Session is not accepting audio”

After the surah-final ayah:

1. Server emits `session.summary` (`reason: "range_complete"` or `"surah_complete"`).
2. Frontend sets `sessionActive = false` and status `"Session ended."` (`App.vue` `session.summary` handler).
3. Session state becomes `CLOSED`; WS worker returns / connection winds down.
4. `ws.onclose` calls `stopMic()`.
5. Further PCM is not sent (mic off) and would be rejected anyway (`not_ready` / closed socket).

So “not accepting audio” is not a separate capture bug in the happy path — it is the **consequence of premature session teardown** at the surah boundary. Fixing cross-surah advance must keep the session in `LISTENING` and the client mic streaming.

If audio is refused **before** the last ayah (mid-surah), that is out of scope for this spec — investigate under stream / capture regressions separately.

---

## 3. Background — why it ends today

### 3.1 Frontend always pins an end range

On Continuous start (`App.vue` → `session.start`):

| Field | Current value |
|-------|----------------|
| `end_surah` | `selectedSurah` whenever `endAyah` is set |
| `end_ayah` | `endAyah` (defaults to **last ayah of the selected surah** in `loadAyahOptions`) |
| `cross_surah` | **hardcoded `false`** |

So a typical Fatihah practice session is configured as:

```json
{
  "start_surah": 1,
  "start_ayah": 1,
  "end_surah": 1,
  "end_ayah": 7,
  "cross_surah": false,
  "auto_advance": true
}
```

That is an **inclusive closed range** ending at 1:7 — not “practice until I stop.”

### 3.2 Backend `_advance` stop rules

In `stream_session.py` `_advance()`:

```text
if current == (end_surah, end_ayah):
    → session.summary (range_complete)     # hits after pass on 1:7

nxt = quran.next_ayah(..., cross_surah=config.cross_surah)
if nxt is None:
    → session.summary (surah_complete)     # hits if no end range + cross_surah false

else:
    → session.advance to nxt               # desired path
```

`QuranService.next_ayah` already supports cross-surah:

- Within surah: `(s, a) → (s, a+1)`
- Last ayah + `cross_surah=True`: `(s, last) → (s+1, 1)` (or next surah present in corpus order)
- Last ayah + `cross_surah=False`: `None` → `surah_complete`

Protocol already documents this (`realtime-stream-spec.md` §5 / §9.3). The gap is **product defaults + UI**, not missing navigation code.

### 3.3 Stop conditions vs product intent

| Spec stop condition (`realtime-stream-spec` §3.3) | Today’s Continuous UI | Desired Continuous default |
|---------------------------------------------------|------------------------|----------------------------|
| Client `session.stop` | Yes | Yes (unchanged) |
| Configured `end_surah`/`end_ayah` completed | Always (End ayah defaults to surah last) | Only when user **intentionally** set a closed range that should stop |
| End of surah + `cross_surah=false` | Always | **No** — default `cross_surah=true` |
| Idle / max session timeout | Yes | Yes |
| Fatal error | Yes | Yes |
| End of Quran (114 last ayah, no next) | Via `nxt is None` | Yes → summary `quran_complete` (or keep `surah_complete` with clear reason) |

---

## 4. Goals

**G1.** Passing the last ayah of surah N with Continuous defaults must emit `session.advance` to **N+1:1** (or the next surah present in corpus order), **not** `session.summary` + “Session ended.”

**G2.** Mic and WebSocket must stay open across that boundary; PCM continues to be accepted without reconnect or pressing Start again.

**G3.** UI status / live ayah must update to the new surah:ayah (e.g. `Listening — 2:1`), never “Session ended” for a mid-Quran surah wall.

**G4.** Explicit closed ranges must still stop: if the user configures an end that is **not** “open practice past this surah,” completing that end still yields `session.summary` `range_complete`.

**G5.** Completing the **last ayah of the last surah in the corpus** (114 in full corpus) still ends the session cleanly with a summary — there is no next surah.

**G6.** Surah / ayah selectors (or at least the live display) stay coherent when the session crosses a surah boundary while `continuousBusy` locks the form.

---

## 5. Non-goals

| Out of scope | Why |
|--------------|-----|
| Changing pass / coverage thresholds | Unrelated to surah boundary |
| Leftover-carry / pause-less tilawah (v1.1) | Separate buffer issue after advance |
| Auto-scrolling the Surah dropdown list UX polish beyond G6 | Nice-to-have; live ayah is enough for P0 |
| Multi-user progress DB / “resume where I left off” | Phase later |
| Changing Single-ayah REST mode | REST has no session advance |
| Inventing a new WS event type for surah change | Reuse `session.advance` with `to.surah` changed |

---

## 6. Product decision (defaults)

### 6.1 Recommended default: open Continuous practice

Continuous mode defaults to **keep going across surahs until the user stops** (or hits Quran / timeout / fatal).

| Setting | New Continuous default | Notes |
|---------|------------------------|-------|
| `cross_surah` | `true` | Enable N → N+1:1 |
| End range | See §6.2 | Avoid accidental `range_complete` on surah last ayah |

### 6.2 End-ayah semantics (choose one; implement A unless UX objects)

**Option A — Open end by default (preferred)**

- UI: End ayah control becomes optional (“Until I stop” / empty / “Open”).
- Default: `end_surah=null`, `end_ayah=null`, `cross_surah=true`.
- User may still pick a closed end (same or later surah) for drill ranges.
- Completing surah last ayah → `session.advance` to next surah.

**Option B — Keep End ayah UI, but do not treat surah-last as stop when it equals “full surah”**

- If `end_surah == start_surah` and `end_ayah == last_ayah(start_surah)` and no explicit “Stop at end” flag → treat as open (`end_*=null`) + `cross_surah=true` on the wire.
- Risk: users who genuinely want “only this surah” lose that without a new checkbox.

**Option C — Keep closed end, add “Continue to next surah” checkbox (default on)**

- When checked: `cross_surah=true` and either null the end or set `end_surah`/`end_ayah` to Quran end.
- When unchecked: today’s behavior (stop at End ayah).

**Decision for this spec:** implement **Option A**. If End ayah remains in the UI for familiarity, default it to **Open / until stop**, not last ayah. Closed ranges stay available for deliberate drills.

### 6.3 Explicit closed-range rule (must preserve)

If `end_surah` / `end_ayah` are set and the session advances **onto or past** that inclusive end, emit `session.summary` `range_complete` as today. Cross-surah only continues while the next ayah is still **at or before** the configured end in corpus order.

Examples:

| Start | End | After pass on | Expected |
|-------|-----|---------------|----------|
| 1:1 | null / open | 1:7 | `session.advance` → 2:1 (or next corpus surah) |
| 1:1 | 1:7 closed | 1:7 | `session.summary` `range_complete` |
| 1:5 | 2:5 | 1:7 | `session.advance` → 2:1 (requires `cross_surah=true`) |
| 1:5 | 2:5 | 2:5 | `session.summary` `range_complete` |
| 114:last | open | 114:last | `session.summary` (no next; prefer reason `quran_complete`) |

---

## 7. Protocol & backend

### 7.1 Existing fields (no new message types required)

| Field | Behavior |
|-------|----------|
| `cross_surah` | Must be `true` for Continuous open practice |
| `end_surah` / `end_ayah` | `null` for open practice; both set for closed drills |
| `session.advance` | `to: { surah, ayah, text, … }` already carries the new surah |
| `session.summary` | Only for real stop reasons (§6.3 / §3.3) |

Optional clarity (same PR or follow-up):

- Add summary reason `quran_complete` when `next_ayah` is `None` at corpus end (today often `surah_complete`).
- Keep `surah_complete` only when `cross_surah=false` and last ayah of current surah is finished.

### 7.2 `_advance` behavior (normative)

```text
on accepted advance trigger:
  if closed_range and current == (end_surah, end_ayah):
      emit session.summary(reason=range_complete)
      return

  nxt = next_ayah(current, cross_surah=config.cross_surah)
  if nxt is None:
      emit session.summary(reason=quran_complete | surah_complete)
      return

  current = nxt
  emit session.advance(from=..., to=..., reason=...)

  if closed_range and corpus_order(current) > corpus_order(end):
      emit session.summary(reason=range_complete)   # existing edge guard
```

Open practice: `cross_surah=true`, no end → path through `session.advance` at every surah boundary until Quran end.

### 7.3 Validation

Existing start validation already rejects `end_surah != start_surah` when `cross_surah=false`. With Continuous default `cross_surah=true`, multi-surah closed ranges become valid without error.

Do not loosen corpus integrity checks.

### 7.4 Session must stay `LISTENING`

Crossing a surah must **not**:

- Set `SessionState.CLOSED`
- Emit `session.summary`
- Close the WebSocket
- Require a new `session.start`

Overlap / buffer clear rules after pass-advance stay as today (`STREAM_OVERLAP_MS`).

---

## 8. Frontend

### 8.1 `session.start` payload

| Field | Change |
|-------|--------|
| `cross_surah` | Send `true` for Continuous open practice (replace hardcoded `false`) |
| `end_surah` / `end_ayah` | Send `null` when End ayah is Open / unset; only set both when user chose a closed end |

### 8.2 End ayah control

- Default to **Open (until I stop)** rather than last ayah of surah.
- When Open: do not send end fields.
- When a number is selected: closed range on `selectedSurah` for v1 (same-surah end), **or** document a later UX for end-surah ≠ start-surah.

### 8.3 `session.advance` handler

Already updates `currentAyah` from `msg.to`. Extend so that when `msg.to.surah` differs from `selectedSurah`:

1. Keep mic on / do not call `stopMic`.
2. Status stays listening-style: `Listening — {surah}:{ayah}` (never “Session ended”).
3. Optionally sync `selectedSurah` / reload `ayahOptions` for display consistency **without** tearing down the session (watchers must not call `ensureSession` or reset WS). Guard: ignore surah `watch` side effects while `sessionActive`.

### 8.4 `session.summary` handler

Keep `"Session ended."` **only** for real summaries. Prefer a slightly richer status when useful:

| `reason` | Suggested status |
|----------|------------------|
| `user_stop` | Session ended. |
| `range_complete` | Range complete. |
| `quran_complete` / last-surah `surah_complete` | Quran complete. / Surah complete. |
| `session_timeout` | Session timed out. |

Do not show “Session ended” on `session.advance`.

### 8.5 Audio acceptance (regression guard)

While `sessionActive && micActive` and WS is `OPEN`:

- PCM chunks must continue to `ws.send` after a cross-surah `session.advance`.
- No forced `stopMic` on advance.
- `ensureSession` must reuse the open session (already does when `sessionActive`).

---

## 9. Acceptance tests

### 9.1 Backend (`test_stream.py`)

| ID | Setup | Expect |
|----|-------|--------|
| **S1** | Open range, `cross_surah=true`, on last ayah of surah 1, pass | `session.advance` to next surah ayah 1; **no** `session.summary` |
| **S2** | Closed `end_surah/end_ayah` = last of start surah, `cross_surah` irrelevant | Pass last ayah → `session.summary` `range_complete` |
| **S3** | `cross_surah=false`, no end, last ayah | `session.summary` `surah_complete` (legacy) |
| **S4** | Closed multi-surah range e.g. 1:6→2:2, `cross_surah=true` | Pass 1:7 → advance 2:1; pass 2:2 → `range_complete` |
| **S5** | Last ayah of last fixture/corpus surah, open + `cross_surah=true` | `session.summary` (no next) |
| **S6** | Existing within-surah advance tests | Unchanged |

Update fixture-aware expectation: sample corpus has surah **1** then **36**, so `next_ayah(1, 7, cross_surah=True) == (36, 1)` remains correct for tests; full corpus uses `(2, 1)`.

### 9.2 Frontend (manual / e2e light)

| ID | Steps | Expect |
|----|-------|--------|
| **U1** | Continuous, Open end, recite through Fatihah 1:7 pass | Live ayah becomes next surah 1; status listening; **mic on**; no “Session ended” |
| **U2** | Keep speaking into next surah | Live % / Heard update; audio accepted |
| **U3** | Closed End ayah = 7 on surah 1 | After 1:7 pass → summary / ended (deliberate stop) |
| **U4** | User presses End session mid-surah | Summary; mic off (unchanged) |
| **U5** | Cross-surah advance with form locked (`continuousBusy`) | No accidental reconnect / surah reload tearing WS |

### 9.3 Lab trace (`?lab=1`)

After pass on surah-final ayah (open practice):

```text
ayah.result (passed, will_advance=true)
session.advance { from: {surah:N, ayah:last}, to: {surah:N+1, ayah:1}, … }
```

Must **not** contain `session.summary` between those events.

---

## 10. Implementation plan

1. **Frontend defaults** — `cross_surah: true`; End ayah Open by default; only send end fields when closed.
2. **Frontend advance UX** — status + optional selector sync; guard watchers during active session; never stop mic on advance.
3. **Backend** — confirm open + `cross_surah` path; optional `quran_complete` reason; add S1–S5 tests.
4. **Docs** — update `realtime-stream-spec.md` §3.3 / §5 defaults note; one line in `docs/agent-context.md`.
5. **Manual verify** — U1/U2 on docker compose Continuous through Fatihah → next surah with mic still on.

P0 = steps 1–3 + U1/U2. Richer status strings and `quran_complete` can ship in the same PR.

---

## 11. Risks & edge cases

| Risk | Mitigation |
|------|------------|
| Users who wanted “only this surah” now continue | Closed End ayah still available (U3); document default change |
| Surah `watch` reloads options and resets start/end mid-session | Guard while `sessionActive` / `continuousBusy` |
| Fixture vs full corpus next surah number | Tests assert against `QuranService.next_ayah`, not hard-coded `2` unless full corpus |
| Very long open sessions | Existing `STREAM_MAX_SESSION_S` / idle timeout still apply |
| `fail_policy=continue` on last ayah | Same advance rules; still cross-surah when configured |
| `fail_policy=stop` on last ayah | Still ends via fail path (unchanged) |

---

## 12. Success criteria

- [x] Open Continuous practice: last ayah of a non-final surah → `session.advance` to next surah ayah 1
- [x] UI does **not** show “Session ended” at that boundary
- [x] Mic stays on; PCM continues; session keeps assessing the new ayah
- [x] Closed end range still ends with `range_complete`
- [x] Quran / corpus end still summarizes cleanly
- [x] S1–S5 (or equivalent) pass in `pytest`

---

## 13. Open questions

1. Should Open end hide the End ayah control entirely, or show a sentinel option (“Until I stop”)?
2. For closed multi-surah drills, is same-surah End ayah enough for MVP, or do we need End surah + End ayah selectors now?
3. Rename UI copy from “Session ended.” to reason-specific strings in the same PR or follow-up?

**Recommendation:** sentinel “Until I stop” option (clear UX); same-surah closed end for MVP; reason-specific status in the same PR if cheap.
