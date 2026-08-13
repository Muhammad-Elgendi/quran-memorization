# Agent / session context — Quran Memorization Assessor

**Purpose:** Persist implementation knowledge from the initial build session so later prompts start with correct architecture, runbooks, and known fixes.

**Authoritative product specs:**
- Phase 1 REST: `specs/implementation-spec.md`
- Phase 2 stream: `specs/realtime-stream-spec.md`

**Last updated:** 2026-08-13 (Phase 2 WebSocket continuous stream)

---

## 1. What was built

### Phase 1 MVP (REST)

Local-first Quran memorization assessor:

1. User selects surah + ayah in a Vue UI.
2. Records audio in the browser (`MediaRecorder` → `audio/webm`).
3. Backend transcribes with **Moonshine Arabic Tiny** (`UsefulSensors/moonshine-tiny-ar` via Hugging Face Transformers).
4. Arabic **normalization** (comparison only) + **sequence-aligned** scoring vs canonical Uthmani text.
5. Returns score, pass/fail, missing/extra/wrong words; UI plays a ~660 Hz warning tone on fail.

### Phase 2 (WebSocket continuous)

1. `WS /api/memorization/stream` — long-lived session with JSON control + binary PCM.
2. Client streams **pcm_s16le @ 16 kHz mono** via AudioWorklet (no Opus encode on the hot path).
3. Server uses energy VAD + silence (≥800 ms) to finalize utterances; `ayah.force_assess` / skip escape hatches.
4. Reuses `SpeechRecognizer.transcribe_audio`, `MemorizationAssessor`, `QuranService`.
5. Auto-advance on pass; UI fail policies: **continue** | **stop** (server also supports `retry`).
6. **Partials default OFF** (`STREAM_PARTIALS_DEFAULT=false`) to keep CPU light; when enabled, gated + 2 s cadence + last-3 s window.
7. Vue mode toggle: Single ayah (REST) vs Continuous (WS).

**Still out of scope:** tajweed, accounts/progress DB, Quran-fine-tuned ASR, leftover-carry for pause-less tilawah (v1.1), multi-replica sticky sessions.

---

## 2. Repository layout

```text
quran-memorization/
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI, CORS, routers, /health
│   │   ├── config.py                  # pydantic-settings (+ STREAM_* knobs)
│   │   ├── api/quran.py               # GET surahs / surah / ayah
│   │   ├── api/memorization.py        # POST assess (multipart)
│   │   ├── api/memorization_stream.py # WS /api/memorization/stream
│   │   ├── models/schemas.py
│   │   └── services/
│   │       ├── quran_service.py
│   │       ├── speech_service.py      # SpeechRecognizer + transcribe_audio
│   │       ├── audio.py               # ffmpeg→WAV for REST uploads
│   │       ├── stream_audio.py        # PCM ring + energy VAD
│   │       ├── stream_session.py      # session state machine
│   │       ├── normalizer.py
│   │       └── assessor.py
│   ├── data/quran.json
│   ├── download_quran.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── tests/                         # test_core.py + test_stream.py
├── frontend/                          # Vue 3 + Vite + axios
│   ├── public/pcm-worklet.js          # 16 kHz PCM capture worklet
│   ├── src/stream.js                  # WS URL + PCM capture helper
│   ├── src/App.vue                    # single + continuous modes
│   ├── Dockerfile
│   └── nginx.conf                     # /api proxy + WebSocket upgrade
├── specs/realtime-stream-spec.md
├── specs/implementation-spec.md
├── docs/agent-context.md
├── k8s/deploy.yaml                    # WS-friendly ingress timeouts
└── …
```

---

## 3. Design principles (do not violate)

| # | Principle |
|---|-----------|
| 1 | Never compare raw Quran strings; always normalize copies only |
| 2 | Never write normalized text back to `quran.json` |
| 3 | STT behind `SpeechRecognizer` (`transcribe` / `transcribe_audio`) — no model imports in routers |
| 4 | Backend owns assessment; clients are thin |
| 5 | Phase 1 REST `/assess` and Phase 2 WS `/stream` coexist |
| 6 | Religious text must be verified vs trusted Uthmani (e.g. Tanzil / nuqayah/quran-text) before production |
| 7 | Stream path prefers PCM @ 16 kHz; partials off by default for CPU |

