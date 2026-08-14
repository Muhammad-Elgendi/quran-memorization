# Partials vs Completion Probe — Recitation Detection Evaluation Spec

**Status:** Draft (decision pending)  
**Phase:** 2.x  
**Companion:** `realtime-stream-spec.md` (protocol), `implementation-spec.md` (Phase 1 assess semantics)  
**Version:** 1.0  
**Last updated:** 2026-08-14

---

## 1. Purpose

Decide whether enabling **`partials: true`** on the WebSocket stream materially improves **ayah completion detection** and overall continuous-recitation UX — or whether the newer **completion probe** path already covers detection needs at lower cost.

This document is an **evaluation spec**, not an implementation mandate. It defines:

1. What partials do today vs what completion probe does
2. Measurable hypotheses and success criteria
3. A repeatable A/B test plan
4. CPU / latency budgets
5. A decision matrix and recommended default

---

## 2. Background — two overlapping mechanisms

As of 2026-08-14 the backend exposes **two independent periodic-STT paths** while the mic is open:

| Mechanism | Config gate | Interval | Audio window | Emits | Triggers `ayah.result`? |
|-----------|-------------|----------|--------------|-------|-------------------------|
| **Partials** | `session.start.partials: true` | `STREAM_PARTIAL_EVERY_MS` (2000 ms) | Last **~3 s** of buffer | `partial.transcript`, `partial.alignment` | **No** — UX only |
| **Completion probe** | `STREAM_COMPLETION_PROBE=true` (server default) | `STREAM_COMPLETION_PROBE_MS` (1500 ms) | **Full** attempt buffer | Internal `_assess_trigger` (`reason: coverage`) | **Yes** — when coverage ≥ `STREAM_COVERAGE_THRESHOLD` |

**Ayah-final triggers (independent of partials):**

| Trigger | Reason | When |
|---------|--------|------|
| Short silence VAD | `silence_short` | ≥400 ms silence + coverage ≥ 85% |
| Long silence VAD | `silence` | ≥2500 ms silence + coverage ≥ 85% |
| Manual | `force` | Client sends `ayah.force_assess` |

**Frontend today:** `partials: false` in `App.vue`; completion probe on by default server-side.

### 2.1 What partials were designed for (spec intent)

From `realtime-stream-spec.md` §3.2 / §6.2:

- **Partial latency target:** ~0.5–2 s provisional transcript + word highlights for the *current* ayah
- **Product goal:** Live feedback (`partial.alignment` → highlight words as user recites)
- **Strategy B hook:** Partial `progress` was intended to drive *early finalize* when coverage is high

### 2.2 What partials actually do in code (v1)

`StreamSession.run_partial()` (`backend/app/services/stream_session.py`):

1. Runs STT on the **last 3 seconds** of PCM only (not the full ayah attempt)
2. Emits transcript + alignment slice (max 40 ops) + a `progress` ratio
3. Does **not** call `run_assess()` or advance the session
4. Shares `stt_busy` with assess / completion probe — drops work when busy

**Implication:** Partials improve **live UX** but, as implemented, do **not** directly improve detection unless the client or server is later wired to act on `partial.alignment.progress`.

### 2.3 What completion probe does (v1.1)

`StreamSession.run_completion_probe()`:

1. Runs STT on the **full** rolling buffer (up to `STREAM_MAX_BUFFER_S`, 45 s)
2. Computes token coverage via `MemorizationAssessor.progress()` (suffix-aware)
3. If coverage ≥ 85%, triggers immediate `ayah.result` with `trigger: coverage`
4. Reuses transcript via `recognized_hint` to avoid double STT on finalize

**Implication:** Completion probe **directly** improves time-to-score after ayah finish; partials do not (unless extended).

---

## 3. Evaluation question

> **Does enabling `partials: true` make recitation detection better than the current default (partials off + completion probe on)?**

Sub-questions:

| ID | Question |
|----|----------|
| Q1 | Does detection latency improve (time from last spoken word → `ayah.result`)? |
| Q2 | Does detection accuracy improve (correct pass/fail vs human label)? |
| Q3 | Does false-finalize rate increase (score triggered too early on partial recitation)? |
| Q4 | Does CPU / memory / concurrent-session capacity degrade unacceptably? |
| Q5 | Does live word highlighting (`partial.alignment`) improve user correction behavior (qualitative)? |

---

## 4. Hypotheses

### H1 — Detection latency (partials do **not** help today)

**Claim:** With completion probe enabled, enabling partials does **not** reduce time-to-`ayah.result` because partials never call `run_assess()`.

**Rationale:** Both paths compete for `stt_busy`; partials may *delay* probes when they overlap.

**Falsify if:** Median `ayah.result` latency drops by ≥200 ms in the partials-on arm with probe still on.

### H2 — Detection accuracy (partials **may hurt** on long ayahs)

**Claim:** Partials STT only the last 3 s window → `partial.alignment.progress` **underestimates** coverage on long ayahs, so any future logic that finalizes on partial progress would increase false negatives.

**Rationale:** Completion probe uses the full buffer; partial window is intentionally capped for CPU.

**Falsify if:** Partials-on arm shows higher F1 on pass/fail vs labeled clips **without** adding finalize-on-partial logic.

### H3 — UX feedback (partials **do** help user behavior)

**Claim:** Live `partial.alignment` helps users self-correct before ayah end, indirectly improving final scores — even if detection timing is unchanged.

**Falsify if:** User testing shows no difference in correction rate or perceived helpfulness.

### H4 — CPU cost (partials **double** periodic STT load)

**Claim:** Partials-on + probe-on ≈ two STT passes every ~1.5–2 s while listening → CPU rises enough to drop concurrent sessions or cause `busy` skips.

**Measure:** `stt_ms` duty cycle, container CPU %, count of `error.busy` frames.

---

## 5. Metrics

### 5.1 Detection quality (primary)

| Metric | Definition | Source |
|--------|------------|--------|
| **TTFR** (time to final result) | `ayah.result.ts` − timestamp of last speech frame (VAD) | Instrumented session logs |
| **TTFR p50 / p95** | Per ayah, aggregated | Same |
| **Detection F1** | Pass/fail vs human-labeled ground truth | Labeled audio set |
| **False finalize rate** | `ayah.result` where coverage was <100% and user had not finished | Manual review |
| **Missed finalize rate** | User finished + 5 s silence but no `ayah.result` | Manual review |
| **Trigger mix** | % by `trigger`: `coverage`, `silence_short`, `silence`, `force` | `ayah.result.trigger` |

### 5.2 Operational (secondary)

| Metric | Definition | Budget (local MVP) |
|--------|------------|---------------------|
| **STT duty cycle** | Σ `stt_ms` / session wall time | ≤35% (partials off baseline TBD) |
| **Concurrent sessions** | Max stable sessions before timeout / busy storm | ≥2 (current limit) |
| **Probe skip rate** | `error.busy` / partial skips per minute | <5% of ticks |
| **Container CPU** | Docker stats during 5-min recitation | ≤80% of 1 core (dev laptop) |

### 5.3 UX (qualitative)

| Signal | Method |
|--------|--------|
| Live highlight usefulness | 5-user think-aloud (continuous mode, Al-Fatihah) |
| Confusion from stale partials | Observe whether transcript shrinks / jumps (Moonshine `stable: false`) |
| Preference | Forced choice: partials on vs off after paired trials |

---

## 6. Test matrix (A/B arms)

Run each arm with **identical** audio fixtures and live user sessions.

| Arm | `partials` | `STREAM_COMPLETION_PROBE` | `STREAM_PARTIAL_EVERY_MS` | `STREAM_COMPLETION_PROBE_MS` | Notes |
|-----|------------|---------------------------|---------------------------|------------------------------|-------|
| **A0 (current default)** | `false` | `true` | — | 1500 | Baseline |
| **A1 (partials UX only)** | `true` | `true` | 2000 | 1500 | Both periodic STT paths |
| **A2 (partials only)** | `true` | `false` | 1500 | — | Isolates partial contribution (expect worse TTFR) |
| **A3 (probe only, faster)** | `false` | `true` | — | 1000 | Tune probe without partials cost |
| **A4 (unified — future)** | `true`* | `true` | 1500 | 1500 | *Partials fed from probe STT result (no double STT) |

