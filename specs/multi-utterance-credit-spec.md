# Continuous Mode: Multi-Utterance Word Credit & Advance Spec

**Status:** Ready for implementation  
**Phase:** 2.x (product behavior; extends stream session state, not REST Single)  
**Companion:**  
- `realtime-stream-spec.md` (§8 boundary detection, §9 advance)  
- `ayah-advance-fix-spec.md` (coverage gate vs score)  
- `continuous-vs-single-detection-spec.md` (silence / buffer lifecycle)  
- `continuous-mistake-tone-spec.md` (when incomplete silence scores a fail)  
- `partials-evaluation-spec.md` (live `partial.alignment` / progress)  
- `implementation-spec.md` (assessor / normalizer non-negotiables)  
**Version:** 1.0  
**Last updated:** 2026-08-15

---

## 1. Purpose

In **Continuous** mode a user may recite one ayah across **multiple spoken utterances** (natural breaths, thinking pauses, or deliberate chunking). Today each silence-finalized STT window is scored **in isolation** against the full ayah. If the user already matched the opening words in an earlier utterance, then continues from the **next** words and gets the remainder right, the live UI can show:

- Earlier words as already heard / credited (or muted as no longer in the current Heard window)
- Current Heard = only the continuation (e.g. last three tokens)
- Live coverage ≈ fraction of the **current** window (e.g. **38% in progress**)
- Session **stuck** on the same ayah — no `ayah.result` pass, no `session.advance`

This is incorrect for memorization practice: the user did not miss any word; they split the ayah across turns.

This spec:

1. Reconstructs the failure from a real **2:3** Continuous session (multi-chunk continuation)
2. Defines **session-scoped word credit** for the current ayah
3. Specifies when cumulative credit means the ayah is **complete** → **pass + auto-advance**
4. Separates **continuation pauses** (keep credit) from **failed attempts** (clear / retry)
5. Defines protocol fields, UI semantics, tests, and non-goals

**Constraint:** Do not mutate stored Quran text. STT stays behind `SpeechRecognizer`. REST `/assess` stays single-shot (no credit state). Reuse `MemorizationAssessor` match rules (`equal` or fuzzy replace ≥ `WORD_MATCH_THRESHOLD`). Do **not** lower `DEFAULT_THRESHOLD` or `STREAM_COVERAGE_THRESHOLD` to fake a pass on an incomplete credit set.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Current ayah | **2:3** `ٱلَّذِينَ يُؤْمِنُونَ بِٱلْغَيْبِ وَيُقِيمُونَ ٱلصَّلَوٰةَ وَمِمَّا رَزَقْنَٰهُمْ يُنفِقُونَ` |
| Mic | on |
| Prior utterance(s) | Opening tokens already matched / treated as credited by the user |
| Current Heard | `وَمِمَّا رَزَقْنَٰهُمْ يُنفِقُونَ` (continuation only) |
| Live % | **~38% in progress** (≈ 3 / 8 expected tokens in the **current** window) |
| Highlights | Prefix words muted / not in current match set; continuation chips green (`word-match`) |
| Expectation | Ayah complete → **pass** → **advance to 2:4** |
| Actual | Remains on 2:3; coverage gate never clears; no pass-advance |

### 2.1 What the UI is showing

| Layer | Source today | Meaning in this bug |
|-------|--------------|---------------------|
| Header `2:3 · mic on · 38% in progress` | `partial.alignment.progress` | Coverage of **this** STT window only |
| Green chips on last words | Current `alignment` equals | Continuation matched as a suffix of the ayah |
| Muted / non-green earlier words | Provisional deletes / pending, or sticky previous paint | Prefix not in **current** Heard — credit is **not** session state |
| Heard line | Confidence-filtered current transcript | Correct for the latest utterance; incomplete for the ayah |
| Auto-advance | `ayah.result` `passed=true` | **Did not happen** |

### 2.2 Root cause (product + code)

```text
Utterance 1:  [word0 … word4]  → partial progress high, then silence / buffer trim
Utterance 2:  [word5 … word7]  → progress() on window alone ≈ 0.38
                              → < STREAM_COVERAGE_THRESHOLD (0.85)
                              → no finalize / no pass-advance
```

`MemorizationAssessor.progress()` already takes the **best recognized suffix** within **one** `recognized` string. It does **not** remember matches from prior utterances on the same ayah. `StreamSession` clears or re-windows audio without a durable **credit mask**.

