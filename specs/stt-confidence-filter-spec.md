# STT Confidence Filter — Heard Words Spec

**Status:** Implemented (P0 + P1 protocol fields)  
**Phase:** 2.x (bugfix + SpeechRecognizer contract extension)  
**Companion:** `realtime-stream-spec.md` (partials / Heard), `implementation-spec.md` §9 (STT), `ayah-advance-fix-spec.md` (coverage vs score — do not regress)  
**Version:** 1.1  
**Last updated:** 2026-08-14

---

## 1. Purpose

In **Continuous** mode the UI **Heard** line can show a long Arabic string the reciter never said. Target ayah chips stay red, live coverage is low, and the session feels broken — even though the user is listening at an 85% accuracy threshold.

This spec:

1. Reconstructs the failure from a real Al-Fatihah **1:5** session
2. Uses **STT decoder confidence** to decide which Heard words to keep, with the **same numeric cutoff** as the Accuracy threshold (default **0.85**)
3. Requires the recognizer to **drop any word below that cutoff** before they reach the UI or the assessor
4. Gates whole-utterance hallucinations (silence / noise decoded as fluent garbage)
5. Defines tests that today’s `MockSpeechRecognizer` string-only contract cannot catch

**Constraint:** Do not mutate stored Quran text. STT stays behind `SpeechRecognizer` — no Moonshine / Transformers imports in API routers. REST `/assess` and WS `/stream` keep one recognizer and one assessor. Do **not** lower `DEFAULT_THRESHOLD` or `STREAM_COVERAGE_THRESHOLD` to hide garbage.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Range | Surah 1 (Al-Fatihah), start 1, end 7 |
| Accuracy threshold | **85%** (memorization pass/fail slider) |
| On fail | Retry same ayah |
| Current ayah | **1:5** (`إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ`) |
| Mic | on |
| Live % | **25%** (coverage / progress, not the slider) |
| Highlights | All four display words **red** (`word-mismatch` / pending-fail) |
| Heard | `يَا كَلْبُ دُعِيَاتٍ لَسْتَ عَيْنَا يَا كَلْبُ دُعِيَا كُلَّا اسْتَعِينُ` |
| User report | Those words were **not recited** |

Target (4 words) vs Heard (~11 tokens) is typical **ASR hallucination / over-generation**: Moonshine still emits a fluent Arabic sentence when it is guessing (noise, leftover buffer, mid-word audio, or low-energy frames).

The last Heard token `اسْتَعِينُ` is a near-miss of `نَسْتَعِينُ` — enough to produce ~25% coverage (1/4) and paint nothing green. The rest is invented speech, including language that must never appear in a Quran recitation UI.

**What the 85% slider did not do:** it never filtered Heard. That control is `MemorizationAssessor.threshold` (`passed = score >= threshold`). It runs **after** STT, on whatever string STT returned. This spec reuses that **same 0.85 (or live slider value)** as the per-word STT keep floor — it does **not** hide the whole Heard line when the recitation score is low.

---

## 3. Background — how Heard is supposed to work

```text
PCM chunks
    │
    ├─ energy VAD ── silence_* ── run_assess() ─► ayah.result.recognized
    │
    └─ periodic STT (unified tick)
           │
           ├─ transcribe_audio(full buffer)  → raw Arabic string
           ├─ partial.transcript.recognized  → App.vue liveRecognized → “Heard: …”
           └─ assessor.progress / alignment  → bar + word chips
```

### 3.1 Today’s STT contract

`MoonshineArabicRecognizer._transcribe_samples`:

```python
generated_ids = self._model.generate(**inputs)
return self._processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()
```

- Greedy (or default) decode
- **No** `output_scores`
- **No** per-token or per-word probability
- Empty / noisy audio still yields **some** Arabic string (seq2seq models do not return “I don’t know”)

`SpeechRecognizer.transcribe` / `transcribe_audio` return `str`. Downstream has no way to skip unsure words.

### 3.2 How that string hits the UI

| Path | File | Behavior |
|------|------|----------|
| Stream partials | `stream_session._partial_events_from_recognized` | Emits `partial.transcript` with the **raw** string even when it is garbage |
| Stream finalize | `stream_session.run_assess` | `ayah.result.recognized` = same raw string (or `recognized_hint` from the probe) |
| REST | `api/memorization.py` | `speech.transcribe(path)` → assessor → `recognized` in JSON |
| Vue Heard | `App.vue` | `liveRecognized = msg.recognized`; rendered if non-empty: `Heard: {{ liveRecognized }}` |

