# Uthmani ↔ Tanzeel / STT Word Matching Fix Spec

**Status:** P0 implemented (2026-08-14); P1 Simple orthography / Heard projection and P2 short-ayah pass integrity deferred  
**Phase:** 2.x (bugfix + orthography / lexicon bridge)  
**Companion:**  
- `ayah-advance-fix-spec.md` (coverage vs dagger-alef; P1 Simple/Imlaei)  
- `stt-confidence-filter-spec.md` (Heard word-keep floor)  
- `realtime-stream-spec.md` (partials / coverage gate)  
- `implementation-spec.md` §5–6 (normalizer + assessor; optional lexicon correction)  
**Version:** 1.0  
**Last updated:** 2026-08-14  
**Working reference commit:** `b897a11ebfd763` (“Continious recitation”)

---

## 1. Purpose

In **Continuous** mode on Al-Fatihah **1:1** (Basmala), the first display word `بِسْمِ` stays unmatched (dashed / `word-missing`) while `ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ` go green. Live coverage sits at **75%**, below the **85%** Accuracy / coverage gate, so the session never finalizes or advances.

Users experience this as “Uthmani chips won’t match Tanzeel / normal Arabic Heard,” especially for **`بسم`**.

This spec:

1. Reconstructs the failure from the 2026-08-14 screenshot  
2. Separates **true orthography mismatches** from **Heard omission** (the screenshot’s actual mechanism)  
3. Documents how `b897a11` matched Heard against the Quran corpus (and why that path still matters)  
4. Specifies a layered fix: restore complete Heard for ayah vocabulary, bridge Uthmani↔simple for comparison, optionally project matched words onto corpus surfaces for display  
5. Defines tests that catch Basmala / agglutination / confidence regressions

**Constraint:** Never mutate stored Uthmani `ayah.text`. Normalize / Simple copies only. STT stays behind `SpeechRecognizer`. One assessor for REST and WS.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Range | Surah 1 (Al-Fatihah), start 1, end 7 |
| Accuracy threshold | **85%** |
| On fail | Retry same ayah (recommended) |
| Current ayah | **1:1** `بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ` |
| Mic | on |
| Live % | **75%** (coverage / `progress`, not the slider) |
| Highlights | `بِسْمِ` = **missing** (dashed); other three = **match** (green) |
| Heard | `اللهِ الرَّحْمٰنِ الرَّحِيْمِ` (no `بسم` / `بِسْمِ`) |
| User framing | Uthmani target words fail to match Tanzeel / normal Arabic Heard |

### 2.1 What the UI is actually showing

| Layer | Source | Script |
|-------|--------|--------|
| Target chips | `currentAyah.text` whitespace split | **Uthmani** (alef wasla `ٱ`, dagger alef `ٰ`, full tashkeel) |
| Chip status | `partial.alignment` via `wordsFromAlignment` | Index-mapped onto Uthmani display words |
| Heard line | `partial.transcript.recognized` (confidence-filtered) | **STT / Tanzeel-like** (plain alef, mixed diacritics) |
| Live % | `assessor.progress(expected_uthmani, heard)` | Token coverage after `normalize_arabic` |

`بِسْمِ` uses CSS class `word-missing` (dashed outline) — alignment op `delete`, not “pending” and not a failed `replace`.

### 2.2 Reproduced alignment (current assessor)

Expected after `tokenize(Uthmani 1:1)`:

```text
بسم | الله | الرحمن | الرحيم
```

Heard after `tokenize(screenshot Heard)`:

```text
الله | الرحمن | الرحيم
```

`SequenceMatcher` opcodes:

| Op | Expected | Recognized |
|----|----------|------------|
| `delete` | `بسم` | — |
| `equal` | `الله` | `الله` |
| `equal` | `الرحمن` | `الرحمن` |
| `equal` | `الرحيم` | `الرحيم` |

| Metric | Value | Effect |
|--------|-------|--------|
| `progress()` | **0.75** (3/4) | `< 0.85` → probe / silence **do not finalize** |
| Chip `بِسْمِ` | missing | Matches screenshot |
| Overall `score` | **~0.90** | `≥ 0.85` → **would `passed=true`** if `ayah.result` ever ran |
| Orthography of `بسم` itself | N/A | Word is **absent**, not misspelled |

