# Continuous vs Single Ayah — Detection Gap Investigation Spec

**Status:** Partially implemented (H1/H2/H5/H7 shipped 2026-08-14; quiet-mic / empty-force regression also fixed)  
**Phase:** 2.x  
**Companion:**  
- `realtime-stream-spec.md` (WS session, VAD, coverage finalize)  
- `implementation-spec.md` (REST `/assess`)  
- `ayah-advance-fix-spec.md` (coverage gate vs score — related, **not** this bug)  
- `stt-confidence-filter-spec.md` (Heard keep-floor)  
- `uthmani-tanzeel-word-matching-spec.md` (Heard recovery)  
- `client-noise-suppression-spec.md` (shared capture graph)  
- `partials-evaluation-spec.md` (periodic STT windows)  
**Version:** 1.1  
**Last updated:** 2026-08-14

---

## 1. Purpose

**Single ayah** mode (REST `POST /api/memorization/assess`) can pass Al-Fatihah **1:3** at a high score, while **Continuous** mode (WS `/api/memorization/stream`) on the same ayah shows ~**50%**, paints `ٱلرَّحِيمِ` red, and does not auto-advance.

This is **not** “STT cannot hear 1:3.” The same model, assessor, and normalizer succeed on the REST path. The gap is **how Continuous captures, windows, and scores audio** relative to Single.

This spec:

1. Reconstructs the failure from a 2026-08-14 Continuous session on **1:3**
2. Names the exact pipeline differences (capture → STT → UI)
3. Ranks hypotheses that can produce a wrong second word while REST still passes
4. Defines a **same-recitation A/B protocol** that isolates capture quality vs windowing vs scoring
5. Lists instrumentation and a decision matrix for the first real fix — without changing pass thresholds to hide the bug

**Constraint:** Do not mutate stored Quran text. STT stays behind `SpeechRecognizer`. REST and WS keep one recognizer and one assessor. Do **not** lower `DEFAULT_THRESHOLD` or `STREAM_COVERAGE_THRESHOLD` to make 50% look like a pass.

---

## 2. Bug report (observed)

| Field | Value |
|-------|--------|
| Mode | Continuous (WebSocket) |
| Range | Surah 1 (Al-Fatihah), **start 3**, end 7 |
| Accuracy threshold | **85%** |
| On fail | Retry same ayah (recommended) |
| Current ayah | **1:3** `ٱلرَّحْمَٰنِ ٱلرَّحِيمِ` |
| Mic | **on** |
| Live % | **50%** |
| Highlights | `ٱلرَّحْمَٰنِ` = **match** (green); `ٱلرَّحِيمِ` = **wrong / mismatch** (red) |
| Heard | Truncated first word + garbage second token, e.g. `الرَّحمَ يضحينَ` (not `الرحيم`) |
| Contrast | Same ayah in **Single ayah** mode allegedly **passes at a high score** |

### 2.1 What the UI is actually showing

| Layer | Source | Meaning |
|-------|--------|---------|
| Header `1:3 · mic on · 50%` | `currentAyah` + `micActive` + `liveProgress` | Session still listening; **50% is coverage (`progress()`), not REST `score`** |
| Green / red chips | `partial.alignment` (or last `ayah.result.alignment`) via `wordsFromAlignment` | First expected token equal; second replace/mismatch |
| Heard | `partial.transcript` / `ayah.result` via `heardTextFromMessage` | Confidence-filtered kept words, else `recognized` |
| Auto-advance | `ayah.result` with `passed=true` | **Did not happen** (still on 1:3, mic on) |

For a 2-word ayah, **50% coverage = 1 of 2 expected tokens matched.** That is consistent with a correct-ish `الرحمن` and a failed `الرحيم`. Character-level REST `score` would also fail this Heard string — so this is **not** the `ayah-advance-fix` pattern (Heard semantically complete, coverage gate too strict).

### 2.2 What this is *not*

| Prior bug | Why it does not explain this screenshot |
|-----------|----------------------------------------|
| `ayah-advance-fix-spec.md` | Heard was `الحمد لله رب العالمين` (complete, simple Arabic); live % 75% with **all chips green**. Here Heard is **wrong** and one chip is red. |
| `stt-confidence-filter-spec.md` | Hallucinated a long unrelated sentence on 1:5. Here Heard is **short** (2 tokens) and the first is a near-miss of the target. |
| `uthmani-tanzeel-word-matching-spec.md` | Basmala `بسم` omitted / agglutinated; chips dashed **missing**. Here the second word is a **wrong substitute**, not a delete of `بسم`. |