There is **no** client-side filter. Whatever the model decoded is shown.

### 3.3 Two different quantities, one cutoff

Decoder confidence and recitation score are **not the same measurement**. They **share the Accuracy threshold** as the keep / pass floor (default `DEFAULT_THRESHOLD = 0.85`).

| Number | Source | Meaning | Role of Accuracy threshold *T* |
|--------|--------|---------|--------------------------------|
| **Accuracy slider *T*** | `session.start.config.threshold` / REST `threshold` (default **0.85**) | User’s bar | **Same *T*** for STT word-keep and `passed` |
| **Live % (25%)** | `partial.alignment.progress` | Fraction of expected tokens matched (coverage) | Not *T* — finalize gate stays `STREAM_COVERAGE_THRESHOLD` |
| **Word match 0.75** | `WORD_MATCH_THRESHOLD` | Fuzzy promote `replace` → green chip | Unchanged; not the Heard filter |
| **STT word confidence** | Decoder token probabilities | Model certainty for **this** Heard word | Keep word iff `confidence >= T` |
| **Recitation `score`** | `fuzz.ratio` expected vs filtered transcript | Did they recite the ayah? | `passed` iff `score >= T` |

**Do** drop an STT word whose decoder *P* is 0.72 when *T* is 0.85.

**Do not** hide the entire Heard line because overall `score` or live coverage is below *T* — that would conceal a clearly transcribed wrong ayah.

### 3.4 Relevant files

| File | Role |
|------|------|
| `backend/app/services/speech_service.py` | Moonshine `generate` + mock; **must** grow a scored transcript type |
| `backend/app/config.py` | `STT_*` knobs; word floor **is** `DEFAULT_THRESHOLD` / session `threshold` |
| `backend/app/services/stream_session.py` | Partials + probe + `run_assess` consume filtered text |
| `backend/app/services/stream_audio.py` | Energy VAD exists but **periodic STT still runs on the full buffer**, including quiet tails |
| `backend/app/api/memorization.py` | REST uses `transcribe()` → must inherit the filter |
| `frontend/src/App.vue` | Heard line is an unfiltered dump of `recognized` |
| `frontend/src/highlight.js` | Maps alignment onto **target** chips; does not filter Heard |

---

## 4. Root cause

### 4.1 Primary — decoder always speaks

Moonshine Arabic Tiny is a seq2seq ASR. On uncertain audio it still emits the highest-logit continuation. Those tokens can be:

- Unrelated modern Arabic (`يا كلب`, `لست`, …)
- Repeated fragments (`يا كلب` twice in one Heard line)
- One lucky Quranic-looking tail (`استعين`)

Because `generate()` is called without scores, the backend **cannot tell a recited `إياك` from a hallucinated `يا كلب`**. Both look like equally valid strings to the assessor.

### 4.2 Secondary — Heard is a raw dump

`partial.transcript.recognized` is the full decoder string. The Vue template shows it whenever it is non-empty. Inserts from alignment are **not** stripped from Heard (alignment only paints the **target** chips). Extra hallucinated words therefore dominate the only line the user reads as “what the app heard.”

### 4.3 Tertiary — periodic STT on non-speech

Unified periodic STT snapshots the **entire** ring buffer on a timer (`STREAM_COMPLETION_PROBE_MS` / partial cadence). VAD is used for silence finalize, **not** as a prerequisite for the probe. Quiet or noisy buffers still go to Moonshine → classic hallucination.

`STREAM_MIN_UTTERANCE_MS` (400 ms) only checks **buffer length**, not energy.

### 4.4 What “match the Accuracy threshold” means (and does not)

**Required:** per-word STT keep floor **= Accuracy threshold *T*** (default **0.85**). If the user moves the slider to 90%, drop Heard words with decoder *P* &lt; 0.90. REST `/assess?threshold=` uses that request’s *T*.

**Forbidden:** hide the **whole** Heard line because recitation `score` or live coverage is below *T*.

| User action | Decoder *P* | Recitation score | Correct UX at *T* = 0.85 |
|-------------|-------------|------------------|--------------------------|
| Recites 1:5 correctly, STT sure | words ≥ 0.85 | high | Show Heard; pass if score ≥ *T* |
| Recites **wrong** ayah clearly | words ≥ 0.85 | low | **Show** Heard (G4); fail |
| Noise decoded as `يا كلب` at *P* = 0.22 | &lt; *T* | n/a | **Drop** those words |
| Quiet / no speech | — | — | No STT / empty Heard |

