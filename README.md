# Quran Memorization Assessor

Local-first Quran memorization practice tool: select an ayah, recite into the microphone, and receive a score with word-level feedback. Use **Single ayah** (REST upload) or **continuous** mode (WebSocket, auto-advance).

The **FastAPI backend** owns corpus serving, speech-to-text (Tarteel Whisper Tiny AR Quran), Arabic normalization, and assessment. The **Vue** client is one consumer of the same API (Flutter can call the same endpoints later).

## Architecture

```text
Browser (Vue)  --REST /assess-->  FastAPI  -->  Quran JSON
       |                              |
       +--WS /stream (PCM)------------+-->  Tarteel Whisper Tiny AR Quran
                                      |
                                      +-->  Normalizer + sequence-aligned assessor
```

## Requirements

- Python **3.11+** (3.12 recommended)
- Node.js **20+**
- Microphone access in the browser
- For Docker: Docker Engine + Compose v2
- Optional: `ffmpeg` on the host if running the backend outside Docker (needed for `webm` decode)

## Quick start (Docker Compose)

Production-like local stack (API + nginx frontend):

```bash
docker compose up --build
```

- App: http://localhost:5173  
- API docs: http://localhost:8000/docs  
- Health: http://localhost:8000/health  

Hot-reload developer stack:

```bash
docker compose -f docker-compose.dev.yml up --build
```

The Tarteel Whisper Tiny AR Quran STT model (~150–160 MB) is **prefetched** on first `up` into the named volume `hf_cache` (`/models/huggingface`). Later starts reuse it; they do not re-download. After this model switch, the first `up` is a cold prefetch even if Moonshine was cached (old blobs are leftover, not a cache hit). Wipe with `docker compose down -v` only if you intend to drop the corpus and model cache.

## Quick start (local venv)

```bash
python -m venv .venv
source .venv/bin/activate
python install.py

# terminal 1
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# terminal 2
cd frontend && npm install && npm run dev
```

- App: http://localhost:5173  
- API docs: http://localhost:8000/docs  

## API (Flutter-ready)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness |
| `GET` | `/api/quran/surahs` | Surah list |
| `GET` | `/api/quran/surahs/{id}` | Full surah |
| `GET` | `/api/quran/surahs/{id}/ayahs/{ayahId}` | Single ayah |
| `POST` | `/api/memorization/assess` | `multipart`: `surah`, `ayah`, optional `threshold`, `audio` |
| `WS` | `/api/memorization/stream` | Continuous session: JSON control + PCM audio |

Clients send audio + target ayah; they do **not** implement Arabic normalization or scoring. Stream protocol: [`specs/realtime-stream-spec.md`](specs/realtime-stream-spec.md).

## Tests

```bash
cd backend
pytest -q
```

Uses a small fixture corpus and a mocked speech recognizer (no GPU / model download required).

## Kubernetes

Manifests live in [`k8s/`](k8s/). Build and load images into your cluster, then:

```bash
kubectl apply -f k8s/deploy.yaml
```

See [`k8s/README.md`](k8s/README.md) for kind/minikube notes. PVCs hold the Quran JSON (`quran-data`) and the STT cache (`hf-model-cache`); an init container prefetches Tarteel Whisper Tiny AR Quran before the API starts. The frontend nginx image proxies `/api` and `/health` to the `backend` Service.

## Corpus provenance

- Download source: Hugging Face [`arbml/quran_uthmani`](https://huggingface.co/datasets/arbml/quran_uthmani)
- Surah Arabic/English names are attached from a static metadata map after download
- Stored text is **Uthmani** and is **never mutated** by the normalizer (normalization is comparison-only)

**Before any public or production release**, verify the corpus against a trusted Uthmani source (for example Tanzil-derived text such as [nuqayah/quran-text](https://github.com/nuqayah/quran-text)) and document the verification date and method.

## Design notes

- Default pass threshold: **85%** (slider / form override clamped to 50–100%)
- Word mismatch threshold: **75%**
- Assessment uses `rapidfuzz` overall similarity plus `difflib.SequenceMatcher` token alignment
- Failed assessments play a short warning tone in the UI (Single REST and Continuous `ayah.result` fail, including a long pause on a wrong / incomplete ayah)
- Continuous mode streams **16 kHz PCM** (AudioWorklet); silence ends an ayah attempt; partials are **off by default** for CPU
- Vite proxy and nginx/Ingress support WebSocket upgrade for `/api`

## Specs

- [`specs/implementation-spec.md`](specs/implementation-spec.md) — Phase 1 REST guide
- [`specs/realtime-stream-spec.md`](specs/realtime-stream-spec.md) — Phase 2 WebSocket protocol
- [`specs/continuous-mistake-tone-spec.md`](specs/continuous-mistake-tone-spec.md) — Continuous fail tone (long-silence `ayah.result`)
- [`specs/first-spec.md`](specs/first-spec.md) — source notes
- [`docs/agent-context.md`](docs/agent-context.md) — full build-session context for humans and later AI prompts
- Cursor rules in [`.cursor/rules/`](.cursor/rules/) — auto-applied project guidance