---

## 4. API contract (Flutter-ready)

| Method | Path | Notes |
|--------|------|-------|
| GET | `/health` | `{ status, service }` |
| GET | `/api/quran/surahs` | `{ number, name, english_name, ayah_count }[]` |
| GET | `/api/quran/surahs/{id}` | Full surah + `ayahs[{number,text}]` |
| GET | `/api/quran/surahs/{id}/ayahs/{ayahId}` | Single ayah |
| POST | `/api/memorization/assess` | multipart: `surah`, `ayah`, `threshold?`, `audio` |
| WS | `/api/memorization/stream` | continuous session; see `specs/realtime-stream-spec.md` |

Assess success JSON includes: `score`, `passed`, `warning`, `expected`, `recognized`, `missing_words`, `extra_words`, `wrong_words`, `message`, `alignment`.

Stream: client sends `session.start` then binary PCM chunks; server emits `session.ready`, `ayah.result`, `session.advance`, `session.summary`. Partials default **false**.

Defaults: overall threshold **0.85**, word mismatch **0.75**, audio **0.5–20s** (REST), max upload **5MB**.

---

## 5. Arabic normalization order

Implemented in `backend/app/services/normalizer.py`:

1. Empty → `""`
2. Unicode NFKC
3. Strip tashkeel / Quranic marks
4. Remove tatweel
5. Normalize alef forms (`ٱأإآ` → `ا`)
6. `ى` → `ي`
7. Phase 1: **leave `ة` as-is** (do not map to `ه`)
8. Punctuation → space; collapse whitespace

`tokenize` = normalize then split.

---

## 6. Assessor behavior

- Overall `score = rapidfuzz.fuzz.ratio(norm_expected, norm_recognized) / 100`
- Token alignment via `difflib.SequenceMatcher` (insert/delete/replace)
- Replace pairs below word threshold → `wrong_words`
- `passed = score >= threshold`; `warning = not passed`
- Short phrases with one wrong word may still **pass** overall score at 0.85 while still listing `wrong_words` (spec scores overall ratio; UI shows mistakes)

---

## 7. Corpus provenance & Fatihah fix

- **Download:** `datasets.load_dataset("arbml/quran_uthmani", split="train")`
- **Fields:** `sorah`, `ayah`, `sentence`
- **Enrichment:** Arabic/English surah names from static `SURAH_META` in `download_quran.py`
- **Bug found in session:** dataset omits Al-Fatihah **ayah 1** (Bismillah); remaining ayahs numbered 2–7. UI used `1..ayah_count` → selecting 1 returned **404 Selected ayah does not exist**.
- **Fix:** `ensure_fatihah_complete()` inserts  
  `بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ`  
  as ayah 1; `--repair` patches existing files; Docker entrypoint runs repair when corpus already present.
- **Frontend fix:** load ayah option list from `GET /api/quran/surahs/{id}` (real numbers), not `1..ayah_count`.
- After repair: **114 surahs, 6236 ayahs** (6235 upstream + inserted Fatihah 1).

---

## 8. Speech / Moonshine

- Model: `UsefulSensors/moonshine-tiny-ar` (~112MB)
- Lazy-load on first `transcribe`
- Audio for STT: mono 16 kHz float samples
- HF cache: `/models/huggingface` (Compose volume `hf_cache`, K8s PVC)
- First assess is slow until model downloads unless `PREFETCH_MODEL=1`
- Unauthenticated HF Hub warning in logs is noisy but OK if model is already cached

---

## 8b. Browser audio → backend decode (critical)

**Frontend** (`frontend/src/App.vue`):

- `MediaRecorder` default → `Blob(..., { type: "audio/webm" })` named `recitation.webm`
- Multipart field name: `audio`; also sends `surah`, `ayah`, `threshold`
- Same-origin via nginx: browser → `:5173/api/...` → proxy → `backend:8000`

**Why raw librosa fails on WebM:**