This is **not**:

| Prior bug | Why it does not explain this screenshot |
|-----------|----------------------------------------|
| `ayah-advance-fix-spec.md` | Heard was semantically complete in one window; coverage under-counted fuzzy tokens |
| `continuous-vs-single-detection-spec.md` | Wrong / truncated Heard on a single take |
| Leftover carry (`realtime-stream-spec.md` §8.2) | Surplus tokens from ayah N applied to N+1 after a **full** pass — opposite direction |

---

## 3. Goals and non-goals

### 3.1 Goals

| ID | Goal |
|----|------|
| G1 | User may recite an ayah in **≥ 1 utterances** and still advance if **every** expected token is eventually credited and **no** required token was skipped |
| G2 | Continuation from the **next uncredited** word (suffix / mid-ayah resume) counts toward completion when the prefix is already credited |
| G3 | When the credit set covers the full ayah, emit `ayah.result` with **`passed=true`** and auto-advance (same path as a single full utterance) |
| G4 | Live progress % and word chips reflect **cumulative credit ∪ current window**, not current window alone |
| G5 | Wrong words, skips, or abandoned fails still surface mistakes; do not silently pass incomplete ayahs |
| G6 | Backend owns credit state; clients do not invent Arabic matching |

### 3.2 Non-goals

- Changing REST Single ayah (one upload = one assess)
- Tajweed / timing credit
- Persisting credit across page reload / new WebSocket session
- Leftover carry into the **next** ayah (still P2 in agent-context; this spec is **within** one ayah)
- Accepting a scrambled word order as complete
- Lowering coverage or accuracy thresholds

---

## 4. Definitions

| Term | Definition |
|------|------------|
| **Expected tokens** | `tokenize(normalize_arabic(expected_text))` — length `N`. Display chips stay Uthmani surfaces; credit indices map 1:1 to display words after the same tokenization rules used by the assessor |
| **Utterance** | One STT segment produced after silence / probe / force from the current attempt buffer (or partial window for UX only) |
| **Match** | Same rule as alignment promotion: exact `equal`, or `replace` with `fuzz.ratio ≥ WORD_MATCH_THRESHOLD` |
| **Credit mask** | Boolean vector `C[0..N)` on the **current ayah**. `C[i]=true` means expected token `i` has been accepted as recited in this ayah’s credit lifetime |
| **Credit cursor** | Smallest index `k` such that all `C[0..k)` are true (contiguous **prefix** credit). `k = N` means the ayah is fully credited |
| **Current window matches** | Match alignment of this utterance’s `recognized` against expected (existing assessor / progress logic, including best suffix) |
| **Cumulative coverage** | `\|{i : C[i]}\| / N` after merging the current window into `C` (for contiguous-prefix mode this equals `k / N`) |
| **Ayah complete (credit)** | Contiguous prefix credit with `k == N` (all tokens credited, no holes) |

**Contiguous prefix (normative for v1):** Credits only extend as a **gap-free prefix**. Matching a later word while an earlier index is still uncredited does **not** fill the hole and does **not** complete the ayah.

Rationale: memorization practice requires the user not to skip words. The screenshot case (prefix already credited, user speaks the remaining suffix) is exactly prefix extension.

---

## 5. Desired user journeys

### 5.1 Happy path — split ayah (screenshot)

```text
Ayah 2:3, N = 8 tokens (illustrative)

U1: user says tokens 0..4 correctly
    → credit cursor k = 5
    → cumulative coverage ≈ 0.625
    → short/long silence: keep listening; do NOT fail-clear credit
    → UI: tokens 0..4 credited; Heard may show U1 text

U2: user starts at token 5; says 5..7 correctly
    → merge → k = 8
    → ayah complete → ayah.result passed=true → session.advance → 2:4
```

### 5.2 Happy path — overlap re-say

User re-says tokens 3..7 after `k=5`. Overlap re-confirms already credited tokens; cursor advances to 8. Still pass-advance.

### 5.3 Happy path — single utterance

No change: one window matches full ayah → credit jumps to `N` → same finalize / pass / advance as today.

### 5.4 Incomplete pause (breath)

`k < N`, silence, Heard empty or only noise → **abandon audio buffer**, **retain credit**, emit `session.listening` (or equivalent). No warning tone. User continues later.

### 5.5 Wrong continuation

`k=5`, user says a wrong token at position 5 (or skips to a later word without matching 5):