So the screenshot is **not** “`بِسْمِ` failed to normalize against `بسم`.” It is “`بسم` never reached the assessor / Heard line.”

---

## 3. Background — how matching worked in `b897a11`

Commit `b897a11ebfd763` introduced Continuous mode. Relevant behavior:

```text
PCM → SpeechRecognizer.transcribe_audio → recognized (raw string)
                                            │
expected = quran.get_ayah(... )["text"]  ───┤  (Uthmani corpus)
                                            ▼
                              MemorizationAssessor.assess(expected, recognized)
                                            │
                              normalize_arabic on both sides (comparison copies)
                              SequenceMatcher on tokenize() words
```

### 3.1 What that commit did right

1. **Heard was always scored against the Quran corpus ayah text** — not against a second orthography file, and not against UI display tricks.  
2. **`normalize_arabic` already folds** the Basmala orthography gap that users notice visually:
   - Uthmani `بِسْمِ` → `بسم`
   - Tanzeel / STT `بسم` / `بِسْمِ` → `بسم`
   - Alef wasla `ٱ` → `ا`
   - Tashkeel / Quranic marks stripped  
3. **No STT confidence filter** — every decoded whitespace word reached `assess` / partial Heard.  
4. **No live word chips** — failures were less visible mid-utterance; finals used the same assessor.

Verified today (same normalizer as `b897a11`):

| Recognized | `progress()` | Chip `بسم` |
|------------|--------------|------------|
| `بسم الله الرحمن الرحيم` | **1.0** | match |
| `بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ` (Tanzeel + tashkeel) | **1.0** | match |
| `باسم الله الرحمن الرحيم` | **1.0** (fuzzy ≥ 0.75) | match |
| Screenshot Heard (no `بسم`) | **0.75** | missing |

**Conclusion:** Uthmani↔Tanzeel matching for **`بسم` when present** still works exactly as in `b897a11`. The regression is elsewhere in the Phase 2.x stack (Heard completeness + visibility), not in `normalize_arabic("بسم")`.

### 3.2 What changed after `b897a11` (relevant deltas)

| Change | Effect on this bug |
|--------|--------------------|
| Live word chips + `partial.alignment` | Missing first word is **visible** as dashed `بِسْمِ` |
| `progress()` coverage gate @ 0.85 | **Blocks** finalize at 75% even though character `score≈0.90` would pass |
| STT confidence filter (`stt-confidence-filter-spec.md`) | Can **drop** a low-confidence first token from `Transcription.text` / Heard |
| SentencePiece `▁` / agglutination bugs (session history) | Can merge `بسم`+`الله` → `بسمالله` (then fuzzy 0.60 → **replace**, not equal) |
| Ayah-advance P0 fuzzy coverage | Fixes dagger-alef seats (`العلمين`/`العالمين`); **does not** invent a missing `بسم` |

`b897a11` is the reference for: **expected = corpus Uthmani; recognized = STT string; match only via normalizer + assessor.** This fix must preserve that contract while restoring complete Heard for in-vocabulary words.

---

## 4. Root-cause analysis

### 4.1 Primary (screenshot) — Heard omission of `بسم`

`بسم` is missing from the filtered Heard string. Causes to treat as first-class:

| Hypothesis | Evidence | Severity |
|------------|----------|----------|
| **H1** Confidence filter dropped `بسم` (`kept=false` while later words kept) | Filter uses Accuracy *T* (0.85) as keep floor; first tokens often weaker | High |
| **H2** Moonshine never emitted `بسم` (late start / clipped buffer / noise on onset) | Energy VAD + partial window; onset loss is real | High |
| **H3** Agglutination `بسمالله` then trim / align leaves no standalone `بسم` | Prior SP spacing bug; `tokenize("بسمالله…")` → progress **0.5**, replace @ 0.60 | Medium |
| **H4** `trim_overgenerated_partial` removed `بسم` | Only runs when `rec_n` ≫ expected; screenshot has **fewer** tokens — **ruled out** for this shot | — |
| **H5** `normalize_arabic` cannot map Uthmani `بِسْمِ` ↔ STT `بسم` | **Ruled out** — exact match when both present | — |

**P0 must address H1 and H3.** H2 is ASR quality; mitigate with ayah-constrained recovery (lexicon), not by lowering global thresholds.