**Control variables (fixed across arms):**

- `STREAM_SHORT_SILENCE_MS=400`, `STREAM_SILENCE_MS=2500`
- `STREAM_COVERAGE_THRESHOLD=0.85`
- Moonshine Arabic Tiny, mono 16 kHz PCM
- Same surah/ayah ranges in scripted tests

---

## 7. Test procedures

### 7.1 Scripted audio (regression)

**Corpus:** 20 clips minimum — mix of:

- Complete ayah (Al-Fatihah 1:1–1:7 + one longer ayah)
- Complete ayah + 500 ms trailing silence
- Mid-ayah pause 1 s, then resume and finish
- Restart from beginning mid-ayah
- Continuous two ayahs without pause
- Quiet / low-RMS recitation (VAD stress)

**Procedure:**

1. Replay clip into `StreamSession` via binary PCM chunks (250 ms cadence) — reuse `backend/tests/test_stream.py` patterns
2. Log all events with timestamps
3. Compare TTFR and pass/fail vs label

**Pass criteria for “detection not worse”:** A1 TTFR p95 ≤ A0 TTFR p95 + 100 ms **and** F1 ≥ A0 F1 − 0.02.

### 7.2 Live recitation (integration)

1. `docker compose up --build` — two builds: env toggles for A0 and A1
2. Browser continuous mode; record screen + save WS traffic (DevTools)
3. 3 internal testers × 5 ayahs each
4. Collect `stt_ms` from `partial.transcript` and `ayah.result`

### 7.3 CPU bench

```bash
# Example: 5-minute session, sample CPU
docker stats quran-memorization-backend-1 --no-stream
```

Run with `STREAM_MAX_CONCURRENT_SESSIONS=2`, two parallel browser sessions.

---

## 8. Known architectural overlaps (evaluation must account)

### 8.1 Double STT when both enabled

```
Every ~1.5 s: completion probe → STT(full buffer)
Every ~2.0 s: partials         → STT(last 3 s)
```

Both gate on `stt_busy`. Overlap causes **skipped** probes or partials — detection latency may **increase**, not decrease.

**Recommendation for fair A1 test:** Either accept overlap as “realistic partials-on cost”, or implement **A4 unified path** first (single STT feeds both UX events and coverage check).

### 8.2 Partials progress ≠ probe coverage

| | Partials `progress` | Probe `coverage` |
|--|---------------------|------------------|
| **Formula** | `equal ops / len(alignment)` on assessor output | `matched expected tokens / len(expected)` best suffix |
| **Window** | Last 3 s audio | Full buffer |
| **Suffix restart** | Full assessor on truncated audio | Explicit suffix loop in `progress()` |

These can diverge on the same utterance → do not assume partial progress can replace probe coverage without unifying logic.

### 8.3 Partials do not trigger finalize (today)

Enabling partials **alone** cannot improve detection unless we add one of:

1. **Finalize-on-partial:** server calls `run_assess()` when `partial.alignment.progress ≥ threshold` (extends Strategy B)
2. **Client-driven:** UI sends `ayah.force_assess` when progress bar hits 100%
3. **Unified STT tick:** merge partial + probe into one periodic job (A4)

Any detection improvement from partials requires **additional implementation** beyond flipping `partials: true`.

---

## 9. Decision criteria

After running §7, apply:

| Outcome | Decision |
|---------|----------|
| A1 beats A0 on TTFR **and** F1 **and** CPU within budget | Enable partials by default; implement frontend live highlights |
| A1 improves UX qualitatively but not TTFR/F1 | Partials **opt-in** for UX; keep probe for detection |
| A1 increases TTFR or `busy` skips vs A0 | Keep partials off; tune A3 (faster probe) instead |
| Partials-only (A2) fails TTFR badly | Confirms probe is necessary; partials are not a substitute |
| CPU fails budget on A1 | Implement A4 (unified STT) before enabling partials |