### 4.5 Ruled out (not this screenshot)

| Hypothesis | Why it is not the 1:5 garbage Heard |
|------------|-------------------------------------|
| Dagger-alef coverage stall (`ayah-advance-fix-spec.md`) | That bug is **green chips + correct Heard + stuck**. Here chips are **red**, Heard is **wrong**, live % is **25%**. |
| `fail_policy=retry` | Retry runs after `ayah.result`. Status is still live listening (`mic on`). |
| User really said those words | User report + content (`يا كلب` ×2) + 11 tokens vs 4-word ayah. |
| Frontend invented tashkeel | Heard is server `recognized`; Vue does not diacritize. Model/decode emitted that string. |
| Client denoise introducing artifacts | Possible aggravator (`client-noise-suppression-spec.md`); not required to explain unfiltered greedy decode. Fix STT confidence regardless of denoise engine. |

### 4.6 Aggravating UX

1. A Quran app displaying vulgar / unrelated Arabic as “Heard” destroys trust.
2. Red chips + long Heard look like “the app is listening but you are terrible,” so users shout or repeat into more hallucination.
3. Over-long Heard can **depress** character `score` if a finalize ever runs (extra tokens in the fuzz ratio), causing false fails on a mostly-correct recitation plus garbage tail.

---

## 5. Goals and non-goals

### 5.1 Goals

**G1.** Heard shows **only words whose decoder confidence is ≥ the Accuracy threshold *T*** (default **0.85**). Unsure tokens are omitted, not shown as if recited. *T* follows the slider / REST `threshold` for that session or request.

**G2.** The **same filtered transcript** is what `MemorizationAssessor` scores (REST and stream). Garbage must not become `extra_words` / alignment inserts that punish a real recitation.

**G3.** If the **whole** decode is a hallucination (low sequence confidence and/or non-speech audio), treat it as **empty**: no Heard line, no coverage bump, no false `ayah.result`.

**G4.** High-confidence **wrong** words still appear (genuine mistakes). Pass/fail stays `score >= threshold`.

**G5.** Fatihah auto-advance P0 (`ayah-advance-fix-spec.md`) still works: a **confident** simple-Arabic 1:2 transcript still reaches `progress() == 1.0` and advances at 85%.

**G6.** Tests can inject **per-word confidence** without loading Moonshine.

### 5.2 Non-goals

- Tajweed scoring
- Replacing Moonshine / Quran-fine-tuned ASR (Phase 3)
- Forced decoding of the expected ayah (would fake a pass)
- Hiding the **entire** Heard line when recitation score or live % is below *T* (that is not “matching the slider”)
- A second independent Heard-strictness slider (one control: Accuracy)
- Mutating `quran.json`
- Changing REST `/assess` required JSON fields (additive fields only)
- Lowering `STREAM_COVERAGE_THRESHOLD` or `DEFAULT_THRESHOLD`

---

## 6. Design — layered filter

Ship **P0** to stop the screenshot. **P1** adds protocol/debug fields. **P2** is optional decoding research.

```text
audio
  │
  ├─ [P0] energy gate: skip STT if buffer is non-speech
  │
  ▼
model.generate(..., output_scores=True)
  │
  ├─ token log-probs → word confidence (min of subwords)
  ├─ [P0] drop words with confidence < T  (T = Accuracy threshold, default 0.85)
  ├─ [P0] if sequence_confidence < STT_SEQUENCE_CONFIDENCE_MIN → empty
  └─ [P0] over-generation guard on partials (see §6.5)
        │
        ▼
   Transcription.text  ──► partial.transcript / assess / Heard
```

### 6.1 P0 — Scored transcription (must ship)

Change the recognizer to compute decoder probabilities on every Moonshine pass.

**Normative generate flags** (Transformers `AutoModelForSpeechSeq2Seq`):

```python
out = model.generate(
    **inputs,
    return_dict_in_generate=True,
    output_scores=True,
    max_new_tokens=settings.STT_MAX_NEW_TOKENS,  # cap runaway decode
)
sequences = out.sequences
transition = model.compute_transition_scores(
    sequences, out.scores, normalize_logits=True
)  # log-probs, shape [batch, gen_len]
token_probs = transition[0].exp()  # [gen_len]
```

Skip special tokens when aligning scores to surface text (pad / bos / eos / decoder start). If a token is skipped in `batch_decode(..., skip_special_tokens=True)`, drop its score too.