### 2.3 User-visible failure modes (must distinguish in the lab)

| ID | Symptom | Likely meaning |
|----|---------|----------------|
| **U1** | Live 50% + bad Heard, **no** `ayah.result`, mic stays on | Coverage gate never fires (`progress < 0.85`); silence paths return `[]` |
| **U2** | `ayah.result` `passed=false` ~50%, then `session.waiting` | Finalize **did** run; transcript really failed assess |
| **U3** | Live 50% **during** recitation, then later pass | Benign mid-utterance partial; user compared a snapshot too early |
| **U4** | Single pass / Continuous fail on **the same saved audio** | True mode-delta (this spec’s target) |
| **U5** | Single pass / Continuous fail only on **different takes** | Confound — do not treat as a code bug |

The screenshot matches **U1 or U2** (stuck on 1:3 with wrong Heard). Investigation must prove **U4** before changing code.

---

## 3. Background — two modes, two pipelines

Both modes share:

- Moonshine Arabic Tiny via `SpeechRecognizer`
- `STT_CONFIDENCE_FILTER` + `apply_ayah_recovery`
- `MemorizationAssessor` + `normalize_arabic`
- Browser capture graph `openAudioGraph()` (default denoise `dtln`)

They **do not** share when STT runs, what PCM it sees, or which number the UI prints.

```text
                    getUserMedia + openAudioGraph (DTLN / native / off)
                                    │
                 ┌──────────────────┴──────────────────┐
                 │                                     │
         Single (REST)                          Continuous (WS)
                 │                                     │
    MediaStreamDestination                    AudioWorklet pcm-capture
    MediaRecorder → WebM/Opus                 linear downsample → pcm_s16le
                 │                                     │
    POST /assess (whole clip)                 WS binary 250 ms chunks
                 │                                     │
    ffmpeg → WAV 16 kHz                       PcmRingBuffer (up to 45 s)
    librosa.load → STT once                   energy VAD + periodic STT
                 │                                     │
    assessor.assess → Score %                 progress() → live %
                                              coverage ≥ 0.85 → ayah.result
```

### 3.1 Capture / codec delta

| | Single ayah | Continuous |
|--|-------------|------------|
| Entry | `startProcessedStream()` | `startPcmCapture()` |
| After denoise | `MediaStreamAudioDestinationNode` | `pcm-capture-processor` |
| Wire format | WebM/Opus blob (container) | `pcm_s16le` @ 16 kHz, 250 ms frames |
| Resample to 16 kHz | **ffmpeg** (`prepare_audio`) | **Linear** sample-and-hold in `pcm-worklet.js` (`i += ratio`) |
| When STT runs | After user taps **Stop** (complete clip) | Every ~1 s on the **rolling buffer** while the mic is open; also on silence / force |
| STT API | `transcribe_detailed(path)` → `librosa.load` | `transcribe_audio_detailed(samples)` → `_pcm_to_float32` |
| UI number | `score` (`fuzz.ratio`) | Live `progress()` (matched expected tokens / `len(expected)`); `ayah.result` also has `score` |

If DTLN is active, `AudioContext` is already **16 kHz**, so the worklet ratio is ~1 and linear downsample is a no-op. If denoise **falls back to native**, device rate is typically 44.1/48 kHz: REST still gets high-quality ffmpeg resample; Continuous gets naive linear downsample (aliasing on short trailing syllables).

### 3.2 Why Continuous can “hear” an unfinished ayah

Frontend Continuous start payload (`App.vue`):

- `partials: true`
- `STREAM_COMPLETION_PROBE=true` (server)

→ `uses_unified_periodic()` is **true**: one STT tick (~`min(2000, 1000)` ms) on the **full** attempt buffer, feeding both live Heard and the coverage probe.

For 1:3 (~1–2 s of speech):

1. First tick often lands **between** `الرحمن` and `الرحيم`, or **mid-`الرحيم`**.
2. Seq2seq STT still emits a fluent second token (`يضحينَ` / similar) instead of waiting.
3. Coverage = 0.50 → probe does **not** emit `_assess_trigger`.
4. Natural pause: `silence_short` (400 ms) / `silence` (800 ms in code) call `run_assess`, then **return `[]`** if coverage < 0.85. No beep, no `ayah.result`, attempt counter unchanged.
5. UI keeps the last partial: `mic on · 50%`, first chip green, second red.

Single ayah never STTs until Stop, so the second word is complete in the file.