- Do **not** advance cursor past the mismatch
- Do **not** mark holes as credited
- On long silence with a non-empty wrong Heard that does not extend credit: apply existing fail / tone / `fail_policy` rules for **this utterance**, then:
  - **`fail_policy=retry` (default):** clear **attempt audio**; **credit policy = keep prefix credit** (see §7.4) so the user can retry from the next uncredited word without re-saying the whole ayah
  - Optional strict mode (config, default off): clear entire credit mask on scored fail

### 5.6 Skip / force advance / next ayah

Any `session.advance` (pass, continue policy, skip) **resets** credit for the new ayah.

---

## 6. Algorithm (normative)

### 6.1 State on `StreamSession` (per current ayah)

```text
credit_mask: list[bool]   # length N, all False on ayah enter
# derived:
#   credit_cursor = first False index, or N if all True
#   cumulative_coverage = credit_cursor / N   # contiguous mode
```

Reset `credit_mask` when:

1. `session.start` / new session
2. After `session.advance` (new current ayah)
3. Optional: scored fail with `STREAM_CREDIT_CLEAR_ON_FAIL=true`
4. Never on short silence, empty abandon, or successful partial credit merge

### 6.2 Merge one utterance into credit

Input: `expected_text`, `recognized` (post confidence filter + Heard recovery), current `credit_mask`.

```text
1. expected = tokenize(expected)
2. N = len(expected); if N == 0: treat as complete (edge)
3. k = credit_cursor(credit_mask)   # first uncredited index
4. Align recognized against expected using the same match rules as
   MemorizationAssessor (SequenceMatcher + fuzzy replace ≥ word threshold).
5. Prefer the alignment hypothesis that extends the contiguous prefix
   starting at k (see §6.3).
6. For each expected index i matched under that hypothesis with i >= k
   contiguous from k:
       credit_mask[i] = True
7. Do not set credit_mask[j] for j < k (already true) except optional
   re-confirm (idempotent).
8. Never set credit_mask[i]=True if any hole exists in [k..i).
9. Return updated mask + new_cursor + cumulative_coverage
```

### 6.3 Alignment hypotheses (continuation-aware)

When merging, evaluate at least:

| Hypothesis | How | Why |
|------------|-----|-----|
| **H-full** | `progress` / align `recognized` vs full `expected` | User re-says from the start |
| **H-suffix** | Best recognized suffix vs full expected (existing `progress`) | STT preamble / Basmala bleed |
| **H-resume** | Align `recognized` vs `expected[k:]` only; matches map to indices `k + j` | Screenshot case: user starts at next words |

Choose the hypothesis that yields the **largest new credit_cursor** without creating holes. Tie-break: prefer H-resume when `k > 0` and H-full would leave cursor unchanged while H-resume advances.

**Must not:** credit `expected[k+2]` from a window that failed to match `expected[k]` / `expected[k+1]`.

### 6.4 Completion and pass decision

After merge (on probe tick, silence assess, or force):

```text
cumulative_coverage = credit_cursor / N

if credit_cursor == N:
    ayah_complete = True
else:
    ayah_complete = False
```

**Advance path when `ayah_complete`:**

1. Build a **credit-complete assessment** (§6.5)
2. Emit `ayah.result` with `passed=true`, `will_advance` per `auto_advance`, `coverage=1.0` (or `cumulative_coverage`), `credit_complete=true`
3. Emit `session.advance` as today
4. Reset credit for the next ayah

**Do not** require the **current** window alone to satisfy `STREAM_COVERAGE_THRESHOLD` when cumulative credit already reached `N`.

**While `not ayah_complete`:**

| Trigger | Behavior |
|---------|----------|
| Short silence | Keep buffer policy as today for mid-phrase breath; **keep credit**; refresh partials with cumulative fields; no fail |
| Long silence + empty Heard | Abandon buffer; **keep credit**; `session.listening` cleared; no fail / no tone |
| Long silence + Heard that did not complete ayah | If window introduced a **committed mismatch** at cursor → scored fail + tone per mistake-tone spec; credit per §7.4. If window only matched a proper prefix extension (`k` advanced but `< N`) → **incomplete continuation**: **no** fail tone; keep credit; clear or keep audio per §7.3; stay listening |
| Coverage probe | Use **cumulative_coverage** (not window-only) for stable ticks / auto-finalize |
| `ayah.force_assess` | Merge current buffer once; if complete → pass-advance; if not → return result with `passed=false`, `credit_cursor`, `cumulative_coverage` (user sees what’s left); do not wipe credit unless fail-clear config |