**Token → word:**

1. Decode **each** generated id with the processor/tokenizer (`skip_special_tokens` per token; empty pieces ignored).
2. Concatenate pieces in order. A **word boundary** is whitespace in the decoded stream (after the same Unicode NFKC the processor already applies — do **not** run `normalize_arabic` here; that is assessor-only).
3. Word confidence = **minimum** token probability of pieces that form that word (pessimistic: one unsure subword drops the word).
4. Sequence confidence = **mean** of kept-candidate token probabilities over the full generated sequence (including words that will be dropped). Use the unfiltered sequence so a fluent hallucination still looks “low-ish” or, if it is overconfident, the energy + over-gen gates still catch it.

**Keep rule (per word):**

```text
T = session.config.threshold          # WS: from session.start
  | request threshold                 # REST: multipart threshold, else DEFAULT_THRESHOLD
  | DEFAULT_THRESHOLD                 # 0.85 when neither is set

kept = (word.confidence >= T)
```

At the screenshot setting (*T* = **0.85**), drop any word with decoder *P* &lt; 0.85.

`Transcription.text` = kept words joined by a single space (Arabic, RTL display unchanged). Dropped words do not leave a placeholder in `text`.

**Empty-utterance rule:**

```text
if sequence_confidence < STT_SEQUENCE_CONFIDENCE_MIN:
    text = ""
    # all words.kept = false
```

Also empty if, after the keep rule, **zero** words remain.

### 6.2 P0 — SpeechRecognizer contract

Keep `transcribe` / `transcribe_audio` returning `str` so existing callers stay valid. That string **must** be the **filtered** `Transcription.text` (G2).

Add a detailed path used by stream (and tests):

```text
TranscriptWord
  text: str
  confidence: float          # 0..1
  kept: bool

Transcription
  text: str                  # filtered; what assessor + UI use
  raw_text: str              # unfiltered decode (never shown in production UI)
  words: list[TranscriptWord]
  sequence_confidence: float # 0..1
  skipped_reason: str | None # None | "low_sequence" | "no_speech" | "empty"

SpeechRecognizer
  transcribe(path, *, threshold: float | None = None) -> str
  transcribe_audio(samples, sr, *, threshold: float | None = None) -> str
  transcribe_detailed(path, *, threshold: float | None = None) -> Transcription
  transcribe_audio_detailed(samples, sr, *, threshold: float | None = None) -> Transcription
```

`threshold` is Accuracy *T*. `None` → `settings.DEFAULT_THRESHOLD` (**0.85**). Adding the kw-only argument is backward compatible for mocks that ignore extra kwargs.

Default ABC implementations: `transcribe_detailed` calls `transcribe` and wraps every whitespace token at `confidence=1.0`, `kept=True`, `sequence_confidence=1.0`. **Moonshine overrides** with real scores then applies the keep rule at *T*. **Mock** accepts optional per-word confidences (see §10) and the same `threshold`.

Routers still must not import Transformers.

### 6.3 P0 — Energy / no-speech gate (must ship with scores)

Periodic STT and final STT **skip the model** when the snapshot is not speech:

```text
rms = sqrt(mean(square(samples)))
if rms < STREAM_VAD_RMS_THRESHOLD:          # same threshold as EnergyVadSegmenter
    return Transcription(text="", skipped_reason="no_speech", ...)
```

Optional tightening (allowed): require at least `STREAM_MIN_UTTERANCE_MS` of frames **above** RMS, not merely buffer duration.

Do not emit `partial.transcript` when `text == ""` (avoids flashing empty Heard). Do not treat no-speech as `ayah.result` fail.

This stops the common “mic on, room noise, Moonshine writes a paragraph” path even if token softmax is overconfident.

### 6.4 P0 — Default knobs

| Setting | Default | Role |
|---------|---------|------|
| `DEFAULT_THRESHOLD` | **0.85** | Accuracy *T*: recitation `passed` **and** STT word-keep floor |
| `STT_CONFIDENCE_FILTER` | `true` | Master switch (tests / emergency rollback) |
| `STT_SEQUENCE_CONFIDENCE_MIN` | **0.50** | Whole decode discarded below this (not the slider; hallucination dump) |
| `STT_MAX_NEW_TOKENS` | **64** | Cap hallucinated essays; Fatihah 1:7 is ~9 words |
| `STT_PARTIAL_MAX_OVERGEN_RATIO` | **2.0** | See §6.5 |

