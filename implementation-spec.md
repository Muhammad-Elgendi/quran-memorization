# Quran Memorization Assessor — Implementation Spec

**Status:** Ready for implementation  
**Source:** `first-spec.md`  
**Version:** 1.0  
**Last updated:** 2026-08-13

---

## 1. Purpose

Build a **local-first Quran memorization assessor**: the user selects an ayah (or range), recites into the microphone, and the system transcribes the audio, compares it to the canonical text, and reports score, pass/fail, and word-level mistakes.

The backend must expose a clean **REST API** so the same server can later serve a Flutter (or other) client without rewriting assessment logic.

---

## 2. Goals and non-goals

### 2.1 Goals (MVP — Phase 1)

| ID | Goal |
|----|------|
| G1 | Serve the full Uthmani Quran corpus (114 surahs, ayah-level) via REST |
| G2 | Accept browser-recorded audio and transcribe with Moonshine Arabic Tiny |
| G3 | Normalize Arabic for comparison (do **not** mutate the stored corpus) |
| G4 | Score recitation vs expected ayah; surface missing / extra / wrong words |
| G5 | Configurable pass threshold (default 85%) |
| G6 | Audible warning tone in the UI when assessment fails |
| G7 | Vue.js web client for selection, recording, and results |
| G8 | One-command-ish install: deps + corpus download + model ready |

### 2.2 Non-goals (MVP)

- Real-time / streaming word-by-word feedback (Phase 2)
- Tajweed scoring (madd, ghunnah, etc.)
- User accounts, cloud sync, or progress persistence (Phase 2+)
- Fine-tuned Quran-specific ASR (Phase 3)
- Mobile/Flutter client (consumes same API later; not built in Phase 1)
- Scraping Quran text from arbitrary websites

### 2.3 Design principles

1. **Never compare raw Quran strings directly** — always go through a normalization layer used only for comparison.
2. **STT behind an interface** — `SpeechRecognizer.transcribe(path) -> str` so Moonshine can be swapped later.
3. **Corpus is immutable** — display/store Uthmani text as-is; normalize only copies used for matching.
4. **Backend owns assessment** — clients send audio + target ayah; clients do not implement Arabic logic.
5. **Verify religious text** before any public/production release against a trusted source (e.g. Tanzil-derived).

---

## 3. System architecture

```text
Browser (Vue.js)
       │
       │ REST (HTTP multipart for audio)
       ▼
┌─────────────────────────────────────┐
│           FastAPI Backend           │
│                                     │
│  Quran API                          │
│  ├── GET surahs / surah / ayah      │
│                                     │
│  Memorization API                   │
│  └── POST assess (audio + target)   │
│                                     │
│  Services                           │
│  ├── QuranService                   │
│  ├── SpeechRecognizer (Moonshine)   │
│  ├── ArabicNormalizer               │
│  └── MemorizationAssessor           │
└──────────────────┬──────────────────┘
                   │
                   ▼
            data/quran.json
```

**Phase 1 transport:** REST only. WebSocket is reserved for Phase 2 streaming.

**Phase 1 comparison flow:**

```text
Canonical ayah text ──┐
                      ├──► Normalizer ──► Alignment + Score ──► AssessmentResult
Moonshine transcript ─┘
```

---

## 4. Repository layout

```text
quran-memorization/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app, CORS, routers, /health
│   │   ├── config.py               # pydantic-settings
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── quran.py
│   │   │   └── memorization.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   └── schemas.py
│   │   └── services/
│   │       ├── __init__.py
│   │       ├── quran_service.py
│   │       ├── speech_service.py
│   │       ├── normalizer.py
│   │       └── assessor.py
│   ├── data/
│   │   └── quran.json              # generated; not hand-edited
│   ├── download_quran.py
│   ├── requirements.txt
│   └── setup.py                    # optional packaging
├── frontend/
│   ├── src/
│   │   ├── App.vue
│   │   ├── main.js
│   │   ├── api.js
│   │   └── assets/                # as needed
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── install.py
├── README.md
├── first-spec.md                   # source notes
└── implementation-spec.md          # this file
```

---

## 5. Configuration

