# Continuous Mode: Ayah Auto-Advance Fix Spec

**Status:** P0 implemented (2026-08-14); P1/P2 deferred  
**Phase:** 2.x (bugfix; protocol compatible)  
**Companion:** `realtime-stream-spec.md` (stream session + coverage), `implementation-spec.md` (assessor / normalizer), `uthmani-tanzeel-word-matching-spec.md` (Heard recovery P0; Simple/Imlaei P1 shared)  
**Version:** 1.0  
**Last updated:** 2026-08-14

---

## 1. Purpose

In **Continuous** mode the UI can show a correctly heard ayah (full “Heard” transcript, every target word highlighted green) while the session **never advances** to the next ayah.

This spec:

1. Reconstructs the failure from a real Al-Fatihah 1:2 session
2. Names the exact code paths that refuse to finalize / advance
3. Separates the **completeness gate** bug from the **accuracy score** (which would actually pass)
4. Specifies a layered fix that does **not** weaken the user’s 85% pass threshold
5. Defines tests that today’s suite cannot catch (Uthmani round-trip mocks)

**Constraint:** Do not mutate stored Quran text. Comparison copies only (`normalizer.py`). STT remains behind `SpeechRecognizer`. REST `/assess` and WS `/stream` keep one assessor.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Range | Surah 1 (Al-Fatihah), start 1, end 7 |
| Accuracy threshold | **85%** |
| On fail | Retry same ayah |
| Current ayah | **1:2** (`ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَٰلَمِينَ`) |
| Mic | on |
| Live % | **75%** |
| Highlights | All four display words green (`word-match`) |
| Heard | `الحمد لله رب العالمين` (simple Arabic; semantically exact) |
| Next ayah | Does **not** appear (stuck on 1:2) |

The user already passed **1:1** (so STT + session + advance *can* work). They stall on 1:2 despite a transcript that a human, and the highlight layer, treat as complete and correct.

**Workaround today (not the fix):** tap **Check now** (`ayah.force_assess`). That path skips the coverage gate; character-level `score` on this transcript is ~98% and **would pass** 85%, then auto-advance. Users should not need a hidden manual confirm after a successful recitation.

---

## 3. Background — how advance is supposed to work

Continuous mode does **not** advance because the transcript “looks right”. It advances only after `ayah.result` with `passed=true` (and `auto_advance=true`).

```text
PCM chunks
    │
    ├─ energy VAD ── silence_short (≥400 ms) ─┐
    │                 silence      (≥800 ms) ─┼─ run_assess() ─► ayah.result
    │                                         │         │
    └─ periodic STT (probe) ── coverage ≥ 0.85┘         │
                                                        ├─ passed + auto_advance → session.advance
                                                        ├─ fail + retry            → session.waiting (same ayah)
                                                        ├─ fail + continue         → session.advance
                                                        └─ fail + stop             → session.summary
```

### 3.1 Three finalize triggers

| Trigger | `reason` | Gate before `ayah.result` | Typical use |
|---------|----------|---------------------------|-------------|
| Completion probe | `coverage` | Probe itself: `progress() ≥ STREAM_COVERAGE_THRESHOLD` (0.85). `run_assess(reason=coverage)` does **not** re-check. | Recite without a long pause |
| Short silence | `silence_short` | `progress() ≥ 0.85` inside `run_assess` | Natural pause after a finished ayah (~400 ms) |
| Long silence | `silence` | `progress() ≥ 0.85` inside `run_assess` | Fallback pause (`STREAM_SILENCE_MS`, **800 ms** in code) |
| Manual | `force` | **None** | UI “Check now” |

If `progress() < 0.85`, silence paths **return `[]`**: no `ayah.result`, no beep, no `session.waiting`, attempt counter unchanged. The UI keeps showing the last `partial.*` highlights. That is exactly the stuck screenshot.

### 3.2 Two different “how much of the ayah?” numbers