### 3.3 Buffer after a failed / non-final attempt

| Event | Buffer |
|-------|--------|
| `ayah.result` (pass or fail) | `clear(keep_overlap_ms=STREAM_OVERLAP_MS)` (**300 ms**) |
| Silence with coverage < 0.85 | Buffer **kept**; VAD short-pause or full reset only |
| Retry policy | Same ayah, leftover overlap + any un-finalized speech remain |

Short ayahs are especially sensitive to 300 ms of previous audio glued onto the next try.

**Leftover-carry of surplus tokens onto ayah N+1 is still v1.1 / not shipped.** This investigation is about **wrong Heard on the current ayah**, not pause-less tilawah.

### 3.4 Shared post-STT (should not differ if the string is the same)

```text
raw decode
  → confidence filter (Accuracy T, default 0.85, after gamma calibration)
  → recover_against_ayah (split agglutination; revive in-vocab ≥ 0.55)
  → [partials] trim_overgenerated_partial (only if Heard is far too long)
  → progress() / assess()
```

Recovery **must not invent** `الرحيم` if the decoder never produced it. Garbage `يضحين` staying in Heard is expected given that rule. If REST’s decode **does** contain `الرحيم`, recovery is not the root cause — the stream **audio or window** is.

---

## 4. Investigation question

> **Why does Continuous STT/coverage on 1:3 emit a truncated/wrong second word (live ~50%, no advance) when Single ayah REST on the same recitation passes well above 85%?**

Sub-questions:

1. Is the REST/WS delta **windowing** (incomplete buffer) or **signal** (PCM vs WebM/ffmpeg)?
2. Is live 50% a **stuck partial** (U1) or a real `ayah.result` fail (U2)?
3. Does **Check now** (`ayah.force_assess`) pass when the live UI shows 50%? (If yes: coverage gate / timing. If no: transcript is actually bad.)
4. Does replaying **Continuous PCM** through REST `/assess` still fail? (If yes: capture/resample. If no: stream windowing / VAD / overlap.)
5. Is 1:3 special (2 tokens, short duration) or does the gap reproduce on 1:2 / 1:5 / longer ayahs?

---

## 5. Hypotheses (ranked)

Confirm or kill in this order. Do not implement a fix for Hn until Hn is evidenced.

### H1 — Mid-utterance STT on an incomplete 1:3 (most likely)

Periodic STT + coverage probe run on the rolling buffer **while the second word is still being said** (or immediately after a too-short pause). Moonshine emits a fluent but wrong continuation. Coverage 50% never finalizes.

**Evidence that confirms:** timestamps show STT `audio_ms` shorter than the spoken 1:3; later ticks never replace Heard; `Check now` after a full recitation + pause **passes**; dumping the buffer at Stop and running REST STT **passes**.

**Evidence that kills:** a buffer snapshot taken *after* the user finished 1:3 and paused still decodes `يضحين`; REST on that same snapshot also fails.

### H2 — `stt_busy` freezes the UI on the first bad partial

First tick (incomplete) occupies the model; later complete-window ticks are dropped (`busy`). Live Heard stays at 50%.

**Confirms:** `?lab=1` WS trace / `busy_errors` increment around the recitation; a later `force_assess` (waits / runs full buffer) passes.

**Kills:** traces show later `partial.transcript` after the user finished, still with garbage `الرحيم`.

### H3 — Capture/resample quality (PCM linear downsample vs ffmpeg)

Native/fallback path: 48 kHz → linear 16 kHz aliases the end of `الرحيم`. REST MediaRecorder + ffmpeg does not. DTLN-at-16 kHz should weaken this unless DTLN itself distorts streaming frames differently than the MediaRecorder tap.

**Confirms:** REST `/assess` on a WAV **exported from the WS ring buffer** still fails; REST on the **MediaRecorder blob from the same take** passes. Reproducing with `VITE_AUDIO_DENOISE=off` vs `dtln` changes the gap.

**Kills:** identical float32 16 kHz from both taps, REST and WS STT agree.

### H4 — Trailing syllable eaten by energy VAD / `pcm_has_speech`

`الرحيم` is quieter / longer madd; RMS drops below `STREAM_VAD_RMS_THRESHOLD` (0.015) so the last 200–400 ms never count as speech, or periodic STT is skipped. REST includes that tail because the user has not tapped Stop yet.

**Confirms:** waveform of the ring buffer ends before the spoken `يم`; raising VAD sensitivity or padding 300 ms of tail fixes decode.