**File:** `backend/app/config.py`

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `APP_NAME` | str | `"Quran Memorization Assistant"` | API title |
| `HOST` | str | `"0.0.0.0"` | Bind host |
| `PORT` | int | `8000` | Bind port |
| `QURAN_PATH` | str | `"data/quran.json"` | Relative to backend root |
| `DEFAULT_THRESHOLD` | float | `0.85` | Pass if score ≥ this |
| `WORD_MATCH_THRESHOLD` | float | `0.75` | Word considered wrong below this |
| `MIN_AUDIO_SECONDS` | float | `0.5` | Reject shorter uploads |
| `MAX_AUDIO_SECONDS` | float | `20.0` | Reject longer uploads (MVP single-ayah) |
| `MOONSHINE_MODEL` | str | `"UsefulSensors/moonshine-tiny-ar"` | HF model id |
| `CORS_ORIGINS` | list[str] | `["*"]` | Tighten for production |

Load from `.env` via `pydantic-settings`. Resolve `QURAN_FILE` as an absolute path from the backend package root.

---

## 6. Data model

### 6.1 On-disk Quran corpus (`data/quran.json`)

Array of surah objects, sorted by `number`:

```json
[
  {
    "number": 1,
    "name": "الفاتحة",
    "english_name": "Al-Fatihah",
    "ayahs": [
      { "number": 1, "text": "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ" }
    ]
  }
]
```

| Field | Required | Notes |
|-------|----------|-------|
| `surah.number` | yes | 1–114 |
| `surah.name` | yes | Arabic name; may be empty until enriched |
| `surah.english_name` | no | Optional metadata |
| `ayah.number` | yes | Within-surah ayah number |
| `ayah.text` | yes | Uthmani Arabic; **never mutated by normalizer** |

**Source for download script:** Hugging Face `arbml/quran_uthmani` (`sorah`, `ayah`, `sentence`).

**Mandatory before production:** verify corpus against a trusted Uthmani source (e.g. Tanzil / `nuqayah/quran-text`). Document verification date and method in README.

**Enrichment (recommended in Phase 1):** after download, merge surah Arabic/English names from a small static map or secondary dataset so the UI does not show blank names.

### 6.2 Assessment result (internal + API)

| Field | Type | Description |
|-------|------|-------------|
| `score` | float 0–1 | Overall similarity after normalization |
| `passed` | bool | `score >= threshold` |
| `warning` | bool | `not passed` (explicit for clients) |
| `expected` | str | Canonical ayah text (unnormalized) |
| `recognized` | str | Raw STT output (unnormalized) |
| `missing_words` | list[str] | Expected tokens with no aligned recognition |
| `extra_words` | list[str] | Recognized tokens with no expected counterpart |
| `wrong_words` | list[{expected, recognized, similarity}] | Aligned pairs below word threshold |
| `message` | str | Short human-readable summary |
| `alignment` | list[AlignmentOp] | Optional Phase 1.1; required Phase 1.1+ |

---

## 7. Arabic normalization

**File:** `backend/app/services/normalizer.py`

### 7.1 Responsibilities

Produce a **comparison-only** string/token list. Must not write back to `quran.json`.

### 7.2 Steps (in order)

1. Empty → `""`
2. Unicode `NFKC`
3. Remove tashkeel: `\u0610-\u061A`, `\u064B-\u065F`, `\u0670`, `\u06D6-\u06ED`
4. Remove Quranic annotation marks (same / overlapping ranges as needed)
5. Remove tatweel `ـ`
6. Normalize alef forms: `ٱ أ إ آ` → `ا`
7. Normalize alef maqsura: `ى` → `ي`
8. Optionally normalize taa marbuta: `ة` → `ه` (document choice; apply consistently to both sides)
9. Replace punctuation / markers (`، ؛ ؟ . ! ? ( ) [ ] { } < > " ' ۞ ۩` etc.) with space
10. Collapse whitespace; strip

### 7.3 API

```text
normalize_arabic(text: str) -> str
tokenize(text: str) -> list[str]   # normalize then split on whitespace
```

### 7.4 Acceptance

- Same ayah with/without diacritics → identical tokens after normalize.
- Corpus display path always returns original `text`.

---

## 8. Quran service

**File:** `backend/app/services/quran_service.py`

| Method | Behavior |
|--------|----------|
| `get_surahs()` | `[{number, name, english_name, ayah_count}, ...]` |
| `get_surah(n)` | Full surah dict or `None` |
| `get_ayah(surah, ayah)` | `{number, text}` or `None` |
| `get_range(surah, start, end)` | List of ayahs inclusive; empty if surah missing |