**No separate `STT_WORD_CONFIDENCE_MIN`.** Word keep is *T*:

| Context | *T* |
|---------|-----|
| Continuous WS | `session.start.config.threshold` (UI Accuracy slider) |
| REST `/assess` | multipart `threshold` if sent, else `DEFAULT_THRESHOLD` |
| CLI / tests that omit it | `DEFAULT_THRESHOLD` (**0.85**) |

**Why match the slider, not a fixed 0.80:** one control. At the screenshot setting, drop Heard words below **0.85**. If the user raises Accuracy to 90%, Heard must be at least as strict.

**Why not also raise `STREAM_COVERAGE_THRESHOLD`:** that is completeness of the ayah, not decoder certainty. Leave it at 0.85 independently (it already matches the default *T* numerically).

Document `STT_CONFIDENCE_FILTER` and `STT_SEQUENCE_CONFIDENCE_MIN` in `.env.example`. Do not add a second Heard slider.

### 6.5 P0 — Partial over-generation guard

Even after word filtering, a **confident** hallucination can be longer than the ayah (softmax peaked on the wrong sentence).

For **`partial.transcript` / `partial.alignment` only** (live UX):

```text
exp_n = len(tokenize(expected))
rec_n = len(tokenize(filtered_text))
if rec_n > max(exp_n * STT_PARTIAL_MAX_OVERGEN_RATIO, exp_n + 2):
    # Do not dump the essay into Heard.
    # Keep only words that fuzzy-match some expected token at WORD_MATCH_THRESHOLD.
    # Remaining extras are omitted from partial.recognized (not from lab raw_text).
```

This is **display/partial** only. It must **not** silently delete high-confidence extras from **final** `ayah.result` — those extras are real evidence of added words (G4). Final assess still uses the confidence-filtered full `text` (no over-gen strip), so a user who actually recited two ayahs without pause is scored honestly (extra_words / suffix logic already exist).

Rationale for the screenshot: 11 Heard tokens vs 4 expected, coverage 0.25 → partial guard would never show `يا كلب…` even if those tokens were spuriously high-prob.

### 6.6 P1 — Protocol fields (should ship same PR or immediately after)

Additive, backward compatible.

`partial.transcript`:

```json
{
  "type": "partial.transcript",
  "surah": 1,
  "ayah": 5,
  "recognized": "نستعين",
  "stable": false,
  "stt_ms": 210,
  "sequence_confidence": 0.62,
  "words": [
    { "text": "نستعين", "confidence": 0.91, "kept": true }
  ]
}
```

- `recognized` = filtered `text` (existing clients keep working).
- `words` omitted when `STT_CONFIDENCE_FILTER=false` or mock-without-scores.
- **Never** send `raw_text` on the production socket (may contain the garbage we are hiding). Lab WS dump (`?lab=1`) **may** include `raw_recognized` for debugging.

`ayah.result` / REST `/assess`: optional `sequence_confidence` (float) and `stt_words` (same shape). Required fields unchanged.

Frontend P1: Heard renders **kept** words from `words` if present; else `recognized`. Do not show dropped words in production.

### 6.7 P2 — Decoding research (out of scope for the screenshot)

| Idea | Why not P0 |
|------|------------|
| Force-decode / prefix the expected ayah | Fakes a correct recitation; violates G4 |
| Constrain beam to Quran lexicon | Large change; Phase 3 ASR territory |
| Whisper-style `no_speech_threshold` | Moonshine Tiny via seq2seq `generate` has no Whisper `no_speech_prob`; energy gate is the analogue |
| Raise temperature / sampling | Makes Heard **more** random; we need suppression |

Client denoise (already specified) **reduces** how often this path fires; it does **not** replace decoder scores.

### 6.8 Explicitly rejected “fixes”

| Idea | Why reject |
|------|------------|
| Hide Heard unless recitation **score** or live % ≥ *T* | Hides genuine wrong recitations. Matching the slider means **per-word decoder *P* ≥ *T***, not “only show Heard after a pass” |
| Hide Heard unless all chips are green | Same; also fails while the user is still mid-ayah |
| Show only alignment `equal` tokens as Heard | Mid-ayah would show a prefix (OK) but **wrong** recited words would vanish (bad for learning) |
| Frontend regex / blocklist (`كلب`, …) | Whack-a-mole; does not fix `لست عينا` etc. |
| Lower coverage threshold so 25% advances | Would auto-advance on garbage |
| `max_new_tokens=4` globally | Truncates 1:7 and every longer ayah |