**Kills:** buffer clearly contains the full second word.

### H5 — Overlap / retry contamination

`STREAM_OVERLAP_MS=300` or a non-cleared buffer after a low-coverage pause prepends junk. Two-word ayahs have no room to absorb extras; decoder may glue garbage onto token 2.

**Confirms:** fail rate jumps on **retry of the same ayah** vs first attempt after `session.ready`; zeroing overlap in a debug build fixes it.

**Kills:** first attempt on a fresh session (range starting at 3, no prior speech) still fails.

### H6 — Confidence filter / recovery / trim (least likely for this screenshot)

Filter kept both Heard tokens (garbage would otherwise disappear). `trim_overgenerated_partial` only fires when Heard is much longer than expected (ratio 2.0) — a 2-token Heard on a 2-token ayah does not trim. Recovery will not map `يضحين` → `الرحيم`.

**Confirms:** `raw_recognized` contains `الرحيم` but display Heard does not (`kept: false` or trim).

**Kills:** `raw_recognized` already lacks `الرحيم` (STT problem upstream).

### H7 — Metric confusion only (not a STT bug)

User compared Continuous **live coverage** mid-phrase to Single **final score**. After Stop-equivalent (`Check now` or long pause with complete audio), Continuous would also pass.

**Confirms:** waiting until speech **and** a pause, or tapping Check now, yields `ayah.result` ≥ 85%.

**Kills:** after a complete recitation + pause, Heard is still `… يضحين` and Check now fails.

Treat H7 as a **protocol check**, not a product fix. If H7 is the whole story, the fix is UX (don’t paint a hard fail on unstable partials; label live % as “in progress”).

---

## 6. Isolation protocol (must run before coding a fix)

Use **Al-Fatihah 1:3**, threshold **85%**, same mic, same room, same denoise mode (log `[audio] capture profile`).

### 6.1 Lab switches

| Tool | How |
|------|-----|
| WS JSON trace | Open Continuous with `?lab=1` → **Download WS trace** after the attempt |
| Denoise | `VITE_AUDIO_DENOISE=dtln\|native\|off` (rebuild frontend) |
| Force finalize | Continuous **Check now** (`ayah.force_assess`) |
| Backend logs | `STT filtered empty`, session `periodic STT`, `audio_ms` / `stt_ms` on `ayah.result` |

**Needed instrumentation (if not already present)** — debug-only, behind lab / env:

1. Dump the ring-buffer snapshot used for each periodic STT and each `run_assess` as WAV (`/tmp` or session artifact): `session_id`, `ayah`, `reason`, `audio_ms`, `rms`, `recognized`, `raw_recognized`, `coverage`, `score`.
2. Log whether the tick was skipped (`stt_busy`, `pcm_has_speech=false`, `samples < min_utterance`).
3. On REST `/assess`, keep the converted WAV for the same recitation when `?lab=1` (or a `DEBUG_SAVE_AUDIO` flag).

Do not commit recitation audio to git.

### 6.2 Step A — prove Single still passes

1. Mode: Single ayah. Surah 1, ayah **3**, 85%.
2. Recite 1:3 once; Stop.
3. Record: `score`, `passed`, `recognized`, `stt_words`, `recovery`.

**Gate:** if Single also fails, this is not a mode-delta — stop and debug STT/capture globally.

### 6.3 Step B — Continuous on a fresh session

1. New WS session. Range **3–7** (same as the screenshot). Retry policy.
2. Mic on. Recite **only** 1:3, then pause ≥ 1 s. Do not continue to 1:4.
3. Note live Heard / % **while speaking**, after pause, and whether `ayah.result` / `session.waiting` arrived.
4. If still 50% after pause: tap **Check now**. Record `trigger`, `coverage`, `score`, `recognized`, `audio_ms`.

### 6.4 Step C — cross-play the audio (the decisive test)

| Replay | Pass? | Conclusion |
|--------|-------|------------|
| REST blob → REST `/assess` | yes (Step A) | Baseline |
| WS buffer WAV → REST `/assess` | **yes** | **H1/H2/H5** (windowing / busy / overlap). Capture is good enough. |
| WS buffer WAV → REST `/assess` | **no** | **H3/H4** (signal / VAD tail). Windowing alone is not the bug. |
| REST blob decoded to PCM → `transcribe_audio_detailed` | compare | Isolates `librosa.load` vs PCM path (**H3** variant) |

### 6.5 Step D — ablations (only after C)