Load JSON once at construction. Fail fast at startup if file missing (clear error telling user to run `download_quran.py`).

---

## 9. Speech recognition

**File:** `backend/app/services/speech_service.py`

### 9.1 Interface

```text
SpeechRecognizer (ABC)
  transcribe(audio_path: str) -> str
```

### 9.2 Implementation: `MoonshineArabicRecognizer`

- Lazy-load model on first `transcribe` (server starts without blocking on ~112 MB download/load).
- Model: `UsefulSensors/moonshine-tiny-ar` via Transformers `AutoProcessor` + `AutoModelForSpeechSeq2Seq`.
- Audio: mono, 16 kHz (`librosa.load`).
- Inference: `model.generate` under `torch.no_grad()`; decode with `skip_special_tokens=True`.
- Return stripped Arabic string.

### 9.3 Audio constraints (API layer)

- Accept `webm`, `wav`, `ogg`, `mp3` (browser MVP uses `audio/webm`).
- Enforce `MIN_AUDIO_SECONDS` / `MAX_AUDIO_SECONDS` after decode; return `400` if out of range.
- Always delete temp files in `finally`.

### 9.4 Failure modes

| Case | HTTP | Detail |
|------|------|--------|
| Unreadable audio | 400 | Invalid audio file |
| Model load failure | 503 | Speech model unavailable |
| Empty transcript | 200 with low score / message | Treat as failed assessment, not 500 |

---

## 10. Memorization assessor

**File:** `backend/app/services/assessor.py`

### 10.1 MVP scoring (must ship)

1. Normalize expected and recognized.
2. Overall `score = rapidfuzz.fuzz.ratio(expected_norm, recognized_norm) / 100`.
3. Word lists via `tokenize`.
4. **Alignment (required improvement over naive index zip):** use sequence alignment (e.g. difflib `SequenceMatcher`, or Needleman–Wunsch / rapidfuzz process) so insertions/deletions do not shift all subsequent words.
5. Classify ops:
   - delete → `missing_words`
   - insert → `extra_words`
   - replace with similarity `< WORD_MATCH_THRESHOLD` → `wrong_words`
   - equal / high similarity → correct
6. `passed = score >= threshold`
7. Messages:
   - pass: e.g. “Excellent. Your recitation closely matches the selected ayah.”
   - fail: e.g. “There may be a memorization error. Please review the highlighted words.”

### 10.2 Example expectations

| Expected (conceptually) | Recognized | Result |
|-------------------------|------------|--------|
| والله غفور رحيم | والله غفور رحيم | pass; no wrong words (diacritics ignored) |
| والله غفور رحيم | والله غفور عليم | fail; one wrong: رحيم → عليم |
| full ayah | truncated | missing words at end; lower score |

### 10.3 Thresholds

- Default overall: `0.85` (client may override per request).
- Word mismatch: `0.75`.
- Clamp request threshold to `[0.5, 1.0]`.

---

## 11. REST API

Base URL (dev): `http://localhost:8000`  
OpenAPI: `/docs`

### 11.1 Health

```http
GET /health
```

```json
{ "status": "ok", "service": "Quran Memorization Assistant" }
```

### 11.2 Quran

```http
GET /api/quran/surahs
→ 200 [{ "number", "name", "english_name", "ayah_count" }, ...]

GET /api/quran/surahs/{surah_number}
→ 200 full surah | 404

GET /api/quran/surahs/{surah_number}/ayahs/{ayah_number}
→ 200 { "number", "text" } | 404
```

### 11.3 Memorization assess

```http
POST /api/memorization/assess
Content-Type: multipart/form-data

surah: int (required)
ayah: int (required)
threshold: float (optional, default 0.85)
audio: file (required)
```

**Success 200:**

```json
{
  "score": 0.91,
  "passed": true,
  "warning": false,
  "expected": "…",
  "recognized": "…",
  "missing_words": [],
  "extra_words": [],
  "wrong_words": [],
  "message": "Excellent. Your recitation closely matches the selected ayah."
}
```

| Status | When |
|--------|------|
| 404 | Ayah does not exist |
| 400 | Missing/invalid audio, duration out of range, bad threshold |
| 503 | STT model unavailable |

### 11.4 CORS

Enable CORS for local Vite (`http://localhost:5173`) and configurable origins. Phase 1 may use `*` for local development.