### 4.2 Secondary — Uthmani vs Simple orthography (whole mushaf)

Already documented in `ayah-advance-fix-spec.md` §4:

| Phenomenon | After current normalize | Typical STT | Example |
|------------|-------------------------|-------------|---------|
| Dagger alef stands in for seat alef | Missing `ا` | Written `ا` | `العلمين` vs `العالمين` |
| Dagger alef is *ā* without seat | Correct simple spelling | Match | `الرحمن` |
| Blind `U+0670 → ا` | Breaks الرحمن → الرحمان | — | **Forbidden** |

P0 fuzzy coverage mitigated finalize stalls. **P1 Simple/Imlaei comparison copy** remains the honest orthography fix and is **in scope** for this matching workstream (shared with ayah-advance P1).

### 4.3 Tertiary — short-ayah character score masks a deleted word

For screenshot Heard vs 1:1: `score≈0.90` and `passed=True` even with `بسم` deleted. Coverage at 0.75 currently **saves** the session from auto-advancing on an incomplete Basmala — until the user hits **Check now** (`force_assess`), which skips the coverage gate.

This spec’s P0 restores `بسم` when it was spoken. **P2** (optional) tightens pass rules for short ayahs so a full-token `delete` cannot pass on character ratio alone.

### 4.4 Ruled out as the screenshot mechanism

| Hypothesis | Why not |
|------------|---------|
| Highlight mapper index bug (Uthmani vs normalized word count) | Both sides have **4** display / token slots; first op is `delete` |
| Frontend comparing raw Uthmani chips to Heard glyphs | Chips only consume alignment ops; they never string-compare scripts |
| Fail policy `retry` blocking advance | No `ayah.result` yet — still listening |
| Need to mutate corpus to Tanzeel | Violates non-negotiable; `b897a11` never did this |

---

## 5. Goals and non-goals

### 5.1 Goals

**G1.** When the reciter says the Basmala and STT emits (or nearly emits) `بسم الله الرحمن الرحيم`, chips / coverage / Heard must treat **`بسم` as matched** and Continuous mode must be able to finalize at default 85% **without** `ayah.force_assess`.

**G2.** Preserve the `b897a11` contract: **expected = Quran corpus text**; **recognized = STT-derived string**; comparison only through normalizer / Simple copies + assessor.

**G3.** Confidence filtering must not systematically strip **in-vocabulary** ayah-prefix words that fuzzy-match the current expected ayah (especially token 0 of short ayahs).

**G4.** Agglutinated STT forms that are concatenations of consecutive expected tokens (e.g. `بسمالله`) must be recoverable into the expected token sequence before assess / progress.

**G5.** Display remains Uthmani for target chips. Optional: Heard may show **corpus surfaces for matched words** (lexicon projection) while extras stay as STT — never write that back to `quran.json`.

**G6.** Do not regress `ayah-advance-fix-spec.md` P0 (Fatihah 1:2 / 1:4 / 1:6 coverage) or `stt-confidence-filter-spec.md` hallucination dumps (`يا كلب…`).

### 5.2 Non-goals

- Mutating `quran.json` Uthmani `text`  
- Replacing Moonshine with Quran-fine-tuned ASR (Phase 3 full scope)  
- Blind global `U+0670 → ا`  
- Lowering `DEFAULT_THRESHOLD` / `STREAM_COVERAGE_THRESHOLD` to hide missing words  
- Client-side Arabic matching logic (Flutter-ready: server owns it)

---

## 6. Design — layered fix

```text
                    ┌─ H1/H3 recovery ─────────────────────────┐
PCM → Moonshine → raw decode → confidence filter → lexicon bridge
                                                         │
                         expected Uthmani (display) ──────┤
                         expected Simple (compare) ───────┤  P1
                                                         ▼
                              MemorizationAssessor (unchanged API)
                                                         │
                              partial.transcript / alignment / progress
```

### 6.1 P0 — Ayah-constrained Heard recovery (must ship)

Add a pure function used by stream partials **and** REST/stream finalize **after** `filter_transcription`, **before** `assess` / `progress`:

```text
function recover_against_ayah(expected_uthmani: str, transcription: Transcription) -> Transcription
```

**Normative behavior:**