| Metric | Function | Definition | UI use | Decides advance? |
|--------|----------|------------|--------|------------------|
| **Coverage / live %** | `MemorizationAssessor.progress()` | Exact `SequenceMatcher` `equal` tokens / `len(expected_tokens)`, best recognized suffix | Progress bar + `· 75%` | **Yes** — all auto-finalize gates |
| **Alignment highlights** | `assess()` → `alignment` | `replace` with `fuzz.ratio ≥ WORD_MATCH_THRESHOLD` (0.75) is **promoted to `op: equal`** | Green / red word chips | No |
| **Score / passed** | `assess().score` | Character `rapidfuzz.fuzz.ratio` on full normalized strings | REST “Score: N%”; stream status after `ayah.result` | **Yes**, but only **after** finalize |

The live `%` is **not** the user’s accuracy slider. `App.vue` binds it to `partial.alignment.progress` (and after a result, to `coverage` then `score`). Users read it as accuracy. In this bug it is exact-token coverage.

### 3.3 Relevant files

| File | Role |
|------|------|
| `backend/app/services/normalizer.py` | Strip tashkeel including **U+0670** (dagger alef); strip tatweel; fold alef forms |
| `backend/app/services/assessor.py` | `progress()`, `_token_match_ratio()`, `assess()` |
| `backend/app/services/stream_session.py` | Probe + silence gates; `will_advance`; `_advance()` |
| `backend/app/config.py` | `STREAM_COVERAGE_THRESHOLD=0.85`, `WORD_MATCH_THRESHOLD=0.75`, `DEFAULT_THRESHOLD=0.85` |
| `frontend/src/App.vue` | `partials: true`, `fail_policy: retry`, live % + highlights |
| `frontend/src/highlight.js` | Maps `alignment` ops 1:1 onto **display** (Uthmani) words |

---

## 4. Root cause

### 4.1 Primary — coverage uses a stricter match than highlights

For 1:2 vs Moonshine-like simple Arabic:

| Side | Tokens after `tokenize()` |
|------|---------------------------|
| Corpus (Uthmani, comparison copy) | `الحمد` `لله` `رب` **`العلمين`** |
| STT (`الحمد لله رب العالمين`) | `الحمد` `لله` `رب` **`العالمين`** |

`ٱلْعَٰلَمِينَ` / `ٱلْعَـٰلَمِينَ` writes the long *ā* as **dagger alef (U+0670)**, sometimes on tatweel. `normalize_arabic()` **deletes** U+0670 (it is in `TASHKEEL`) and deletes tatweel. The seat alef never appears → `العلمين`.

Moonshine (modern Arabic, no Uthmani marks) emits `العالمين` **with** a written alef.

Reproduced (2026-08-14, current assessor, threshold 0.85):

| Metric | Value | Effect |
|--------|-------|--------|
| `progress()` | **0.750** (3/4 exact) | `< 0.85` → probe and silence **never finalize** |
| Word `fuzz.ratio(العلمين, العالمين)` | **0.933** | `≥ 0.75` → alignment `op: equal` → **all four chips green** |
| Overall `score` | **0.976** | `≥ 0.85` → **would pass** if `ayah.result` ever ran |
| `passed` | `True` (if assessed) | Would emit `session.advance` |

So the product **detects** the ayah (UX) and **would accept** it (score), but **refuses to ask** the assessor (coverage gate).

Why 75% specifically: 1:2 has **four** tokens. One dagger-alef spelling mismatch costs 25%. `0.75 < 0.85` by construction. Short ayahs are uniquely toxic; a 20-word ayah with the same mismatch stays at 95% and still auto-advances.

### 4.2 Why 1:1 worked and 1:2 did not

Dagger alef is not uniformly “missing alef in simple script”:

| Ayah | Uthmani phenomenon | After current normalize | Typical STT | `progress()` |
|------|--------------------|-------------------------|-------------|--------------|
| 1:1 `ٱلرَّحْمَٰنِ` | Dagger alef for *ā* in الرحمن | `الرحمن` (no seat alef — **this is** simple spelling) | `الرحمن` | **1.00** → advances |
| 1:2 `ٱلْعَٰلَمِينَ` | Dagger alef **stands in for** a seat alef | `العلمين` | `العالمين` | **0.75** → **stuck** |
| 1:3 | Same as الرحمن | match | match | 1.00 |
| 1:4 `مَٰلِكِ` | Dagger alef → should be `مالك` | `ملك` | `مالك` | **0.667** → stuck |
| 1:5 | Hamza folds to alef | match | match | 1.00 |
| 1:6 `ٱلصِّرَٰطَ` | Dagger alef → `الصراط` | `الصرط` | `الصراط` | **0.667** → stuck |
| 1:7 `صِرَٰطَ` | Same | 8/9 exact = **0.889** | `صراط` | **≥ 0.85** → would pass the gate |

Naively mapping **every** U+0670 → `ا` is **wrong**:

- Fixes 1:2, 1:4, 1:6, 1:7
- Breaks 1:1 / 1:3: `الرحمن` becomes `الرحمان`, coverage drops to **0.75 / 0.50** vs STT `الرحمن`

That is why Tanzil publishes a separate **Simple / Imlaei** encoding instead of a one-line Unicode rewrite.

### 4.3 Secondary — tests never speak STT

`backend/tests/test_stream.py` feeds `MockSpeechRecognizer(transcript=ayah["text"])` — the **Uthmani corpus string**. Expected and recognized are identical, so `progress() == 1.0` always. The Fatihah 1:2 failure cannot appear.

REST `/assess` is **not** stuck: it never uses `progress()`. Same transcript would return `passed=true`. The defect is **stream completion detection**, not the pass/fail formula.

### 4.4 Ruled out (not this screenshot)

| Hypothesis | Why it is not the 1:2 75% stall |
|------------|----------------------------------|
| `fail_policy=retry` blocking advance | Retry only runs **after** `ayah.result` with `passed=false`. Here finalize never happens. Status is still live listening, not “Needs work” / `session.waiting`. |
| Score 75% vs slider 85% | Live `%` is `progress`, not `score`. Score on this pair is **97.6%**. |
| Partials 3 s window truncating long ayahs | Unified periodic path (`partials=true` + probe) STTs the **full** buffer. 1:2 is short. |
| VAD never seeing silence | Probe is supposed to finalize **without** silence once coverage ≥ 0.85. Coverage never gets there. |
| Leftover-carry / pause-less tilawah (v1.1) | Real follow-up (buffer wipe after a *successful* advance). This session never left 1:2. |
| `auto_advance=false` | Frontend sends `auto_advance: true`. |
| Wrong current ayah / 404 Fatihah 1 | Current is 1:2; 1:1 already advanced. |
| Highlight mapper skipping words | Display split and token count are both 4; fuzzy `equal` paints every chip green. |

### 4.5 Aggravating UX

1. Green chips + correct Heard **look like success**; the 75% bar looks “almost there” so users repeat the same ayah.
2. Retry policy is the recommended default, which is correct for real mistakes and **invisible** when no result is emitted.
3. “Check now” / “Skip ayah” exist but are not discoverable as the recovery for a false incomplete.

---

## 5. Goals and non-goals

### 5.1 Goals

**G1.** If STT emits simple Arabic that a reciter (and the highlight layer) would call a complete 1:2, the session must emit `ayah.result` and, at the default 85% threshold, `session.advance` to 1:3 **without** `ayah.force_assess`.

**G2.** Completeness (`progress` / coverage gate) and presentation (`alignment` / green chips) must use the **same definition of “this expected word was heard”**.

**G3.** Pass/fail stays `score >= threshold`. Do **not** lower `STREAM_COVERAGE_THRESHOLD` or `DEFAULT_THRESHOLD` to paper over spelling.

**G4.** Mid-ayah pauses still must **not** finalize (e.g. Heard `الحمد لله` on 1:2 → coverage 0.50 → keep listening).

**G5.** Tests must use **STT-like** transcripts (undiacritized simple Arabic), not Uthmani round-trips.