### 6.5 Passing score when credit-complete

When completion is reached via multi-utterance credit, the last window may be only a suffix — character `fuzz.ratio(expected, recognized_window)` can be low even though every word was credited across turns.

**Normative score for credit-complete pass:**

```text
score = max(
    session.threshold,                          # at least a clear pass
    assessor.assess(expected, synthetic).score  # optional quality signal
)
passed = True
```

Where `synthetic` is one of (implementation may pick **A** for v1):

| Option | Synthetic recognized text | Notes |
|--------|---------------------------|-------|
| **A (v1)** | Join display/normalized expected tokens for all credited indices (i.e. full expected) | Score ≈ 1.0; honest “ayah filled”; simplest |
| **B** | Concatenate per-token best recognized surfaces stored when each index was first credited | Closer to what was heard; more state |
| **C** | `max(window_score, threshold)` only | Avoid; can look like a low pass for a fully credited ayah |

`wrong_words` / `missing_words` on a credit-complete pass must be **empty**. `alignment` must show all expected tokens as `equal` (for UI green chips on advance).

`message`: e.g. `Completed across multiple utterances.` when `credit_utterances > 1`; else keep existing excellent message.

### 6.6 Interaction with window-only coverage gate

Replace “finalize when `progress(expected, window) ≥ 0.85`” with:

```text
finalize_coverage = cumulative_coverage_after_merge(window)
```

Stable ticks (`STREAM_COVERAGE_STABLE_TICKS`) apply to **cumulative** coverage ≥ `STREAM_COVERAGE_THRESHOLD`, **or** `credit_cursor == N`.

Window-only `progress` remains useful as debug (`window_coverage`) but must not block advance when cumulative is complete.

---

## 7. Policy details

### 7.1 Contiguous prefix only (v1)

Allowed credit shapes: `11111000`, `11111111`, `00000000`.  
Forbidden: `11101111`, `00111000` (holes or non-prefix islands).

### 7.2 Starting mid-ayah without prior credit

If `k=0` and the user only says the last three words of 2:3:

- H-resume matches a suffix of expected, but indices are **not** contiguous from 0
- Cursor stays 0; cumulative coverage stays 0
- Do **not** pass-advance  
This preserves “must not skip words.”

### 7.3 Audio buffer after partial credit

After a silence that **extended** credit but left `k < N`:

- Prefer **clear attempt buffer** (keep small overlap) so the next utterance is not glued to the previous PCM
- Retain `credit_mask`
- Re-arm VAD / listening

After silence that **did not** change credit and Heard is empty: existing abandon path + retain credit.

### 7.4 Credit on scored fail (`fail_policy=retry`)

**Default (`STREAM_CREDIT_KEEP_ON_FAIL=true`):** keep contiguous prefix credit; clear only tokens at/after the first mismatch if they were tentatively set in the failed window (normally they were never committed). User retries from the next uncredited word.

**Strict (`STREAM_CREDIT_KEEP_ON_FAIL=false`):** clear entire mask on any `ayah.result` with `passed=false`. Closer to “whole ayah retry.”

### 7.5 Mistake tone

| Situation | Tone? |
|-----------|-------|
| Partial credit + listening (incomplete, no committed wrong) | No |
| Credit-complete pass | No |
| Scored fail at cursor (wrong/missing committed) | Yes (existing Continuous mistake tone) |
| `session.waiting` after fail | Do not double-beep |

Update `continuous-mistake-tone-spec.md` acceptance: long silence with **prefix-only progress and no mismatch** is **incomplete continuation**, not a fail — **no** tone. This **narrows** the earlier “long silence + non-empty Heard below coverage → fail” rule to cases where the window is a **failed attempt** (mismatch / cannot extend credit), not a successful partial chunk.

### 7.6 Partials UX

`partial.alignment` / progress must expose cumulative credit:

- Chips for `C[i]=true` → `match` (credited), even if not in current Heard
- Current window matches beyond cursor → `match` (live)
- Uncredited not in window → `pending`
- Provisional mismatches still `pending` until final fail (unchanged)

Heard line may continue to show **current utterance only** (what was just heard). Optional later: `Heard (this turn)` vs `Credited so far`.

### 7.7 Config knobs