1. Let `E = tokenize(expected_uthmani)` (and, once P1 exists, prefer `tokenize(expected_simple)`).  
2. Let `R` = kept words from `transcription` (surface strings), in order.  
3. **Agglutination split (H3):**  
   For each kept word `w`, if `tokenize(w) == [t]` and `t` is not a fuzzy match to any single `E[i]`, try to segment `t` as a concatenation of **two or more consecutive** `E[i..j]` tokens (greedy left-to-right, require each piece exact on normalized forms).  
   Example: `بسمالله` + `E=["بسم","الله",…]` → `["بسم","الله"]`.  
4. **In-vocab soft-keep (H1):**  
   Among `transcription.words` with `kept=false`, if `tokenize(word.text)` fuzzy-matches some unmatched `E[i]` at `≥ WORD_MATCH_THRESHOLD` **and** the match is consistent with sequence order (cannot skip backwards past an already-matched expected index), **revive** that word into the filtered text.  
   Cap: only revive words whose raw (pre-calibration optional) confidence is ≥ `STT_INVOCAB_FLOOR` (new setting, default **0.55**), so pure garbage still dies.  
5. Rebuild `Transcription.text` from the recovered kept sequence (single spaces).  
6. Do **not** invent tokens that never appeared in raw decode **except** via agglutination split of an emitted surface.

**Why this matches `b897a11` spirit:** Heard remains STT-derived; the corpus only **constrains recovery**, it does not replace ASR with a forced lexicon dump of the whole ayah.

**Config:**

| Setting | Default | Role |
|---------|---------|------|
| `STT_AYAH_LEXICON_RECOVERY` | `true` | Master switch |
| `STT_INVOCAB_FLOOR` | `0.55` | Min decoder conf to revive a dropped in-vocab word |
| Existing Accuracy *T* | `0.85` | Still the default keep floor for out-of-vocab / weak tokens |

**Hallucination safety (G6):** Revivals only for fuzzy matches to **current ayah** `E`. `يا كلب…` still fails in-vocab match → stays dropped.

### 6.2 P0 — Wire recovery into session + REST

| Path | Change |
|------|--------|
| `stream_session._partial_events_from_recognized` | Run recovery on `transcription` (or on `recognized` + expected) before display / `progress` / `assess` |
| `stream_session.run_assess` | Same recovered string for score / coverage / `ayah.result.recognized` |
| `api/memorization.py` | Same after `transcribe_detailed` |
| `SpeechRecognizer` | Prefer implementing recovery in one place (`stt_confidence.py` or new `lexicon_recovery.py`) called from recognizer **or** session — **one** call site for REST+WS |

Emit optional debug fields when `?lab=1` / lab trace:

```json
{
  "recognized": "بسم الله الرحمن الرحيم",
  "raw_recognized": "الله الرحمن الرحيم",
  "recovery": { "revived": ["بسم"], "split": [] }
}
```

Do not show `raw_recognized` in the default Heard line (`heardTextFromMessage` stays on filtered/recovered text).

### 6.3 P1 — Simple / Imlaei comparison orthography (should ship with or right after P0)

Same as `ayah-advance-fix-spec.md` §6.3:

1. Keep displaying Uthmani `text`.  
2. Store trusted `text_simple` (Tanzil Simple / Imlaei or equivalent) beside `text`, **or** resolve Simple at download/repair time.  
3. Assessor / `progress` / recovery tokenization for **corpus side** uses Simple; STT side unchanged.  
4. Provenance: verify before public release; never overwrite Uthmani `text`.

This makes Uthmani↔Tanzeel matching exact for dagger-alef seats and reduces dependence on fuzzy 0.75 for completeness.

### 6.4 P1 — Optional Heard projection onto corpus surfaces

When a recovered/aligned recognized token matches expected index `i`:

- **Assessment** continues to use normalized/Simple tokens.  
- **Heard display** (stream `partial.transcript.recognized` only) may join the **Uthmani display words** for matched indices and STT surfaces for inserts.

Example after full Basmala match:

```text
Heard: بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ
```

This is the product reading of “implementing the heard words with the quran corpus” without lying about extras. Gate behind `STT_HEARD_PROJECT_TO_CORPUS` (default `false` until UX sign-off).

### 6.5 P2 — Short-ayah pass integrity (optional, separate PR)