| Ablation | Kills / confirms |
|----------|------------------|
| `partials: false` (completion probe only) | If 1:3 starts passing: live partials are misleading or stealing the STT slot (**H2**) |
| `STREAM_COMPLETION_PROBE=false` + wait for silence | If silence-only pass: probe-too-early (**H1**) |
| `STREAM_OVERLAP_MS=0` | **H5** |
| `VITE_AUDIO_DENOISE=off` then `native` then `dtln` | **H3** |
| Recite 1:2 (4 words) and 1:5 the same way | Short-ayah-only vs general stream STT |
| First attempt vs immediate retry | **H5** |

### 6.6 Pass/fail log template

```text
take:        #
mode:        single | continuous
denoise:     dtln | native | off
ayah:        1:3
spoken:      complete | interrupted
pause_ms:    …
event:       partial | ayah.result | none
audio_ms:    …
busy_skips:  …
Heard:       …
raw:         …
coverage:    …
score:       …
passed:      …
check_now:   n/a | pass | fail
```

Need **≥ 5 paired takes** (same sitting: one Single, one Continuous) before ranking a hypothesis as confirmed.

---

## 7. Success criteria (after a future fix)

A fix is acceptable only if it addresses the **confirmed** hypothesis and meets:

| ID | Criterion |
|----|-----------|
| S1 | Same speaker, 1:3, 85%: Continuous auto-advances (or `ayah.result` pass) when Single would pass, for ≥ 4/5 paired takes |
| S2 | REST `/assess` scores on 1:3 do not regress |
| S3 | Do not pass 1:3 on character score when `الرحيم` was never spoken (no inventing via recovery) |
| S4 | Live UI may show unstable partials **while speaking**, but after a ≥ 800 ms pause the session must either **pass**, **fail with `ayah.result`**, or show a clear “still listening / incomplete” state — not a silent 50% stall |
| S5 | Existing coverage / confidence / Basmala recovery tests stay green (`pytest -q`, frontend audio/highlight tests) |

---

## 8. Decision matrix (do not skip to a random fix)

| If confirmed | Prefer | Avoid |
|--------------|--------|-------|
| **H1** incomplete window | Hold finalize until coverage **stable** across 2 ticks, or until silence **and** coverage ≥ T, or delay first probe until `audio_ms` ≥ typical 1:3 duration; optionally **debounce** live mismatch paint | Lowering 85%; lexicon-inventing `الرحيم` |
| **H2** busy freeze | Queue the latest snapshot instead of dropping; don’t let partials starve `run_assess` | Disabling all live feedback without measuring |
| **H3** resample/codec | Higher-quality resample in the worklet (sinc) **or** capture at 16 kHz and skip linear downsample; consider sending the same tap REST uses | Blindly turning denoise off as the product default |
| **H4** VAD tail | Pad 200–400 ms after last high-RMS frame into the STT window; or lower min speech for finals only | Disabling `pcm_has_speech` (CPU + hallucinations) |
| **H5** overlap | Clear overlap on **retry** / failed 2-word ayahs; keep overlap only on **pass-advance** | `OVERLAP_MS=0` globally (cuts next ayah’s first word — leftover-carry spec) |
| **H6** filter | Show `raw` in lab; do not keep garbage extras on short ayahs | Raising `STT_INVOCAB_FLOOR` to invent matches |
| **H7** UX only | Label live % “in progress”; don’t use red chips as a hard fail until `ayah.result` | Changing STT or thresholds |

**Likely first code change if H1+H7 (screenshot-shaped):** treat sub-threshold periodic decodes as **provisional** (don’t lock red `الرحيم`), and only **finalize** on silence/`force` with the **latest full buffer** — which is what Single does with Stop.

---

## 9. Tests (once a cause is chosen)

Today’s `MockSpeechRecognizer` cannot catch a real Moonshine windowing bug. Add **deterministic session tests** that fake time and transcripts:

| Test | Intent |
|------|--------|
| Periodic STT at t=0.6 s returns `الرحمن يضحين` (coverage 0.5) → **no** `ayah.result` | Documents current gate |
| Same session, later tick or `force` with `الرحمن الرحيم` → pass + advance | Lock the “complete window wins” behavior |
| `stt_busy=True` during a complete snapshot → next tick still assesses (if H2 fix) | No frozen 50% |
| After `ayah.result` fail + retry, overlap-only buffer does not STT as 1:3 | H5 |
| REST assess fixture unchanged for a clean `الرحمن الرحيم` transcript | No assessor regression |