### 5.2 Non-goals

- Tajweed scoring
- Mutating `quran.json` Uthmani `text`
- Changing REST `/assess` JSON shape
- Leftover transcript carry after a successful advance (call out as P2; do not block this fix)
- Replacing Moonshine or adding a Quran-fine-tuned ASR
- Making dagger-alef restoration a universal `U+0670 → ا` map

---

## 6. Design — layered fix

Ship **P0** to unblock Continuous Fatihah. **P1** makes exact-token coverage honest for the whole mushaf. **P2** is pause-less recitation (already deferred in `realtime-stream-spec.md` §8.2).

### 6.1 P0 — Unify “matched token” (must ship)

**Change `_token_match_ratio()`** so a token counts as matched when:

1. `SequenceMatcher` tag is `equal`, **or**
2. tag is `replace` and `fuzz.ratio(expected_word, recognized_word) / 100 >= word_match_threshold`

Use the same pairing rules as `_assess_direct` (paired replace slice; leftover expected → unmatched; leftover recognized → ignore for coverage).

Keep the existing **best-suffix** loop in `progress()`.

**Do not** change `score` or `passed`.

Consequences for the bug transcript:

| Ayah | Before `progress()` | After P0 `progress()` | Probe at 0.85 |
|------|---------------------|------------------------|---------------|
| 1:2 `العالمين` vs `العلمين` (0.93) | 0.75 | **1.00** | finalize → score 0.98 → advance |
| 1:4 `مالك` vs `ملك` (0.86) | 0.67 | **1.00** | finalize → advance |
| 1:6 `الصراط` vs `الصرط` (0.91) | 0.67 | **1.00** | finalize → advance |
| Mid-ayah `الحمد لله` vs 1:2 | 0.50 | 0.50 | still blocked (G4) |

Partial events already call both `progress()` and `assess()`. After P0 the bar and the chips agree.

### 6.2 P0 algorithm (normative)

```text
function token_match_ratio(expected_words, recognized_words, word_thr):
    matcher = SequenceMatcher(expected_words, recognized_words, autojunk=False)
    matched = 0
    for tag, i1, i2, j1, j2 in matcher.opcodes:
        if tag == "equal":
            matched += (i2 - i1)
        elif tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for k in 0 .. paired-1:
                if fuzz.ratio(expected_words[i1+k], recognized_words[j1+k]) / 100 >= word_thr:
                    matched += 1
            # unpaired expected words in the replace slice stay unmatched
    return matched / len(expected_words)

function progress(expected, recognized):
    ew, rw = tokenize(expected), tokenize(recognized)
    return max(token_match_ratio(ew, rw[s:], word_match_threshold) for s in 0 .. len(rw)-1)
```

Implementation note: `_token_match_ratio` is a `@staticmethod` today and cannot see `word_match_threshold`. Make it an instance method (or pass the threshold). Session already constructs `MemorizationAssessor(threshold=config.threshold)` which inherits default `WORD_MATCH_THRESHOLD`.

DRY option (acceptable): derive coverage from `_assess_direct(...).alignment` as  
`count(op==equal) / len(tokenize(expected))` on the best-suffix candidate **chosen by that coverage**, not by character `score`. Do not reuse `assess()`’s “best by score” suffix pick for the gate (score-best and coverage-best can differ once extra trailing words exist).

### 6.3 P1 — Comparison orthography (should ship soon)

P0 treats `العلمين` ≈ `العالمين` as a **fuzzy** match. That is the right completeness signal; it is still a **normalization hole**. Character `score` stays high here, but other ayahs with several dagger-alef seats will leak accuracy.

**Do this:**

1. Keep displaying Uthmani `ayah.text` (corpus unchanged).
2. For `tokenize` / `normalize_arabic` **inputs from the corpus only**, compare against a **Simple / Imlaei** copy (Tanzil simple or equivalent trusted source), **or** store `text_simple` beside `text` in `quran.json`.
3. STT strings stay as emitted; they are already simple-ish.