- Librosa **1.x** loads only through **soundfile/libsndfile** (audioread fallback removed).
- libsndfile does **not** recognize WebM/Opus → `LibsndfileError: Format not recognised`.
- Assess caught that and returned **`400 {"detail":"Invalid audio file"}`**.

**Fix (implemented):**

- `backend/app/services/audio.py`:
  - `needs_ffmpeg_conversion(path)` — probe with `soundfile.info`; if it fails → need ffmpeg
  - `convert_to_wav(src)` — `ffmpeg -i … -ac 1 -ar 16000 -c:a pcm_s16le` → temp `.wav`
  - `prepare_audio(path) -> (path, owns_file)` — returns soundfile-readable path; caller deletes if `owns_file`
- `api/memorization.py` calls `prepare_audio` **once** before `librosa.get_duration` and `speech.transcribe`; cleans up both temp upload and converted wav in `finally`.
- Dockerfile already installs **ffmpeg** + **libsndfile1** — do not remove them.
- Spec alternative (open decision): “Convert webm via ffmpeg if librosa fails” — this is that path.

**Verify WebM path in running container:**

```bash
docker compose exec backend bash -c '
  ffmpeg -y -f lavfi -i sine=frequency=440:duration=1.5 -ac 1 /tmp/t.wav
  ffmpeg -y -i /tmp/t.wav -c:a libopus /tmp/t.webm
  curl -s -o /tmp/out.json -w "%{http_code}\n" \
    -F surah=1 -F ayah=1 -F threshold=0.5 \
    -F "audio=@/tmp/t.webm;type=audio/webm;filename=recitation.webm" \
    http://127.0.0.1:8000/api/memorization/assess
'
# Expect HTTP 200 (not 400 Invalid audio file). First call may take ~60s while model loads.
```

**Do not:**

- Assume `librosa.load` / `get_duration` can read browser WebM
- Rely on installing `audioread` alone (librosa 1.0 does not use it for load)
- Drop ffmpeg from the image

---

## 9. Config / env pitfalls

| Setting | Gotcha |
|---------|--------|
| `CORS_ORIGINS=*` | Must use `NoDecode` on `list[str]` field or pydantic-settings JSON-parses `*` and **crashes on startup** |
| `QURAN_PATH` | Relative to backend root → `data/quran.json` |
| Compose `CORS_ORIGINS: "*"` | Valid only after NoDecode fix |

---

## 10. Docker Compose behavior

**Prod-like (`docker-compose.yml`):**

- Frontend published at **5173→80** (nginx), API at **8000**
- Nginx proxies `/api/` and `/health` to `backend:8000`
- Named volumes: `quran_data`, `hf_cache`
- Healthcheck on backend `/health`; frontend waits until healthy

**Dev (`docker-compose.dev.yml`):**

- Backend bind-mount + uvicorn `--reload`
- Frontend Node/Vite; proxy target `http://backend:8000`

**Entrypoint order:** missing corpus → download; else → `--repair`; optional prefetch; then CMD.

**Nginx note:** “client request body is buffered to a temporary file” on assess is **normal** for larger multipart bodies — not the root cause of 404s.

---

## 11. Kubernetes

- Manifest: `k8s/deploy.yaml`
- Namespace: `quran-memorization`
- Images expected: `quran-memorization-backend:latest`, `quran-memorization-frontend:latest`
- Frontend nginx assumes Service DNS name **`backend`**
- Ingress example host: `quran.local` (nginx ingress annotations for 6m body / 300s read timeout)
- Load images into kind/minikube before apply (see `k8s/README.md`)

---

## 12. Testing

```bash
cd backend && pytest -q
```

- Unit: normalizer, assessor (exact/wrong/missing/extra), QuranService
- API: TestClient + fixture `tests/fixtures/quran_sample.json` + `MockSpeechRecognizer`
- Fixture includes proper Fatihah ayah 1 (unlike raw upstream dataset)
- `test_assess_webm_via_ffmpeg` encodes Opus WebM with ffmpeg then POSTs assess (skips if no ffmpeg/libopus)
- Host tip: some host ffmpeg builds lack `libopus`; container ffmpeg (Debian bookworm) works

---

## 13. Session incident log (for debugging regressions)