If alignment contains any `delete` of a full expected token and `len(E) ≤ 6`, require `passed = score >= threshold **and** progress >= threshold` (or require zero deletes). Prevents `force_assess` from advancing 1:1 at `score=0.90` with `بسم` missing.

Do **not** block P0 on P2.

### 6.6 Explicitly rejected “fixes”

| Idea | Why reject |
|------|------------|
| Lower coverage / accuracy to 0.70 | Advances incomplete Basmalas; hides H1 |
| Compare UI chips to Heard in Vue | Breaks Flutter-ready contract; duplicates Arabic logic |
| Rewrite corpus to Tanzeel | Violates non-negotiable; breaks Uthmani display |
| Blind `U+0670 → ا` | Breaks `الرحمن` (ayah-advance §4.2) |
| Disable confidence filter globally | Restores `يا كلب` Heard dumps |
| Force-insert entire ayah text into Heard when coverage > 0.5 | Fakes recitation |

---

## 7. Algorithms (normative sketches)

### 7.1 Agglutination split

```text
function try_split(token_norm, E, start_i):
    # Returns list of E slices covering token_norm, or null
    if start_i >= len(E): return null
    for end in start_i+1 .. len(E):
        if "".join(E[start_i:end]) == token_norm:
            return E[start_i:end]
        if not token_norm.startswith("".join(E[start_i:end])):
            break
    return null
```

Only split when `end - start_i >= 2`. Prefer the **shortest** non-empty cover that consumes the whole `token_norm` (avoids over-splitting).

### 7.2 Ordered in-vocab revive

```text
function revive(dropped_words, E, already_kept_norm_seq, floor):
    next_i = longest_prefix_coverage(E, already_kept_norm_seq)
    revived = []
    for w in dropped_words in decode order:
        if w.confidence < floor: continue
        n = tokenize(w.text)
        if len(n) != 1: continue
        # search forward from next_i for fuzzy match
        for j in next_i .. len(E)-1:
            if fuzz.ratio(n[0], E[j]) / 100 >= WORD_MATCH_THRESHOLD:
                revived.append(w)
                next_i = j + 1
                break
    return revived
```

Insert revived words into the kept sequence in **decode order** (stable), then re-run agglutination if needed.

### 7.3 Assessor contract (unchanged API)

```text
assess(expected_uthmani_or_simple_for_compare, recognized_recovered)
progress(same)
```

Alignment `expected` / `recognized` fields remain **normalized comparison tokens** (as today). Display chips keep mapping by index onto Uthmani whitespace words (`highlight.js`).

---

## 8. Protocol / UI

### 8.1 Events

No breaking change to event types. `partial.transcript.recognized` and `ayah.result.recognized` become **recovered** text when the flag is on.

Optional additive fields (P1 lab / debug):

| Field | Where | Meaning |
|-------|-------|---------|
| `raw_recognized` | partial / result | Pre-recovery filtered text |
| `recovery.revived` | lab | Surfaces revived |
| `recovery.split` | lab | Agglutination splits performed |
| `stt_words[].kept` | existing | May flip `false→true` after revive |

### 8.2 Frontend

P0: **no Vue logic change required** if server Heard is complete — chips go green when alignment includes `equal` for `بسم`.

Optional: label live % as “Coverage” (already suggested in ayah-advance spec).

---

## 9. Tests

### 9.1 Unit — recovery

| ID | Input | Expected |
|----|-------|----------|
| L1 | expected 1:1 Uthmani; kept=`الله الرحمن الرحيم`; dropped=`بسم` conf=0.62 | text includes leading `بسم`; progress **1.0** |
| L2 | dropped=`بسم` conf=0.40 (`< 0.55`) | **not** revived; progress 0.75 |
| L3 | kept=`بسمالله الرحمن الرحيم` | split → progress **1.0**; alignment equal×4 |
| L4 | kept=`يا كلب دعياة…` on 1:5 | no revive; still empty / garbage-free per confidence spec |
| L5 | Tanzeel+tashkeel full Basmala, no drops | progress **1.0** (normalize parity with `b897a11`) |
| L6 | `باسم الله…` | fuzzy equal on first token; progress **1.0** |

### 9.2 Stream / REST