---

## 7. Stream session behavior after P0

No required protocol break. Same event types.

| Path | Before | After P0 |
|------|--------|----------|
| Periodic STT, quiet buffer | Moonshine hallucinates → Heard dump | `skipped_reason=no_speech` → **no** `partial.transcript` |
| Periodic STT, mixed speech + garbage | Full garbage string in Heard; 25% bar | Unsure words dropped; Heard is short or empty; over-gen guard on leftovers |
| Coverage probe | `progress(expected, raw)` | `progress(expected, filtered)`; garbage inserts no longer inflate/deflate oddly |
| `run_assess` | Scores raw hallucination | Scores filtered text; empty → existing empty-transcript fail **only** on explicit finalize (`force` / silence with coverage). Silence + empty filtered + low coverage → still `return []` (keep listening) |
| REST `/assess` | Same hallucination in `recognized` | Filtered `recognized`; empty → existing fail-200 path |
| Confident correct 1:2 | Unchanged | Unchanged (G5) |
| Confident **wrong** ayah | Shown (good) | Still shown (G4) |

`recognized_hint` on `_assess_trigger` must be the **filtered** string from the same STT call (do not re-decode without scores).

When `STT_CONFIDENCE_FILTER=false`, behavior matches today’s unfiltered `generate` (minus optional `max_new_tokens` cap, which may stay on for safety).

---

## 8. Frontend (minimal P0)

P0 does **not** require Vue changes if the backend never sends garbage in `recognized`. Still do this in the same PR (defense in depth):

1. Render Heard only when `liveRecognized.trim()` is non-empty (already the case).
2. Do **not** hide Heard in Vue because live % or last `score` is below the slider — the server already dropped words with *P* &lt; *T*.
3. If `msg.words` exists (P1), join items with `kept === true` only.
4. Optional caption: “Heard (≥ accuracy threshold)”.
5. Lab (`?lab=1`): may show sequence confidence next to live %; never show `raw_text` in the default UI.
6. When the user moves the Accuracy slider **during** a live session, send the new `threshold` on the next `session.start` (or a small `session.update` if already specified). Until then, the session keeps the *T* from start — same as pass/fail today.

No change to `highlight.js` word chips: they follow alignment on the **filtered** transcript, so hallucinated inserts disappear and chips stay pending/red until real words arrive — which is correct.

---

## 9. Interaction with other specs

| Spec | Interaction |
|------|-------------|
| `ayah-advance-fix-spec.md` | Coverage uses filtered STT. Tests A1–A7 / S1–S3 must still pass with mock confidences of **1.0**. Do not re-introduce exact-token-only coverage. |
| `partials-evaluation-spec.md` | Partials stay UX; they must not display unfiltered probes. Unified tick still one STT pass. |
| `client-noise-suppression-spec.md` | Complementary: cleaner audio → fewer low-conf tokens. Confidence filter still required. |
| `implementation-spec.md` §9 | Extend the ABC as in §6.2; still no model imports in routers. |
| `realtime-stream-spec.md` §6.2 | `partial.transcript.recognized` definition becomes **confidence-filtered** text. Document P1 fields when shipped. |

---

## 10. Test plan (normative)

Tests must **fail on current main** (string-only STT, Heard = raw) and **pass after P0**. Do not load Moonshine in CI.

### 10.1 Recognizer unit (`backend/tests/test_speech_confidence.py` — new)

Pure functions: token-prob → words → filter. Feed **fake** ids/scores or a testable helper `filter_transcription(raw_text, word_confidences, *, threshold, seq_min) -> Transcription` (`threshold` = Accuracy *T*, default 0.85).

| ID | Input | Assert |
|----|--------|--------|
| C1 | words `يا` 0.21, `كلب` 0.18, `نستعين` 0.92; seq mean 0.44; *T* = 0.85 | `text == ""` if seq &lt; 0.50; if seq computed only on these three, empty via sequence gate |
| C2 | same words, **force** seq mean 0.70, *T* = 0.85 | `text == "نستعين"`; `يا`/`كلب` not in `text`; `kept` flags match |
| C3 | all words ≥ 0.85, seq 0.90, text `اياك نعبد واياك نستعين`, *T* = 0.85 | `text` unchanged (identity) |
| C4 | empty raw / no tokens | `text == ""`, `skipped_reason` in `{empty, low_sequence}` |
| C5 | `STT_CONFIDENCE_FILTER=false` | `text == raw_text` even if confidences are low |
| C6 | Word conf **0.849** vs *T* = **0.85** | dropped; **0.85** kept (boundary) |
| C7 | Word conf 0.86, *T* = 0.85 → kept; same word, *T* = 0.90 → dropped | floor **tracks** Accuracy threshold |