```text
STREAM_MULTI_UTTERANCE_CREDIT=true          # master switch (default true in Continuous)
STREAM_CREDIT_KEEP_ON_FAIL=true             # §7.4 default
STREAM_CREDIT_CLEAR_ON_FAIL=false           # alias / inverse of keep
STREAM_CREDIT_REQUIRE_CONTIGUOUS=true       # v1 always true; no sparse mode yet
```

---

## 8. Protocol / API

### 8.1 Events — additive fields

#### `partial.alignment`

```json
{
  "type": "partial.alignment",
  "progress": 0.625,
  "window_coverage": 0.375,
  "credit_cursor": 5,
  "credit_total": 8,
  "alignment": ["…"],
  "provisional": true
}
```

| Field | Meaning |
|-------|---------|
| `progress` | **Cumulative** coverage (`credit_cursor / credit_total`) — drives UI `%` |
| `window_coverage` | Optional; coverage of this STT window alone |
| `credit_cursor` | Contiguous credited prefix length |
| `credit_total` | `N` |

#### `ayah.result`

```json
{
  "type": "ayah.result",
  "passed": true,
  "score": 1.0,
  "coverage": 1.0,
  "credit_complete": true,
  "credit_utterances": 2,
  "credit_cursor": 8,
  "credit_total": 8,
  "will_advance": true,
  "trigger": "silence",
  "alignment": ["… all equal …"]
}
```

On incomplete fail:

```json
{
  "passed": false,
  "credit_complete": false,
  "credit_cursor": 5,
  "credit_total": 8,
  "coverage": 0.625,
  "will_advance": false
}
```

#### `session.advance`

Unchanged shape. Credit reset is server-side; client clears live chips as today.

#### `session.listening`

Optional: `credit_cursor`, `credit_total` when `cleared: true` after incomplete abandon so UI can keep credited chips.

### 8.2 Client responsibilities

1. Bind live `%` to `progress` (cumulative), not `window_coverage`.
2. Paint credited prefix as match even when Heard shows only the continuation.
3. On `ayah.result` pass + `session.advance`, clear credit UI for the new ayah (server already reset).
4. Do not locally invent credit masks.

### 8.3 REST

No change. Single upload has no multi-utterance credit.

---

## 9. Worked example (2:3 screenshot)

Expected tokens (illustrative indices after normalize/tokenize):

| i | Token (display approx.) |
|---|-------------------------|
| 0 | ٱذين |
| 1 | يؤمنون |
| 2 | بالغيب |
| 3 | ويقيمون |
| 4 | الصلوة |
| 5 | ومما |
| 6 | رزقنهم |
| 7 | ينفقون |

**After U1** (user said 0..4): `credit_cursor=5`, `progress=0.625`.

**U2 Heard:** `ومما رزقنهم ينفقون` → H-resume against `expected[5:]` → all three match → `credit_cursor=8` → **complete**.

Emit pass with `credit_complete=true`, `credit_utterances=2`, advance to **2:4**.

Without this spec: window `progress≈0.375`, stuck “38% in progress.”

---

## 10. Implementation plan

### 10.1 Backend

| Step | Change |
|------|--------|
| 1 | Add credit state + reset hooks on ayah enter / advance in `stream_session.py` |
| 2 | Extract `merge_credit(expected, recognized, mask, cursor) -> CreditMergeResult` next to assessor (pure, unit-tested) — keep STT out of routers |
| 3 | Call merge from partial probe path and `run_assess` before coverage decisions |
| 4 | Gate finalize / pass on cumulative completion (§6.4–6.5) |
| 5 | Adjust long-silence fail vs incomplete-continuation (§7.5) |
| 6 | Extend event payloads (§8.1) |
| 7 | Settings in `config.py` (§7.7) |

### 10.2 Frontend

| Step | Change |
|------|--------|
| 1 | Use cumulative `progress` / optional `credit_cursor` for status `%` |
| 2 | `wordsFromAlignment` or App state: show credited prefix as `match` across partial updates (may need `credit_mask` or cursor in events) |
| 3 | Keep Heard = current turn |
| 4 | No protocol inventing on the client |

### 10.3 Docs

- Link from `docs/agent-context.md` (follow-ups / Continuous behavior)
- Cross-link from `realtime-stream-spec.md` §8–9
- Note mistake-tone narrowing in `continuous-mistake-tone-spec.md`

---

## 11. Acceptance tests

### 11.1 Backend unit — `merge_credit`