Trusted Simple 1:2 is `الحمد لله رب العالمين` → exact tokens, `progress()==1`, `score==1`, no dependence on 0.75 word fuzz.

P0 of `uthmani-tanzeel-word-matching-spec.md` (ayah-constrained Heard recovery) shipped 2026-08-14; this Simple/Imlaei comparison copy remains the honest orthography fix.

**Do not** implement P1 as `U+0670 → ا` globally (breaks الرحمن). If a mechanical map is used at all, it must be validated per-token against Simple gold, not assumed.

Provenance: same bar as Uthmani — verify against Tanzil / nuqayah (or documented equivalent) before any public release. Never write normalized/simple text back over `text`.

### 6.4 P2 — Leftover carry (out of scope, do not confuse with P0)

After a **successful** coverage finalize, `buffer.clear(keep_overlap_ms=300)` drops the rest of a pause-less utterance. Ayah N+1 that was already spoken is lost. That is `realtime-stream-spec.md` §8.2 v1.1. Fixing P0 without P2 means: **paused** Fatihah auto-advances; **unpaused** Fatihah may still skip N+1 until the user repeats it. Document in release notes; do not hold P0.

### 6.5 Explicitly rejected “fixes”

| Idea | Why reject |
|------|------------|
| Lower `STREAM_COVERAGE_THRESHOLD` to 0.70 | Unblocks 1:2 (0.75) but **not** 1:4/1:6 (0.67). At 0.65, a 3-word ayah finalizes after **2** real words. |
| Lower user default threshold | Score is already 0.98; the slider is not the gate. |
| Frontend: `force_assess` when the bar is full | Bar never reaches 100% today. Even if P0 lands, completeness belongs on the server (Flutter-ready). |
| Frontend: advance when all chips are green | Chips can go green at 75% coverage **today**; that would skip the assessor. |
| Count inserts toward coverage | Completeness is “expected tokens heard”, not “lots of speech”. |

---

## 7. Fuzzy-match risk (P0)

`WORD_MATCH_THRESHOLD` stays **0.75**. Coverage then inherits the same near-misses alignment already shows as green.

| Pair | `fuzz.ratio` | Counts as match at 0.75? | Wanted? |
|------|--------------|---------------------------|---------|
| `العلمين` / `العالمين` | 93.3 | yes | yes (this bug) |
| `ملك` / `مالك` | 85.7 | yes | yes |
| `الصرط` / `الصراط` | 90.9 | yes | yes |
| `رحيم` / `عليم` | 50.0 | no | no |
| `غفور` / `شكور` | 50.0 | no | no |
| `الحمد` / `احمد` | 88.9 | yes | unfortunate near-miss |
| `لله` / `الله` | 85.7 | yes | unfortunate near-miss |

Coverage is a **completeness** gate. A false high coverage only **starts** `assess()`; `passed` still uses character `score` plus retry/continue/stop. Worst case with `retry`: extra assess + warning beep, still same ayah. Do **not** raise `WORD_MATCH_THRESHOLD` in P0 (would re-stick `ملك`/`مالك` at 85.7% if the threshold moved above that). Optional later: a dedicated `COVERAGE_WORD_THRESHOLD` (e.g. 0.90) so only dagger-alef-class near-matches count; not required to close this bug.

---

## 8. Stream session behavior after P0

No protocol change. Same events.

| Path | Before | After P0 on correct 1:2 STT |
|------|--------|------------------------------|
| Periodic probe | `progress=0.75` → no `_assess_trigger` | `progress=1.0` → `run_assess(reason=coverage, recognized_hint=…)` |
| Silence 400 ms / 800 ms | `run_assess` returns `[]` | emits `ayah.result` + `session.advance` |
| `fail_policy=retry` + genuine miss | unchanged | unchanged |
| `ayah.force_assess` | already worked | still works |

Keep `STREAM_COVERAGE_THRESHOLD=0.85`. Keep `STREAM_SILENCE_MS=800` (comments/docs that say 2500 ms are stale; unrelated).

