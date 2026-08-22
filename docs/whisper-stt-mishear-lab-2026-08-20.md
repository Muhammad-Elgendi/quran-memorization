# Lab note — Whisper mishear on first try (2026-08-20)

**Symptom:** Correct Quran recitation was often scored wrong on the first attempt (wrong or empty Heard). A second try frequently passed with the same ayah and mic.

**Status:** Fixed and user-confirmed. Defaults: `STT_DECODER_PROB_GAMMA=1.0`, periodic STT fingerprint reuse, ayah near-miss recovery.

---

## Error (what users saw)

1. Single (REST) or Continuous (WS): recite Fatihah / short ayah correctly.
2. Heard showed a wrong Arabic token or dropped a real word (`فَأَكْفُرُ`-style junk, missing `بسم` / `الم`, etc.).
3. Score fail / no advance. Re-recording the same ayah often succeeded.

This looked like “STT is flaky,” but logs showed three compounding bugs after the Moonshine → Whisper switch.

---

## Root causes (runtime evidence)

### 1. Moonshine gamma left on Whisper (primary)

`STT_DECODER_PROB_GAMMA` was still **0.12** (Moonshine Tiny lab 2026-08-14). Whisper decoder softmax is better calibrated; raising raw ~0.57 junk with `p ** 0.12` produced calibrated ~0.94, so Accuracy *T* (0.85) **kept** hallucinations as Heard.

Setting gamma to **1.0** (identity) dumped junk correctly. Side effect: some correct but soft tokens (~0.40) were also dumped via `low_sequence` / keep floor — recovery had to cover the near-miss cases below.

### 2. Frozen PCM re-decoded in a loop (Continuous)

Periodic STT kept calling Whisper on an **unchanged** ring-buffer snapshot (paused / trailing silence). The same ~3 s PCM was transcribed ~20×, locking a wrong Heard and burning CPU.

### 3. Exact-only ayah recovery missed Whisper near-misses

After gamma=1.0, Whisper often emitted edit-distance-1 tokens for the expected word (`اسم` / `إسم` for `بسم`, `الر` for `الم`). Exact in-vocab revive missed them; fuzzy revive at ≥0.90 correctly rejected long hallucinations (`الرسم` ↛ `الم`) but did not help short near-misses.

---

## Fix (what shipped)

| Area | Change |
|------|--------|
| Config | `STT_DECODER_PROB_GAMMA = 1.0` in `backend/app/config.py` and `.env.example` |
| Continuous | `stream_session.run_periodic_stt`: fingerprint `(n_samples, peak, energy)`; if unchanged and `_last_stt` exists → reuse text, still run coverage probe; clear fingerprint on decode-state reset |
| Recovery | `stt_confidence.apply_ayah_recovery` / `recover_against_ayah`: no early return on `low_sequence`; exact revive below invocab floor when normalized token ∈ expected; **near-miss** (Levenshtein ≤ 1, min len ≥ 2) rewrite onto expected; near-miss skip-ahead only for short windows (≤2); fuzzy revive still ≥0.90 + invocab floor; rewrite already-kept near-misses |

**Do not invent** tokens that never appeared in the decode except agglutination split and near-miss rewrite of an emitted surface onto the matched expected token.

---

## Verification

- Unit: `test_speech_confidence.py` (gamma identity), `test_lexicon_recovery.py` (near-miss revive / hallucination safety), `test_stream.py` (`test_session_unified_periodic_single_stt` → one recognizer call on frozen audio).
- Manual: user confirmed first-try mishear resolved (2026-08-20).

---

## Pointers

| File | Role |
|------|------|
| `backend/app/config.py` | Default gamma |
| `backend/app/services/stt_confidence.py` | Calibration + recovery |
| `backend/app/services/stream_session.py` | Periodic reuse |
| `specs/whisper-tiny-ar-quran-switch-spec.md` | L5 gamma lab |
| `specs/uthmani-tanzeel-word-matching-spec.md` | Ayah recovery contract |
| `docs/agent-context.md` §13 | Incident table row |