### 9.1 Recommended default (pre-measurement)

**Implemented (2026-08-14):** A4 unified tick + live highlighting shipped.

| Setting | Value | Reason |
|---------|-------|--------|
| `partials` (client) | `true` | Live word highlights + unified STT tick |
| `STREAM_PARTIALS_DEFAULT` | `true` | Server default matches client |
| `STREAM_COMPLETION_PROBE` | `true` | Same STT pass drives coverage finalize |
| Frontend live highlights | Shipped | Consumes `partial.alignment` / `ayah.result` |

---

## 10. Implementation options (if evaluation favors partials)

Priority order:

| ID | Change | Detection impact | UX impact | Cost |
|----|--------|------------------|-----------|------|
| P1 | **Unified periodic tick (A4)** — one STT → emit `partial.*` + coverage check | High | High | Medium dev |
| P2 | **Full-buffer partials** — remove 3 s cap when partials on | Medium | Medium | Higher CPU |
| P3 | **Finalize on partial progress** — `progress ≥ 0.85` → `run_assess` | High | Low | Low dev; risk double-fire with probe |
| P4 | **Client progress bar** — show partials, user taps Check at 100% | Low | High | Frontend only |
| P5 | **Disable probe when partials finalize (P3)** | — | — | Avoid duplicate triggers |

**Avoid P3 without deduplication** — probe and partial-finalize could race.

---

## 11. Instrumentation checklist (before benchmarking)

- [ ] Log `ayah.result.trigger`, `stt_ms`, `audio_ms` server-side (already on result frames)
- [ ] Add debug field `coverage` on `ayah.result` when reason is `coverage` or `silence_short`
- [ ] Count `busy` errors per session in summary
- [ ] Frontend: optional WS trace download for lab sessions
- [ ] pytest benchmark: TTFR from synthetic PCM + MockSpeechRecognizer with deterministic transcripts

---

## 12. Acceptance for closing this spec

This evaluation is **complete** when:

1. Arms A0 and A1 (minimum) run on scripted corpus §7.1
2. TTFR p50/p95 and F1 recorded in a results table (append as §13)
3. CPU bench §7.3 recorded for A0 vs A1
4. Decision applied: update `App.vue` default, `STREAM_*` defaults, and `realtime-stream-spec.md` status line
5. If partials enabled: frontend handles `partial.alignment` OR server implements A4 unified tick

---

## 13. Results (fill after benchmark)

| Arm | TTFR p50 (ms) | TTFR p95 (ms) | F1 | False finalize % | STT duty % | CPU % | Notes |
|-----|---------------|---------------|-----|------------------|------------|-------|-------|
| A0 | — | — | — | — | — | — | baseline |
| A1 | — | — | — | — | — | — | partials on |
| A2 | — | — | — | — | — | — | optional |
| A3 | — | — | — | — | — | — | optional |

**Decision:** _TBD_

---

## 14. References

| Document / code | Relevance |
|-----------------|-----------|
| `specs/realtime-stream-spec.md` §8.1 Strategy B | Original partials + coverage intent |
| `backend/app/services/stream_session.py` | `run_partial`, `run_completion_probe`, `run_assess` |
| `backend/app/config.py` | `STREAM_PARTIAL_*`, `STREAM_COMPLETION_*` |
| `frontend/src/App.vue` | `partials: false` in `session.start` |
| `backend/tests/test_stream.py` | Session + WS integration patterns for §7.1 |

---

## 15. Open questions

1. Should partials use the **same** suffix-aware `progress()` as the probe (today they use a different progress formula)?
2. Is 3 s partial window acceptable for ayahs longer than ~8 s spoken duration?
3. Should K8s deployment disable partials on CPU-limited nodes via env?
4. When Phase 3 ASR arrives, does partial cadence need to drop to ≥3 s?