Optional debug (cheap, useful): add `progress` next to `coverage` on `ayah.result` always, and log `progress` / `score` / `passed` at INFO on finalize. Helps confirm gate vs slider in traces (`?lab=1` WS dump).

---

## 9. Frontend (minimal)

P0 does not require a Vue change to restore advance. Optional UX, same PR or follow-up:

1. Label the live number **Coverage** (or “Heard”) so it is not confused with the accuracy slider.
2. After `ayah.result` with `passed=false`, keep showing **score** in status (already does) and do not overwrite the bar with a stale partial 75% without a caption.
3. Do **not** auto-send `ayah.force_assess` from the client as the primary fix.

`wordsFromAlignment` stays as-is for P0 (it already paints fuzzy `equal` green). P1 may later align display tokens vs comparison tokens if `text_simple` word counts ever diverge; Fatihah 1:2 does not diverge.

---

## 10. Test plan (normative)

Add tests that **fail on current main** and **pass after P0**.

### 10.1 Assessor unit (`backend/tests/test_core.py`)

Use **simple Arabic** as `recognized`, Uthmani (fixture) as `expected`.

| ID | Expected (fixture Uthmani) | Recognized (STT-like) | Assert |
|----|----------------------------|------------------------|--------|
| A1 | 1:2 `ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَٰلَمِينَ` | `الحمد لله رب العالمين` | `progress() == 1.0` (±eps) |
| A2 | same | same | `assess().passed` at 0.85; `score >= 0.85`; no `wrong_words` |
| A3 | same | `الحمد لله` | `progress() == 0.5`; must **not** be ≥ 0.85 |
| A4 | 1:4 `مَٰلِكِ يَوْمِ ٱلدِّينِ` | `مالك يوم الدين` | `progress() == 1.0` |
| A5 | 1:6 `ٱهْدِنَا ٱلصِّرَٰطَ ٱلْمُسْتَقِيمَ` | `اهدنا الصراط المستقيم` | `progress() == 1.0` |
| A6 | 1:1 Bismillah | `بسم الله الرحمن الرحيم` | `progress() == 1.0` (must not regress الرحمن) |
| A7 | 1:2 | `الحمد لله الحمد لله رب العالمين` | `progress() == 1.0` (existing suffix behavior) |
| A8 | 1:2 | `الحمد لله رب العليين` | `progress() < 0.85` **or** document if `العليين`/`العلمين` (85.7%) counts; prefer **not** treating this as complete if a stricter coverage word threshold is added |

Keep existing Uthmani-round-trip tests; they still prove diacritic stripping.

### 10.2 Stream session (`backend/tests/test_stream.py`)

| ID | Setup | Assert |
|----|--------|--------|
| S1 | Mock STT = `الحمد لله رب العالمين`, session on **1:2**, threshold 0.85, `fail_policy=retry`, `auto_advance=true` | `run_periodic_stt()` emits `_assess_trigger`; `run_assess(reason=coverage, recognized_hint=…)` emits `ayah.result` `passed=true` and `session.advance` to 1:3 |
| S2 | Same transcript, `run_assess(reason=silence)` / `silence_short` | Does **not** return `[]`; advances |
| S3 | Mock STT = `الحمد لله`, 1:2 | Probe does **not** trigger; silence `run_assess` returns `[]`; `current_ayah` stays 2 |
| S4 | Scripted Fatihah 1→7 with STT-like lines (appendix §14), `fail_policy=retry`, threshold 0.85 | Auto-advance through 1:7 without `force`; summary `ayahs_passed == 7` (or 6 if range end handling differs — match `end_ayah`) |

Do **not** consider S1 satisfied by `MockSpeechRecognizer(transcript=uthmani_1_2)`.

### 10.3 Manual / lab