Do **not** call the real HF model in this file.

### 10.2 Mock + stream (`backend/tests/test_stream.py`)

Extend `MockSpeechRecognizer` to accept:

```python
MockSpeechRecognizer(
    transcript="يا كلب دعياة لست عينا يا كلب دعيا كلا استعين",
    word_confidences=[0.15, 0.12, 0.20, 0.18, 0.22, 0.14, 0.11, 0.19, 0.16, 0.88],
)
```

When `word_confidences` is omitted, every token is 1.0 (existing tests stay green).

| ID | Setup | Assert |
|----|--------|--------|
| S-H1 | Screenshot-like transcript + low confidences except last `استعين` 0.88; session on **1:5**; **threshold 0.85**; partials on | `partial.transcript.recognized` does **not** contain `كلب` or `يا`; 0.88 ≥ 0.85 so `استعين` may remain |
| S-H2 | Same | `partial.alignment.progress` is not computed against the 11-word dump (no 11 insert ops of garbage). Progress ≤ 0.25 or based on kept words only |
| S-H3 | `transcript=""` / `skipped_reason=no_speech` | **no** `partial.transcript` event (or `recognized=""` and frontend would hide — prefer **omit event**) |
| S-H4 | Mock 1:2 `الحمد لله رب العالمين` at conf 1.0, threshold 0.85 | Still S1 from ayah-advance spec: coverage finalize + advance to 1:3 |
| S-H5 | High-conf **wrong** transcript `مالك يوم الدين` on 1:5 (all words 0.95) | Heard / `recognized` **keeps** that string (G4); score fails at 0.85; no silent drop |
| S-H6 | RMS-zero PCM snapshot, real `Moonshine` **not** used — session energy gate | No STT call (`recognizer.calls` unchanged) when buffer is below VAD RMS |
| S-H7 | Same as S-H1 but session **threshold 0.90**; last word 0.88 | `استعين` **dropped** (0.88 &lt; 0.90); Heard empty or omit event |

### 10.3 Assessor (no change required except fixtures)

Existing `test_core.py` assessor tests pass unchanged (they do not go through STT).

Add one stream/REST integration with mock filtered empty: empty recognized → fail assessment on **force**, not a 500.

### 10.4 Manual / lab (Moonshine on)

1. Continuous, Fatihah 1–7, threshold 85%, retry, mic on, **quiet room, do not speak** for 10 s. Expect: no Heard dump, chips pending, no advance.
2. Recite **1:5** clearly. Expect: Heard is a short simple-Arabic line close to `اياك نعبد واياك نستعين` (or a confident prefix), **not** `يا كلب…`. Chips move green as coverage rises. Advance still works.
3. Recite **1:4** while the target is 1:5. Expect: Heard shows the 1:4 words (confident mistake), fail + retry, **not** an empty Heard.
4. `?lab=1` WS dump: `recognized` ≠ raw hallucination from step 1; optional `sequence_confidence` present after P1.
5. Repeat step 1 with denoise on and off — both must hide garbage (filter is server-side).

If step 2 drops real Fatihah words at *T* = 0.85, **do not** silently lower the floor in code. Record it here and decide whether to keep matching the slider (product choice) vs a documented exception. Default remains **match *T***.

---

## 11. Acceptance

This spec is **implemented** when:

1. C1–C7 and S-H1–S-H7 pass in CI without downloading the model
2. Manual §10.4 step 1 cannot reproduce the screenshot Heard line in a quiet room
3. Manual §10.4 step 2 still auto-advances Fatihah with natural pauses (G5)
4. Manual §10.4 step 3 still **shows** the wrong ayah as Heard (G4)
5. REST `/assess` with mocked low-conf garbage does not return that garbage as `recognized`
6. `DEFAULT_THRESHOLD` stays **0.85** (now also the default STT word floor). `WORD_MATCH_THRESHOLD` and `STREAM_COVERAGE_THRESHOLD` are unchanged
7. No Transformers / Moonshine imports outside `speech_service.py` (and prefetch)

P1 protocol fields are **not** required to close the screenshot if `recognized` is already filtered; they are required before any UI that shows per-word confidence.

