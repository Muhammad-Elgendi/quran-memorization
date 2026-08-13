# Quran Memorization Assessor

Local-first Quran memorization practice tool: select an ayah, recite into the microphone, and receive a score with word-level feedback.

The **FastAPI backend** owns corpus serving, speech-to-text (Moonshine Arabic Tiny), Arabic normalization, and assessment. The **Vue** client is one consumer of the same REST API (Flutter can call the same endpoints later).

## Architecture

```text
Browser (Vue)  --REST-->  FastAPI  -->  Quran JSON
                              |
                              +-->  Moonshine Arabic Tiny (Transformers)
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

First assess may take a while while the ~112 MB speech model downloads into the `hf_cache` volume. Set `PREFETCH_MODEL=1` to download at container start:

```bash
PREFETCH_MODEL=1 docker compose up --build
```

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

## REST API (Flutter-ready)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness |
| `GET` | `/api/quran/surahs` | Surah list |
| `GET` | `/api/quran/surahs/{id}` | Full surah |
| `GET` | `/api/quran/surahs/{id}/ayahs/{ayahId}` | Single ayah |
| `POST` | `/api/memorization/assess` | `multipart`: `surah`, `ayah`, optional `threshold`, `audio` |

Clients send audio + target ayah; they do **not** implement Arabic normalization or scoring.

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

See [`k8s/README.md`](k8s/README.md) for kind/minikube notes. PVCs hold the Quran JSON and Hugging Face model cache. The frontend nginx image proxies `/api` and `/health` to the `backend` Service.

## Corpus provenance

- Download source: Hugging Face [`arbml/quran_uthmani`](https://huggingface.co/datasets/arbml/quran_uthmani)
- Surah Arabic/English names are attached from a static metadata map after download
- Stored text is **Uthmani** and is **never mutated** by the normalizer (normalization is comparison-only)

**Before any public or production release**, verify the corpus against a trusted Uthmani source (for example Tanzil-derived text such as [nuqayah/quran-text](https://github.com/nuqayah/quran-text)) and document the verification date and method.

## Design notes

- Default pass threshold: **85%** (slider / form override clamped to 50–100%)
- Word mismatch threshold: **75%**
- Assessment uses `rapidfuzz` overall similarity plus `difflib.SequenceMatcher` token alignment
- Failed assessments play a short warning tone in the UI
- Phase 1 is REST-only (no streaming WebSocket yet)

## Specs

- [`implementation-spec.md`](implementation-spec.md) — authoritative Phase 1 guide
- [`first-spec.md`](first-spec.md) — source notes
- [`docs/agent-context.md`](docs/agent-context.md) — full build-session context for humans and later AI prompts (architecture, runbooks, incident fixes)
- Cursor rules in [`.cursor/rules/`](.cursor/rules/) — auto-applied project guidance