| Symptom | Cause | Resolution |
|---------|-------|------------|
| Backend crash loop: `SettingsError` parsing `CORS_ORIGINS` | pydantic-settings JSON-decoded `*` | `Annotated[list[str], NoDecode]` + string parser |
| Assess 404 “Selected ayah does not exist” on Fatihah 1 | Corpus missing ayah 1; UI assumed 1..N | Insert Bismillah; `--repair`; UI uses real ayah numbers |
| `GET /api/quran/surahs/1/ayahs/1` → 404 on old Docker volume | Volume corpus predated Fatihah repair | Entrypoint `download_quran.py --repair` on start; log may show `Repaired Al-Fatihah ayah 1` |
| Assess **400 Invalid audio file** (frontend WebM) | Librosa 1.x + soundfile cannot decode WebM/Opus | `services/audio.py` ffmpeg→WAV before duration/STT; rebuild backend image |
| Nginx body buffer warning | Large multipart upload | Harmless; ignore unless assess fails for other reasons |
| First `npm install` aborted in agent env | Network/sandbox | Retried successfully; lockfile present |
| Agent Docker socket / compose from sandbox | Sandbox lacks `/var/run/docker.sock` | Use full/`all` permissions for `docker compose` |

**Log signature for the WebM bug:**

```text
backend-1   | ... "POST /api/memorization/assess HTTP/1.1" 400 Bad Request
frontend-1  | ... "POST /api/memorization/assess HTTP/1.1" 400 31 ...
```

Response body length ~31 ≈ `{"detail":"Invalid audio file"}`.

---

## 14. How to run (cheat sheet)

```bash
# Recommended local
docker compose up --build
# App http://localhost:5173  API http://localhost:8000/docs

# Prefetch STT model at start
PREFETCH_MODEL=1 docker compose up --build

# Hot reload
docker compose -f docker-compose.dev.yml up --build

# Host venv
python -m venv .venv && source .venv/bin/activate
python install.py
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
cd frontend && npm install && npm run dev

# Repair corpus only
python backend/download_quran.py --repair

# Reset Docker corpus/model volumes (destructive)
docker compose down -v
```

After code changes with prod-like Compose: **rebuild images** (`--build`). Corpus on `quran_data` persists across recreates; repair runs on each backend start if file exists.

---

## 15. Phase roadmap (from spec; not implemented)

- **1.1:** UI alignment highlighting, orthography toggles, range assess, richer metadata
- **2:** WebSocket streaming STT, incremental feedback, sessions/progress
- **3:** Quran-tuned ASR / lexicon correction; swap recognizer without API changes

---

## 16. Pointers for later prompts

- Prefer `@docs/agent-context.md` + `@implementation-spec.md` when changing product behavior.
- Cursor rules under `.cursor/rules/` inject architecture, backend pitfalls, and Docker/K8s notes automatically.
- Do not reintroduce naive index-zip assessment or mutate corpus text for matching.
- Do not remove Fatihah repair, `NoDecode` on `CORS_ORIGINS`, or ffmpeg WebM conversion without an equivalent fix.
- Prod Compose bakes code into images — after Python/Vue fixes: `docker compose up --build` (hot-copy into a running container is temporary).
- When debugging assess 400s: distinguish empty/short/long/unsupported-suffix vs **Invalid audio file** (decode). Check `prepare_audio` / ffmpeg first for WebM.

---

## 17. Aug 13 audio-debug session summary

**User report:** Frontend assess failed with backend `400`; UI showed Invalid audio file.

**Investigation:**

1. Endpoint raises that detail when `librosa.get_duration` throws (`memorization.py`).
2. Reproduced inside `backend` container: synthetic Opus WebM → soundfile/librosa fail; WAV OK.
3. Confirmed `ffmpeg` present in image; `audioread` not installed and not used by librosa 1.0.
4. Also observed intermittent ayah-1 404s until volume repair ran on restart.

**Shipped:**

- New `backend/app/services/audio.py`
- Assess path uses `prepare_audio` once for duration + STT
- Backend image rebuilt; WebM curl assess returned **200**
- Regression test `test_assess_webm_via_ffmpeg`