---

## 12. Implementation sketch (for the implementing agent)

Order:

1. Add `Transcription` / `TranscriptWord` + `filter_transcription()` helper in `speech_service.py` (or `stt_confidence.py` next to it). Unit tests §10.1 **red then green**.
2. `config.py` + `.env.example`: `STT_*` knobs (§6.4).
3. Moonshine `_transcribe_samples`: `generate` with scores; map tokens → words; filter at `threshold` (default 0.85). Stream passes `self.config.threshold`; REST passes the request threshold. Keep `transcribe_audio_detailed`.
4. Energy gate in `stream_session.run_periodic_stt` / `run_assess` **before** `transcribe_audio` (§6.3). Reuse `STREAM_VAD_RMS_THRESHOLD`. Pass `threshold=self.config.threshold` into detailed transcribe.
5. Omit empty `partial.transcript` events. Pass filtered `recognized_hint`.
6. Partial over-gen guard §6.5 (session, not assessor).
7. Extend `MockSpeechRecognizer` + stream tests §10.2.
8. Frontend: consume `words` if present; never display `raw_text`.
9. Optional P1 fields on events / REST.
10. Do **not** retune `STREAM_COVERAGE_THRESHOLD` or change the default Accuracy value in this PR — only **apply** existing *T* to STT word-keep.

CPU note: `output_scores=True` keeps per-step logits. For Tiny + `STT_MAX_NEW_TOKENS=64` this is acceptable on the existing 1–2 session CPU budget. Do not enable `output_attentions`.

---

## 13. Appendix — screenshot transcript (test gold)

Display / expected (Uthmani 1:5) stays in the corpus. This is **only** a recognizer-input fixture:

```text
raw:     يا كلب دعياة لست عينا يا كلب دعيا كلا استعين
kept:    استعين                         # if only this token ≥ T (0.85 in the screenshot)
shown:   (Heard hidden if sequence_confidence < 0.50; else “استعين”)
forbidden in UI:  كلب, يا كلب, دعياة, لست, عينا
```

Undiacritized form is what tests should use (Moonshine-like). The live UI may show tashkeel if the model emits it; the filter is confidence-based, not orthography-based.

---

## 14. References

| Document / code | Relevance |
|-----------------|-----------|
| This screenshot (2026-08-14) | 1:5, 25%, red chips, Heard hallucination, 85% slider, retry |
| `backend/app/services/speech_service.py` | Unscored `generate` |
| Hugging Face `generate(output_scores=True)` + `compute_transition_scores(..., normalize_logits=True)` | Token log-probs |
| `backend/app/services/stream_session.py` | Raw string → `partial.transcript` |
| `frontend/src/App.vue` | `Heard: {{ liveRecognized }}` |
| `specs/ayah-advance-fix-spec.md` | Do not regress coverage / Fatihah advance |
| `specs/implementation-spec.md` §9 | STT ABC |
| `specs/realtime-stream-spec.md` §6.2 | Partial event shapes |
| `specs/client-noise-suppression-spec.md` | Complementary, not sufficient |

---

## 15. Open questions

1. **Lab 2026-08-14:** Tiny greedy softmax is **not** on the Accuracy-slider scale. Matching raw *P* ≥ 0.85 emptied REST `/assess` Heard (Fatihah 1:1, score 0%) and dropped `الرحمن` from Continuous 1:3 while `الرحيم` stayed. **Exception:** `MoonshineArabicRecognizer` maps decoder *P* with `p ** STT_DECODER_PROB_GAMMA` (default **0.12**) before the keep rule, and uses the **mean** of subword probs (not min) for word confidence. Mock tests still compare raw *P* to *T*. Screenshot garbage (~0.12–0.22) stays below 0.85 after the map; typical speech (~0.28+) passes. Do not set gamma to 1.0 without another lab pass.
2. Should sequence confidence be a **length-normalized geometric mean** (harsher on one bad token) instead of arithmetic mean?
3. Expose a lab-only “show dropped words (dimmed)” toggle for tuning, never default-on?
4. REST response: add `sequence_confidence` now (P1) or wait until a Flutter client needs it?
5. Is `STT_MAX_NEW_TOKENS=64` enough for the longest commonly streamed ayahs in Continuous mode (Al-Baqarah 2:282 is far above 64 tokens — Continuous MVP is Fatihah-scale; raise the cap when long-ayah streaming is a goal, do not use an uncapped decode to “be safe”).