Do **not** add a test that asserts `يضحين` must map to `الرحيم`.

---

## 10. Non-goals

- Tajweed scoring of `الرحيم`
- Quran-fine-tuned ASR (Phase 3)
- Shipped leftover-carry for pause-less tilawah (`realtime-stream-spec.md` §8.2)
- Changing stored Uthmani text or mapping dagger-alef globally
- Using this spec to re-open Basmala `بسم` recovery or 1:2 all-green-but-stuck advance (already specified)

---

## 11. Pointers

| Area | File |
|------|------|
| REST assess | `backend/app/api/memorization.py` |
| WS session / probe / assess | `backend/app/services/stream_session.py` |
| Ring buffer / VAD | `backend/app/services/stream_audio.py` |
| STT PCM vs file | `backend/app/services/speech_service.py` |
| Filter + recovery | `backend/app/services/stt_confidence.py` |
| Vue mode split | `frontend/src/App.vue` |
| Capture graph | `frontend/src/audio/capture-service.js` |
| Linear downsample | `frontend/public/pcm-worklet.js` |
| Heard / chips | `frontend/src/highlight.js` |

---

## 12. Shipped fixes (2026-08-14)

Investigation §5–§8 still applies for any remaining Single↔Continuous gap on 1:3. The following Continuous-only changes are **in tree** (REST / Single ayah untouched).

### 12.1 H1 + H7 — provisional mid-utterance + stable finalize

| Change | Where |
|--------|--------|
| Coverage probe requires `STREAM_COVERAGE_STABLE_TICKS` (default **2**) consecutive ticks ≥ `STREAM_COVERAGE_THRESHOLD` before `_assess_trigger` | `config.py`, `stream_session.run_periodic_stt` |
| `partial.alignment` includes `provisional: true` when coverage is still below the finalize gate | `stream_session._partial_events_from_recognized` |
| Live Continuous chips: mismatch/missing stay **pending** until `ayah.result` (no hard red on an incomplete window); header may show `N% in progress` | `frontend/src/highlight.js`, `App.vue` |
| Short silence no longer zeros the long-silence timer (`EnergyVadSegmenter.reset_short_pause` is a no-op on the silence counter) | `stream_audio.py` |
| Low-coverage short silence → `session.listening` (buffer kept); low-coverage **long** silence → abandon + clear buffer + clear live highlights | `stream_session.run_assess` / `_abandon_incomplete_attempt` |
| PCM continues to append while `ASSESSING` / `stt_busy`; queued assess / periodic tick uses the latest buffer | `on_audio_chunk`, `should_run_periodic_stt`, WS STT worker |
| Fail + `retry`: clear buffer with **0** overlap (no 300 ms glue onto the next try of a short ayah) | `run_assess` |

### 12.2 Regression — empty Score 0% / “detection dead” (post–§12.1)

**Symptom:** Continuous mic on, no live Heard; **Check now** or pause → `ayah.result` Score **0%**, Recognized empty, all words missing, `session.waiting` (“Retry the same ayah…”).

**Cause:**

1. Long-silence abandon cleared the ring buffer; leftover quiet PCM (or AGC-off / DTLN levels under `STREAM_VAD_RMS_THRESHOLD`) failed `pcm_has_speech`.
2. `reason=force` then **skipped STT** and scored `recognized=""` as a memorization fail.
3. Periodic STT used the same strict VAD RMS, so quiet speech never produced partials.

**Fix:**

| Change | Detail |
|--------|--------|
| `STREAM_STT_RMS_THRESHOLD` (default **0.008**) | Energy gate for **periodic / auto** STT only — lower than `STREAM_VAD_RMS_THRESHOLD` (0.015) |
| `ayah.force_assess` | Always runs STT when buffer ≥ `STREAM_MIN_UTTERANCE_MS` (no energy short-circuit to empty) |
| Empty Heard on force | Soft `error` `code=no_speech` + `session.listening` — **not** `ayah.result` / Score 0% |

**Tests:** `test_force_empty_filtered_is_no_speech_not_zero_score`, `test_force_quiet_pcm_still_runs_stt`, `test_periodic_stt_accepts_quiet_speech` in `backend/tests/test_stream.py`.

### 12.3 Still open

- Lab A/B (§6) to confirm remaining 1:3 wrong-second-word cases are windowing (H1) vs capture (H3) vs VAD tail (H4).
- Do **not** lower Accuracy *T* or invent `الرحيم` via recovery to hide a bad decode.