| ID | Setup | Input | Expect |
|----|-------|-------|--------|
| C1 | mask empty | full ayah recognized | cursor → N; complete |
| C2 | cursor=5 on 2:3 | suffix tokens 5..7 only | cursor → 8; complete |
| C3 | cursor=0 | suffix tokens 5..7 only | cursor stays 0; not complete |
| C4 | cursor=5 | wrong token at 5 | cursor stays 5; no holes filled |
| C5 | cursor=5 | tokens 3..7 (overlap) | cursor → 8; complete |
| C6 | cursor=4 | tokens 5..7 (skips 4) | cursor stays 4 |
| C7 | Fuzzy dagger-alef / simple Arabic | same as assessor match rules | credits when alignment would `equal` |

### 11.2 Backend stream integration

| ID | Scenario | Expect |
|----|----------|--------|
| S1 | Two utterances: prefix then suffix; silences between | One `ayah.result` `passed=true`, `credit_complete=true`, then `session.advance` |
| S2 | Same as S1 with `auto_advance=true` | Next ayah payload correct |
| S3 | Prefix only + long silence + empty | No fail; credit retained; listening |
| S4 | Prefix only + long silence + wrong Heard at cursor | Fail + tone path; credit keep/clear per config |
| S5 | Full ayah one utterance | Still pass-advance; `credit_utterances=1` or omit |
| S6 | Cumulative coverage ≥ 0.85 via credit but window_coverage low | Probe/silence **may** finalize; must pass when cursor==N |
| S7 | Advance resets credit | New ayah starts cursor 0 |
| S8 | `STREAM_MULTI_UTTERANCE_CREDIT=false` | Legacy window-only behavior (screenshot stays stuck) |

### 11.3 Frontend / manual

| ID | Check |
|----|-------|
| U1 | 2:3 split as screenshot → advances to 2:4 with pass status |
| U2 | Live % grows across utterances (not stuck at last-window %) |
| U3 | Credited prefix stays visually matched while Heard shows continuation |
| U4 | Starting mid-ayah with no prior credit does **not** advance |
| U5 | Mistake tone still plays on real wrong continuation; not on successful partial chunk pause |

---

## 12. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| False advance by crediting sparse suffix matches | Contiguous prefix only (§7.1, C3/C6) |
| Conflict with mistake-tone long-silence fail | Narrow fail to mismatch / non-extending wrong Heard (§7.5) |
| STT hallucinating later ayah words mid-credit | Resume align only accepts matches at cursor; extras = insert, no skip-ahead credit |
| Score looks “fake” at 100% on multi-utterance | Document Option A; optional message `Completed across multiple utterances.` |
| Memory of wrong credit after a bad merge | Only commit matches under chosen hypothesis; unit-test C4–C6 |
| Double finalize | Same attempt locks as today’s coverage streak / assess busy flags |

---

## 13. Open questions (defaults proposed)

| # | Question | Default |
|---|----------|---------|
| 1 | Keep credit after scored fail + retry? | **Yes** (`STREAM_CREDIT_KEEP_ON_FAIL=true`) |
| 2 | Synthetic score Option A vs B? | **A** for v1 |
| 3 | Should short ayahs (N≤2) allow multi-utterance credit? | **Yes** (same rules) |
| 4 | Expose `credit_mask` bit array to client? | **No** — cursor + alignment enough for v1 |
| 5 | Apply credit merge on provisional partials or only on silence/probe final STT? | Merge on **probe + final** STT; partials may show tentative cursor but only **commit** on the same paths that today trust completion probes / `run_assess` |

---

## 14. Success criteria

1. The 2:3 screenshot scenario **pass-advances** without requiring the user to re-say already credited words.
2. No advance if any expected word was never credited (no skips).
3. Live progress reflects cumulative credit.
4. Existing single-utterance Continuous passes and mistake-tone fails still behave correctly under tests S4–S5 and U5.
5. REST Single and corpus immutability unchanged.

---

## 15. Summary for implementers

**Problem:** Coverage and pass decisions use only the latest STT window, so a correct multi-chunk ayah never reaches 0.85 / never passes.

**Fix:** Maintain a contiguous **credit cursor** on the current ayah; merge each utterance with a **resume-at-cursor** alignment; when cursor reaches `N`, emit a **passing** `ayah.result` and **advance**.

**Do not:** Pass on a mid-ayah suffix with empty prefix credit; do not fail-and-wipe a successful partial chunk that only needs another utterance.