1. Continuous, Fatihah 1–7, threshold 85%, retry, mic on.
2. Recite 1:1 pause 1:2 pause … (natural pauses).
3. Expect live coverage → 100% near the end of each ayah, then advance within ~1 s (probe interval) **without** Check now.
4. `?lab=1` download WS trace: for 1:2, `partial.alignment.progress >= 0.85` then `ayah.result` `trigger=coverage` or `silence_short`, then `session.advance` `to.ayah=3`.

---

## 11. Acceptance

This spec is **implemented** when:

1. A1–A7 and S1–S3 pass in CI
2. Manual §10.3: stuck-at-1:2 screenshot cannot be reproduced with a correct recitation
3. Mid-ayah pause on 1:2 after two words does **not** jump to 1:3
4. 1:1 Bismillah still auto-advances (الرحمن not broken)
5. REST `/assess` behavior unchanged for the same 1:2 audio (still passes)
6. No `U+0670 → ا` global map in `normalizer.py` unless P1 gold tests prove الرحمن still exact-matches `الرحمن`

P1 and P2 are **not** required to close the reported bug; track them as follow-ups in `docs/agent-context.md` when implemented.

---

## 12. Implementation sketch (for the implementing agent)

Order:

1. `assessor.py`: `_token_match_ratio` as in §6.2; unit tests §10.1 first (red), then code (green)
2. Confirm `progress()` still suffix-best; no change to `fuzz.ratio` overall score
3. Stream tests §10.2 with STT-like strings on fixture 1:2
4. Optional: `ayah.result` always includes `coverage`
5. Do not retune `STREAM_*` thresholds in this PR
6. P1 simple text = separate PR (corpus + download + “never overwrite `text`”)

---

## 13. References

| Document / code | Relevance |
|-----------------|-----------|
| This screenshot / session | 1:2, 75%, all-green, Heard exact, retry, 85% |
| `backend/app/services/assessor.py` | `progress` exact-only vs alignment fuzzy-equal |
| `backend/app/services/normalizer.py` | `TASHKEEL` includes U+0670 |
| `backend/app/services/stream_session.py` | `STREAM_COVERAGE_THRESHOLD` gates |
| `backend/app/config.py` | 0.85 coverage / 0.75 word / 0.85 score |
| `frontend/src/App.vue` | live % = `progress`; `partials: true`; retry default |
| `specs/realtime-stream-spec.md` §8–9 | Strategy B coverage; advance rules |
| `specs/implementation-spec.md` §5, §10 | Normalize comparison copies; score = fuzz.ratio |
| `specs/partials-evaluation-spec.md` | Probe vs partials; do not treat partial % as a second gate with a different formula |
| Tanzil Uthmani vs Simple | Why dagger alef cannot be blindly restored |

---

## 14. Appendix — STT-like Fatihah lines (test gold)

These are **comparison transcripts**, not a new corpus. Display remains Uthmani.

```text
1:1  بسم الله الرحمن الرحيم
1:2  الحمد لله رب العالمين
1:3  الرحمن الرحيم
1:4  مالك يوم الدين
1:5  اياك نعبد واياك نستعين
1:6  اهدنا الصراط المستقيم
1:7  صراط الذين انعمت عليهم غير المغضوب عليهم ولا الضالين
```

Moonshine may fold hamza/`إ`/`أ` slightly differently (e.g. `إياك` vs `اياك`); P0 fuzzy match is why A5/S4 should still pass. If a future model emits `الرحمان`, P1 Simple vs STT needs a recorded decision (prefer keeping `الرحمن` as gold; treat `الرحمان` as fuzzy coverage only).

---

## 15. Open questions

1. P1 storage: extra `text_simple` field on each ayah vs a second file vs Tanzil download at entrypoint? (`text_simple` beside `text` is the least surprising for Flutter clients if we ever expose it; **do not** send it to the UI in P0.)
2. Should `COVERAGE_WORD_THRESHOLD` be split from `WORD_MATCH_THRESHOLD` (0.90 vs 0.75) to reduce `الحمد`/`احمد` false completes?
3. After P0, should the live bar show coverage (completeness) and a second number for running `score`? (UX only.)
4. When to schedule leftover-carry (P2) relative to P1?