| ID | Scenario | Assert |
|----|----------|--------|
| S1 | Mock detailed transcript: words with `بسم` kept=false@0.62 + rest kept@0.9; session 1:1; threshold 0.85 | `partial.transcript.recognized` starts with `بسم`; `progress≥0.85`; coverage finalize advances |
| S2 | Same as screenshot (no `بسم` in raw decode at all) | No invention; progress 0.75; **no** false advance |
| S3 | Agglutinated mock `بسمالله الرحمن الرحيم` | advances 1:1→1:2 without force |
| S4 | Regression: Fatihah STT-like lines from ayah-advance appendix | S4 suite still green |
| S5 | Regression: confidence hallucination fixture | `كلب` never in Heard |

### 9.3 Gold strings

```text
# Corpus display (Uthmani) — do not mutate
1:1  بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ

# STT / Tanzeel-like (comparison / mocks)
1:1  بسم الله الرحمن الرحيم

# Screenshot failure gold (incomplete Heard)
1:1  الله الرحمن الرحيم          → progress 0.75, delete بسم

# Agglutination gold
1:1  بسمالله الرحمن الرحيم       → after P0 split → progress 1.0
```

Do **not** consider L1/S1 satisfied by `MockSpeechRecognizer(transcript=uthmani_1_1)` round-trip.

---

## 10. Implementation plan

1. **Reproduce in tests** — L1/L3/S1 against screenshot + agglutination. **Done.**  
2. **Implement** `recover_against_ayah` + config flags in `stt_confidence.py`. **Done.**  
3. **Wire** one implementation (`apply_ayah_recovery`) used by stream partials, `run_assess`, and REST assess. **Done.**  
4. **Green** L*/S* + existing confidence + ayah-advance suites. **Done.**  
5. **Docs** — `docs/agent-context.md` incident row; cross-link from ayah-advance P1 and implementation-spec lexicon bullet. **Done.**  
6. **P1 follow-up** — `text_simple` download/repair + assessor corpus-side tokenize; optional Heard projection flag.

---

## 11. Acceptance criteria

- [x] Screenshot scenario with **revivable** dropped `بسم` (conf ≥ 0.55) → green `بِسْمِ`, coverage ≥ 85%, auto-advance without force  
- [x] Screenshot scenario with **no** `بسم` in raw decode → still 75%, no fake Heard word  
- [x] `بسمالله…` agglutination recovers four equals  
- [x] Full simple / Tanzeel Basmala still matches Uthmani at progress 1.0 (parity with `b897a11`)  
- [x] Hallucination Heard dump still suppressed  
- [x] Fatihah 1:2 / 1:4 / 1:6 advance regressions still pass  
- [x] Corpus Uthmani bytes unchanged on disk  

---

## 12. References

| Document / code | Relevance |
|-----------------|-----------|
| Screenshot 2026-08-14 20:15 | 1:1, 75%, dashed `بِسْمِ`, Heard without `بسم` |
| `b897a11ebfd763` | Working reference: assess(corpus, STT) via normalizer |
| `backend/app/services/normalizer.py` | Uthmani→comparison folds; `بسم` already OK when present |
| `backend/app/services/assessor.py` | Alignment + `progress` |
| `backend/app/services/stt_confidence.py` | Keep floor; agglutination / revive lives next to filter |
| `backend/app/services/stream_session.py` | Partials + coverage gate |
| `frontend/src/highlight.js` | Index map alignment → Uthmani chips |
| `specs/ayah-advance-fix-spec.md` | Dagger-alef / Simple P1 |
| `specs/stt-confidence-filter-spec.md` | Why words disappear from Heard |
| `specs/implementation-spec.md` § Phase 3 lexicon correction | Prior art for ayah-constrained post-STT fix |
| Tanzil Uthmani vs Simple | Authoritative orthography pairing |

---

## 13. Open questions

1. Should in-vocab revive use **pre-calibration** or **post-calibration** confidence against `STT_INVOCAB_FLOOR`? (Recommend **post-calibration**, same number the UI would show in lab.)  
2. Default `STT_HEARD_PROJECT_TO_CORPUS` on or off for first UX release?  
3. P2 short-ayah `passed` coupling to `progress` — ship with P0 or wait for false-advance reports from `force_assess`?  
4. Store `text_simple` in `quran.json` vs sidecar file for Flutter payload size?