### 11.5 Future endpoints (document only; do not implement in Phase 1)

```text
POST /api/memorization/assess-range   # multi-ayah
WS   /api/memorization/stream         # chunked live feedback
GET  /api/sessions/{id}
GET  /api/progress
```

---

## 12. FastAPI application

**File:** `backend/app/main.py`

- Construct `QuranService` once; inject into routers via factory `create_router(quran_service)`.
- Include Quran + Memorization routers.
- CORS middleware as configured.
- Version `1.0.0`; description states API is client-agnostic (Vue now, Flutter later).

Run:

```bash
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 13. Corpus download script

**File:** `backend/download_quran.py`

1. `load_dataset("arbml/quran_uthmani", split="train")`
2. Group by `sorah` → ayahs with `number` + `text` from `sentence`
3. Sort surahs and ayahs
4. Write `data/quran.json` with `ensure_ascii=False`, indent 2
5. Print counts (expect 114 surahs; ~6236 ayahs)

Idempotent: safe to re-run (overwrites file).

Optional Phase 1 follow-up: attach official surah names from a verified static JSON.

---

## 14. Frontend (Vue 3 + Vite)

### 14.1 Setup

```bash
npm create vite@latest frontend -- --template vue
cd frontend && npm install && npm install axios
```

`frontend/src/api.js`: axios instance with `baseURL` from env (`VITE_API_BASE_URL`, default `http://localhost:8000`).

`vite.config.js`: proxy `/api` and `/health` to backend if preferred over CORS.

### 14.2 UI behavior (single view)

| Control | Behavior |
|---------|----------|
| Surah select | Loaded from `GET /api/quran/surahs` on mount |
| Ayah select | `1 … currentSurah.ayah_count`; reset when surah changes |
| Threshold slider | 0.50–1.00 step 0.01; show percent label |
| Start Recitation | `getUserMedia` → `MediaRecorder`; clear prior result |
| Stop | Stop tracks; build `audio/webm` blob; `POST /assess` |
| Loading | “Analyzing your recitation…”; disable start while loading |
| Result | Score %, pass/fail styling, recognized + expected (RTL), wrong-word list |
| Warning | If `warning` / `!passed`, play short oscillator beep (~660 Hz, ~0.3 s) |

### 14.3 UX rules

- Arabic text: `dir="rtl"`, large readable font.
- Do not show normalized text as “Expected”; show corpus text.
- Handle mic permission denial with a visible error message.
- Disable assess if no surah selected.

### 14.4 Out of scope for Phase 1 UI

- Multi-ayah range picker (API may expose `get_range` later)
- Live streaming highlights
- Progress history

---

## 15. Dependencies

### 15.1 Backend `requirements.txt`

```text
fastapi
uvicorn[standard]
python-multipart
pydantic
pydantic-settings
numpy
scipy
soundfile
librosa
jiwer
rapidfuzz
huggingface_hub
transformers
torch
torchaudio
moonshine-voice
datasets
requests
```

**Note:** Prefer Moonshine’s documented runtime when stable; Transformers path is the specified Phase 1 path for `UsefulSensors/moonshine-tiny-ar`. Keep `torch` required for that path.

### 15.2 Frontend

- `vue` (Vite template)
- `axios`

---

## 16. Installation and runbook

### 16.1 `install.py` (repo root)

1. Upgrade pip
2. `pip install -r backend/requirements.txt`
3. Run `backend/download_quran.py`
4. Optionally prefetch model via `huggingface_hub` download of `UsefulSensors/moonshine-tiny-ar` (first `from_pretrained` also downloads)
5. Print how to start backend and frontend

### 16.2 Developer run

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python install.py

# terminal 1
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# terminal 2
cd frontend && npm install && npm run dev
```

- App: `http://localhost:5173`
- API docs: `http://localhost:8000/docs`

### 16.3 README must include

- Purpose and local-first nature
- Python / Node version expectations
- Mic permission note
- Corpus provenance + verification disclaimer
- How Flutter would call the same endpoints later

---

## 17. Phased delivery

### Phase 1 — MVP (implement now)

- [ ] Project skeleton as in §4
- [ ] Config, normalizer, Quran service, download script
- [ ] Moonshine recognizer behind ABC
- [ ] Assessor with **sequence alignment** (not naive zip-by-index)
- [ ] Quran + assess REST endpoints + health
- [ ] Vue client: select, record, upload, results, warning tone
- [ ] `install.py` + README
- [ ] Basic tests (§18)

