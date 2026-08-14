# Multi-utterance credit — implementation notes

**Date:** 2026-08-15  
**Spec:** [`specs/multi-utterance-credit-spec.md`](../specs/multi-utterance-credit-spec.md) (authoritative product behavior)  
**Scope:** Continuous (WebSocket) only — REST Single unchanged

---

## Problem fixed

In Continuous mode, each silence-finalized STT window was scored **alone** against the full ayah. Reciting an ayah across breaths (e.g. 2:3 prefix, then suffix) left live coverage at the **current-window** fraction (~38%) and never passed or advanced.

---

## What we shipped

### Backend

| Area | Change |
|------|--------|
| `backend/app/services/assessor.py` | Pure `merge_credit()` + `CreditMergeResult`; hypotheses **H-full**, **H-suffix**, **H-resume**; contiguous-prefix only; `alignment_from_credit()`; `credit_complete_assessment()` (Option A synthetic score) |
| | Strip leading **credited-prefix re-say** before align so e.g. `الرحمن` cannot fuzzy-credit `الرحيم` |
| `backend/app/services/stream_session.py` | Per-ayah `credit_mask` / cursor / utterance count; reset on ayah enter and `session.advance` |
| | Commit merge in `run_assess`; tentative merge for partials + coverage probe |
| | Finalize / pass when **cursor == N** (not window-only ≥ 0.85) |
| | Long silence: **clean partial** → keep credit, clear buffer, `session.listening` (no fail/tone); **mismatch** → fail + fail_policy |
| | Short silence: commit credit, keep buffer, no fail |
| | Empty abandon: retain credit; `partial.alignment` + `session.listening` carry `credit_cursor` |
| `backend/app/config.py` | `STREAM_MULTI_UTTERANCE_CREDIT` (default `true`), `STREAM_CREDIT_KEEP_ON_FAIL` (default `true`), `STREAM_CREDIT_CLEAR_ON_FAIL`, `STREAM_CREDIT_REQUIRE_CONTIGUOUS` |

### Protocol (additive)

- `partial.alignment`: `progress` = **cumulative**; optional `window_coverage`, `credit_cursor`, `credit_total`
- `ayah.result`: `credit_complete`, `credit_utterances`, `credit_cursor`, `credit_total`, `window_coverage`
- `session.listening`: optional `credit_cursor` / `credit_total` / `progress` when credit retained

### Frontend

| Area | Change |
|------|--------|
| `frontend/src/App.vue` | Live % uses cumulative `progress`; on `session.listening` with `cleared` + retained credit, clear Heard only — keep credited chips |

### Tests

| File | Coverage |
|------|----------|
| `backend/tests/test_credit.py` | Unit C1–C7 (`merge_credit`) |
| `backend/tests/test_stream.py` | Integration S1–S8 (split ayah pass-advance, empty retain, wrong fail+keep credit, legacy off, etc.) |

### Docs cross-links

- `docs/agent-context.md` — Continuous items 11–14
- `specs/realtime-stream-spec.md` §8 — cumulative probe / partial vs fail
- `specs/continuous-mistake-tone-spec.md` §7.1.1 — fail narrowed to committed mismatch, not every below-coverage Heard

---

## Behavior summary

```text
Utterance 1: match tokens 0..k-1  → credit_cursor = k, keep listening
Utterance 2: resume at k … N-1    → credit_cursor = N
             → ayah.result passed=true, credit_complete=true
             → session.advance (credit reset for next ayah)
```

- **Must not skip:** mid-ayah suffix with empty prefix credit does not advance.
- **Must not fail** a clean partial pause; **must fail** wrong continuation at the cursor (tone path unchanged).
- Master switch off (`STREAM_MULTI_UTTERANCE_CREDIT=false`) restores window-only legacy behavior.

---

## Non-goals (unchanged)

- REST `/assess` credit state
- Tajweed / timing credit
- Credit across reload or new WebSocket session
- Leftover carry into the **next** ayah
- Sparse (non-contiguous) credit masks