### Phase 1.1 — Assessment quality

- [ ] Return per-token alignment for UI highlighting
- [ ] Optional `ة`/`ه` and other orthography toggles
- [ ] Range assess: concatenate ayahs in range, single recording
- [ ] Surah name enrichment from verified metadata

### Phase 2 — Real-time trainer

- [ ] WebSocket (or chunked upload) streaming STT
- [ ] Incremental alignment; immediate tone + highlight on mismatch
- [ ] Session + progress persistence

### Phase 3 — Quran-tuned ASR

- [ ] Evaluate / fine-tune on Quran-Ayah-Corpus (or similar)
- [ ] Optional post-STT “Quran lexicon correction” constrained to expected ayah vocabulary
- [ ] Swap recognizer implementation without API changes

---

## 18. Testing requirements

### 18.1 Unit

| Area | Cases |
|------|--------|
| Normalizer | Diacritics stripped; alef variants; empty; punctuation |
| Assessor | Exact match; wrong word; missing tail; extra words; threshold boundary |
| QuranService | Valid/invalid surah/ayah; range bounds |

### 18.2 API (TestClient)

- List surahs non-empty after fixture corpus
- Assess with tiny fixture WAV + mocked `SpeechRecognizer`
- 404 for unknown ayah
- 400 for empty file

### 18.3 Manual checklist

1. Load UI; surah list appears
2. Select Al-Fatihah ayah 1; record; receive score
3. Fail case triggers red state + beep
4. `/docs` shows all routes
5. Fresh clone: `install.py` produces working `quran.json`

---

## 19. Security and operations (MVP)

- No auth in Phase 1 (local use only).
- Limit upload size (e.g. 5 MB) in addition to duration.
- Do not log full audio; temp files deleted after request.
- CORS `*` is for local only; document locking origins before any network exposure.
- Model and corpus downloaded to local disk; no cloud assessment required.

---

## 20. Acceptance criteria (Phase 1 done when)

1. Backend starts and `/health` returns ok with corpus loaded.
2. All three Quran GET endpoints return correct data for surah 1 and a mid-Quran sample (e.g. 36:1).
3. Recording from the Vue app produces an assessment JSON with score, pass/fail, and word diagnostics.
4. Diacritic-only differences do not cause false failure at default threshold for a clean reading (manual spot-check).
5. Clear substitution (e.g. رحيم vs عليم style case on a short phrase) surfaces in `wrong_words`.
6. Failed assessment plays a warning tone once.
7. STT is only referenced through `SpeechRecognizer`; no Moonshine imports in API routers beyond the service module.
8. `install.py` documents and performs dependency + corpus setup successfully on a clean venv.

---

## 21. Open decisions (resolve during implementation if needed)

| Topic | Default for Phase 1 | Alternative |
|-------|---------------------|-------------|
| STT runtime | Transformers HF model | `moonshine-voice` package API if equivalent quality |
| Alignment algo | `difflib.SequenceMatcher` on tokens | Needleman–Wunsch / `jiwer` alignment |
| Taa marbuta | Leave as-is after other norms | Map `ة` → `ه` |
| Surah names | Fill from static map post-download | Leave empty until enrichment |
| Audio decode | librosa for all formats | Convert webm via ffmpeg if librosa fails |

---

## 22. Flutter readiness (contract only)

Clients need only:

```text
GET  /api/quran/surahs
GET  /api/quran/surahs/{id}
GET  /api/quran/surahs/{id}/ayahs/{ayahId}
POST /api/memorization/assess
GET  /health
```

No knowledge of Python, Transformers, Moonshine, or corpus format is required on the client.

---

## 23. Reference links

- Moonshine: https://github.com/moonshine-ai/moonshine
- Model: https://huggingface.co/UsefulSensors/moonshine-tiny-ar
- Corpus: https://huggingface.co/datasets/arbml/quran_uthmani
- Alt corpus: https://huggingface.co/datasets/quranlab/quran
- Trusted text reference: https://github.com/nuqayah/quran-text
- Future ASR data: https://huggingface.co/datasets/rabah2026/Quran-Ayah-Corpus

---

*This document is the authoritative implementation guide for Phase 1. Prefer this file over `first-spec.md` when the two differ; notable upgrades include mandatory sequence alignment and explicit acceptance/testing criteria.*
