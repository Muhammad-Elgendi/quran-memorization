# STT Model Switch — Moonshine Arabic Tiny → Tarteel Whisper Tiny AR Quran

**Status:** Ready for implementation  
**Phase:** 2.x (model swap; protocol / assessment unchanged)  
**Companion:**  
- `implementation-spec.md` §9 (STT behind `SpeechRecognizer`)  
- `stt-confidence-filter-spec.md` (Heard keep floor; generate + scores)  
- `realtime-stream-spec.md` (WS `/stream`, PCM 16 kHz)  
- `ayah-advance-fix-spec.md` / `uthmani-tanzeel-word-matching-spec.md` (coverage + lexicon recovery — do not regress)  
**Version:** 1.0  
**Last updated:** 2026-08-16

---

## 1. Purpose

Replace the live speech-to-text weights **only**. The backend continues to expose the same REST `/assess` and WebSocket `/stream` contracts, the same assessor / normalizer / credit / coverage behavior, and the same Vue Heard UX. The recognizer still implements `SpeechRecognizer`.

| Today (from) | Tomorrow (to) |
|--------------|---------------|
| `UsefulSensors/moonshine-tiny-ar` (Moonshine Arabic Tiny; org/GitHub: moonshine-ai) | `tarteel-ai/whisper-tiny-ar-quran` |
| `MoonshineArabicRecognizer` | `WhisperQuranRecognizer` |
| Env `MOONSHINE_MODEL` | Env `STT_MODEL` |

This is the swap the original design reserved (`SpeechRecognizer` so the ASR can change later). It is **not** a new assessment mode, a new protocol version, or a threshold retune.

**Why Tarteel (context only, not a new product goal):** `tarteel-ai/whisper-tiny-ar-quran` is a fine-tune of `openai/whisper-tiny` on Quran recitation (eval WER **7.05** on the trainer card; Apache-2.0). Moonshine Tiny AR is general modern Arabic. Quran-domain ASR should reduce Heard garbage and Uthmani↔STT mismatch that later specs papered over — without changing how those specs decide keep / pass / advance.

---

## 2. Hard constraint — freeze product behavior

Do **not** ship any of the following as part of this change:

| Frozen | Reason |
|--------|--------|
| Stored Quran text / `quran.json` | Non-negotiable |
| `ArabicNormalizer` / assessor alignment / `WORD_MATCH_THRESHOLD` / `DEFAULT_THRESHOLD` / `STREAM_COVERAGE_THRESHOLD` | Scoring stays the same contract |
| REST JSON field names and WS frame types | Flutter / Vue clients must not notice |
| Energy VAD, silence windows, credit cursor, fail policies, mistake tone, denoise | Orthogonal |
| `MockSpeechRecognizer` semantics | CI stays model-free |
| Loading Transformers / the HF model in pytest | Same as today |
| Adding `openai-whisper`, Faster-Whisper, CTranslate2, or a second live model | One recognizer, Transformers path |
| Whisper `no_speech_prob` as a new Heard dump | Energy gate + sequence confidence already cover this (`stt-confidence-filter-spec.md` §6) |
| Forced decoding of the expected ayah | Would fake a pass |
| Lowering pass/coverage thresholds to make the new model “look better” | Hide model issues, do not paper them |
| Parallel Moonshine fallback | “Only” means one checkpoint |

Allowed changes are **only**:

1. Model id, class name, env/config name, prefetch default, docs/ops strings.
2. **Model-required** inference details (Whisper `input_features`, Arabic+transcribe decoder prompt, 30 s encoder window, special-token ids / decode).
3. **Special-token sanitizing** so Whisper prefix tokens (`<|ar|>`, `<|transcribe|>`, …) never reach Heard or the assessor (backend primary; Vue defensive).
4. **Decoder-probability gamma lab** if Whisper token *P* is not on the Moonshine-Tiny calibration (`STT_DECODER_PROB_GAMMA = 0.12`). Filter *contract* stays “keep iff calibrated *P* ≥ *T*”; only the map may change after lab.

---

## 3. Current vs target pipeline

Unchanged outer pipe:

```text
PCM / WAV 16 kHz mono
        │
        ▼
SpeechRecognizer.transcribe[_audio][_detailed]
        │
        ├─ energy gate (stream only)     ← unchanged
        ├─ generate + decoder scores     ← same contract, Whisper generate kwargs
        ├─ skip/strip special tokens     ← stronger (Whisper prefix tokens)
        ├─ calibrate + keep at T         ← same; gamma lab only
        ├─ ayah lexicon recovery         ← unchanged
        └─ Transcription.text → assessor / partial.transcript / Heard
```

Inner generate today (`MoonshineArabicRecognizer._transcribe_samples_detailed`):

```python
inputs = self._processor(audio, sampling_rate=16000, return_tensors="pt")
generated = self._model.generate(**inputs, max_new_tokens=STT_MAX_NEW_TOKENS, ...)
raw = self._processor.batch_decode(sequences, skip_special_tokens=True)[0].strip()
```

Moonshine processors typically yield encoder `input_values` (variable-length waveform). Whisper processors yield **fixed 30 s log-Mel `input_features`**. Spreading `**inputs` into `generate()` can also pass an `attention_mask` that Whisper’s generate path mishandles. That is the main inference-shape change; it is not an API change.

---

## 4. Model facts (normative)

| Item | Value |
|------|--------|
| HF id | `tarteel-ai/whisper-tiny-ar-quran` |
| Base | `openai/whisper-tiny` (39M; encoder+decoder seq2seq) |
| License | Apache-2.0 |
| Transformers class | `WhisperForConditionalGeneration` (still loaded via `AutoModelForSpeechSeq2Seq`) |
| Processor | `AutoProcessor` / Whisper processor + tokenizer |
| Audio | mono **16 kHz** float32 (same as today; `librosa.load(..., sr=16000)`) |
| Encoder window | **30.0 s** = `30 * 16000 = 480_000` samples. Feature extractor pads short clips and **truncates long clips from the start** unless we slice first. |
| Disk size | ~150–160 MB weights (+ tokenizer JSON). PVC `hf-model-cache` 5Gi stays enough. |
| Prefetch cache key | `models--tarteel-ai--whisper-tiny-ar-quran` under `$HF_HUB_CACHE` |
| Trainer card (informational) | Transformers 4.26 era fine-tune; eval WER 7.0535. Runtime here is **transformers≥4.46** already in `requirements.txt`. |

Do **not** pin an extra pip package. Do **not** switch to the `whisper` CLI / `openai-whisper` sliding long-form transcribe. Keep one `generate()` per recognizer call, same as Moonshine.

Hugging Face card: <https://huggingface.co/tarteel-ai/whisper-tiny-ar-quran>

---

## 5. Goals and non-goals

### 5.1 Goals

| ID | Goal |
|----|------|
| M1 | Live STT is **only** `tarteel-ai/whisper-tiny-ar-quran`. No Moonshine checkpoint is loaded in Compose, K8s, or `install.py`. |
| M2 | `SpeechRecognizer` ABC, `MockSpeechRecognizer`, REST, and WS stay source-compatible for callers outside `speech_service.py`. |
| M3 | Decode is **Arabic transcription**, never English / translate, never timestamp tokens in Heard. |
| M4 | Special tokens never appear in `Transcription.text`, `recognized`, kept `words[].text`, or the Vue Heard line. |
| M5 | Confidence filter, over-generation trim, and ayah lexicon recovery still run on the surface string. |
| M6 | Prefetch skip-if-cached keys off the **new** repo id (leftover Moonshine blobs must not satisfy prefetch). |
| M7 | CI does not download the model. Unit tests cover sanitizer, 30 s tail clamp, and generate-arg construction with fakes. |

### 5.2 Non-goals

- `tarteel-ai/whisper-base-ar-quran` or any larger Whisper.
- Word-level timestamps / `return_timestamps=True`.
- Using Whisper `no_speech` as a replacement for `STREAM_STT_RMS_THRESHOLD`.
- Changing probe cadence, VAD RMS, `STT_MAX_NEW_TOKENS` (stay **64**), or max upload duration.
- Rewriting historical specs’ narrative (they may still say “Moonshine”; follow-through docs listed in §13 are enough).
- GPU-only path, `device_map`, or quantization.
- Replacing Moonshine references in `first-spec.md` (historical).

---

## 6. Model-specific required changes

These are the only behavioral edits allowed inside the recognizer. Everything else in this section is decode hygiene so Whisper does not leak control tokens or silently drop the end of a long Continuous buffer.

### 6.1 Class, constructor, load

**Replace** `MoonshineArabicRecognizer` with:

```python
class WhisperQuranRecognizer(SpeechRecognizer):
    """Tarteel Whisper Tiny AR Quran via Hugging Face Transformers."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.STT_MODEL
        self._model = None
        self._processor = None
```

Call sites (`main.py` shared singleton, REST router fallback, WS router fallback) construct `WhisperQuranRecognizer()`. Do not leave a `MoonshineArabicRecognizer` alias unless a one-line deprecation is needed for a private fork — this repo has no external importers.

Keep lazy `_load()`:

```python
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

self._processor = AutoProcessor.from_pretrained(self.model_name)
self._model = AutoModelForSpeechSeq2Seq.from_pretrained(self.model_name)
self._model.eval()
```

After load, pin generation so old 4.26-era `generation_config.forced_decoder_ids` cannot force English or `translate`:

```python
gen = self._model.generation_config
gen.language = "ar"
gen.task = "transcribe"
gen.forced_decoder_ids = None   # WhisperGenerationMixin rebuilds from language/task
# do not set return_timestamps
```

Do **not** `.to("cuda")` unless Moonshine already did (it does not). Same CPU-first local-first default.

### 6.2 Features in, not `**processor(...)`

Normative inner inference (`_transcribe_samples_detailed`):

```python
features = self._processor(
    audio,
    sampling_rate=16000,
    return_tensors="pt",
)
input_features = features.input_features  # required

generate_kwargs = {
    "max_new_tokens": settings.STT_MAX_NEW_TOKENS,
    "language": "ar",
    "task": "transcribe",
    "do_sample": False,
}
if want_scores:
    generate_kwargs["return_dict_in_generate"] = True
    generate_kwargs["output_scores"] = True

with torch.no_grad():
    generated = self._model.generate(input_features, **generate_kwargs)
```

**Do not** pass `attention_mask` from the processor into `generate()` unless a lab log proves Whisper 4.46 in this repo needs it and Heard quality improves. Default is features-only.

If `generate(..., language="ar", task="transcribe")` raises `TypeError` (unexpected kwargs on a non-Whisper class), fall back once:

```python
forced = self._processor.get_decoder_prompt_ids(language="ar", task="transcribe")
generated = self._model.generate(
    input_features,
    forced_decoder_ids=forced,
    max_new_tokens=settings.STT_MAX_NEW_TOKENS,
    ...
)
```

Do not keep both `forced_decoder_ids` **and** `language=` on the same call (Transformers warns / double-prompts).

`max_new_tokens=64` stays. Whisper’s baked `max_length=448` is the cap on total decoder ids; passing only `max_new_tokens` is the intended override. If a runtime warns that both are set, clear `generation_config.max_length` on load rather than raising `STT_MAX_NEW_TOKENS`.

### 6.3 30-second window — slice the **tail** before the processor

Whisper’s feature extractor truncates **from sample 0**. Continuous `STREAM_MAX_BUFFER_S` is **45**. If we pass a 45 s ring buffer through, the **current** recitation (at the end) is discarded and STT sees stale audio. That would change Continuous behavior.

**Required** (inside the recognizer, after resample to 16 kHz, before `processor`):

```python
WHISPER_MAX_SAMPLES = 30 * 16000  # 480_000

if audio.size > WHISPER_MAX_SAMPLES:
    audio = audio[-WHISPER_MAX_SAMPLES:]
```

REST `MAX_AUDIO_SECONDS=20` is already inside 30 s — this clamp is a no-op on `/assess`. Do **not** change `STREAM_MAX_BUFFER_S` or `MAX_AUDIO_SECONDS` in this spec.

Do **not** implement OpenAI-Whisper long-form sliding windows. One buffer → one `generate()`, same as today.

### 6.4 Special tokens — skip, collect, then strip

Whisper decoder sequences are **not** “text tokens then eos”. They start with a control prefix:

```text
<|startoftranscript|> <|ar|> <|transcribe|> <|notimestamps|>  [Arabic BPE…]  <|endoftext|>
```

If `return_timestamps` leaks on, timestamp tokens look like `<|0.00|>`, `<|1.28|>`.

Moonshine already:

1. `batch_decode(..., skip_special_tokens=True)`
2. Drops ids in `_collect_special_ids` (pad / bos / eos / unk / decoder_start + `tokenizer.all_special_ids`) before aligning scores to words

**Keep both.** Then add a **surface sanitizer** because Whisper tokenizers (especially 4.26-era fine-tunes on 4.46 runtimes) sometimes leave `<|…|>` in the string even with `skip_special_tokens=True`. Those strings must never become Heard words or assessor tokens.

Normative helper (`speech_service.py` or a tiny `stt_decode.py` next to it — **not** in API routers):

```python
import re

_WHISPER_ANGLE_TOKEN = re.compile(r"<\|[^|>]*\|>")

def strip_decoder_special_tokens(text: str) -> str:
    cleaned = _WHISPER_ANGLE_TOKEN.sub(" ", text or "")
    return " ".join(cleaned.split())
```

Apply **immediately** after `batch_decode`, on the string that becomes `raw` for `filter_transcription` / `transcription_from_plain_text`. Filtered `text` and `raw_text` both go through this (do not leave `raw_text` leaking tokens into protocol debug fields the UI might ever print).

Extend `_collect_special_ids` so score alignment drops prefix ids even when `all_special_ids` is incomplete:

| Source | Include |
|--------|---------|
| Existing | `all_special_ids`, pad/eos/bos/unk/`decoder_start_token_id` |
| Whisper config | `bos_token_id`, `eos_token_id`, `pad_token_id`, `decoder_start_token_id` |
| Optional extra | `tokenizer.additional_special_tokens_ids` if present |
| Timestamp range | If `tokenizer.timestamp_begin` exists, treat `id >= timestamp_begin` as special |

Do **not** treat ordinary Arabic BPE ids as special.

Decode used for per-token pieces must also `skip_special_tokens=True`. `_WORD_START_MARKERS` already includes Whisper’s `Ġ` and SentencePiece `▁` — no change to `words_from_token_pieces`.

### 6.5 Frontend defensive strip (Heard only)

Backend is authoritative. Vue still sanitizes whatever it renders as Heard so a future protocol field cannot leak tokens.

In `frontend/src/highlight.js`, `heardTextFromMessage`:

1. Build the string exactly as today (kept `words[].text` joined, else `recognized`).
2. Run the same `<|…|>` strip + whitespace collapse on the **result** and, when using `words`, on each `word.text` **before** join.

Do not change alignment / chip painting (`wordsFromAlignment`). Do not hide the Heard line when score is low. Do not strip Arabic punctuation or Quran marks.

Extend `frontend/tests/highlight.test.js`:

| Case | Input | Output |
|------|--------|--------|
| Prefix leak | `recognized: "<|startoftranscript|><|ar|>بسم الله"` | `بسم الله` |
| Timestamp leak | `"<\|0.00\|>الحمد<\|0.64\|>"` | `الحمد` |
| Kept words include a token | `words: [{text:"<|ar|>", kept:true}, {text:"الرحمن", kept:true}]` | `الرحمن` |
| Clean Arabic | `الحمد لله` | unchanged |

### 6.6 Confidence filter — contract frozen, gamma is a lab knob

Keep:

- `generate(..., output_scores=True)` + `compute_transition_scores(..., normalize_logits=True)` + `exp` → token *P*
- Drop special ids before word aggregation
- Word confidence = **mean** of subword *P* (current Moonshine override; do not switch back to min)
- `calibrate_decoder_prob` then keep iff `≥ T`
- Sequence dump if calibrated mean `< STT_SEQUENCE_CONFIDENCE_MIN` (0.50)
- `STT_CONFIDENCE_FILTER`, `STT_PARTIAL_MAX_OVERGEN_RATIO`, lexicon recovery

**Moonshine lab 2026-08-14** set `STT_DECODER_PROB_GAMMA = 0.12` because Tiny greedy softmax sat ~0.28 on real speech. Whisper token probabilities are typically **higher / better calibrated**. Leaving gamma at 0.12 would map almost everything toward 1.0 and **weaken** the Heard filter (product regression). Setting gamma to 1.0 without a check could empty Heard if this fine-tune is still under-confident.

**Required lab (not a silent code surprise):**

| Knob | Ship default after lab | Fallback if lab not done before merge |
|------|------------------------|---------------------------------------|
| `STT_DECODER_PROB_GAMMA` | **1.0** if Fatihah 1:1 Heard is non-empty at T=0.85 and 1:5-style garbage still dumps | Keep **0.12** only if identity empties confident Quran speech; record the new value in this spec’s changelog |

Do **not** change `DEFAULT_THRESHOLD` or `STT_SEQUENCE_CONFIDENCE_MIN` to compensate. Mock tests still compare **raw** *P* to *T* (`stt-confidence-filter-spec.md`).

Comment on `calibrate_decoder_prob`: replace “Moonshine Tiny greedy softmax” with “decoder softmax → Accuracy slider” and mention the gamma lab date.

### 6.7 What not to “fix” while swapping

| Temptation | Why out of scope |
|------------|------------------|
| Whisper `no_speech_threshold` | Energy gate already skips quiet probes |
| Encoder attention on the real (unpadded) length | Performance only; 30 s pad is Whisper’s default |
| Suppress English token ids | `language="ar"` is the specified control |
| Strip leading Basmala if the model prepends it | Assessment change; lab-note only |
| Retune `STREAM_PARTIAL_EVERY_MS` because 30 s Mel is heavier | Measure in lab; do not pre-emptively slow probes |

---

## 7. Configuration and ops

### 7.1 Settings

`backend/app/config.py`:

| Remove | Add | Default |
|--------|-----|---------|
| `MOONSHINE_MODEL` | `STT_MODEL` | `"tarteel-ai/whisper-tiny-ar-quran"` |

Do **not** accept `MOONSHINE_MODEL` as a silent alias: leftover Compose env would keep downloading Moonshine and look like “the switch did nothing.” Grep the repo and replace every assignment.

Leave `STT_*` filter knobs as they are except gamma per §6.6.

### 7.2 Files that currently hard-code Moonshine

Replace model id **and** prose in:

| File | Change |
|------|--------|
| `.env.example` | `STT_MODEL=tarteel-ai/whisper-tiny-ar-quran` |
| `docker-compose.yml` | prefetch + backend env |
| `docker-compose.dev.yml` | prefetch env |
| `k8s/deploy.yaml` | ConfigMap `STT_MODEL`; PVC label `purpose: stt-model-cache` (or keep name, fix comment) |
| `k8s/README.md` | ~150–160 MB Tarteel Whisper Tiny, not 112 MB Moonshine |
| `backend/prefetch_model.py` | `DEFAULT_MODEL`; read `STT_MODEL`; docstring |
| `backend/tests/test_prefetch.py` | repo id strings in fixtures |
| `install.py` | print “Prefetching Tarteel Whisper Tiny AR Quran…” |
| `README.md` | architecture line, prefetch size |
| `docs/agent-context.md` | §1 / §8 speech |
| `.cursor/rules/quran-memorization.mdc` | stack + “no Moonshine imports” → “no STT model imports in routers” |
| `.cursor/rules/docker-k8s.mdc` | prefetch sentence |
| `specs/implementation-spec.md` | G2, architecture, §5 `STT_MODEL`, §9.2 class/model, references |

`prefetch_model.py` already skip-if-cached by `repo_id`. After the default changes, a volume that only holds Moonshine **must** download Tarteel. Document: first `docker compose up` after this change is a cold prefetch; `docker compose down -v` also wipes `quran_data` — prefer leaving the old blobs (harmless extra ~112 MB) over a casual `-v`.

K8s: `kubectl -n quran-memorization delete pvc hf-model-cache` only if operators want to reclaim Moonshine bytes; a rolling initContainer will fetch Tarteel into the same claim if space remains (5Gi is enough for both + headroom).

### 7.3 Health / logs

Do **not** add the model id to `/health` unless already present (it is not). Optional debug log at first `_load()`: `STT loaded {self.model_name}` — fine; not required.

Unauthenticated HF Hub warnings stay noisy-but-OK when cached.

---

## 8. Code map (implementation)

| File | Action |
|------|--------|
| `backend/app/services/speech_service.py` | Rename class; §6.1–6.4; keep ABC + mock |
| `backend/app/services/stt_confidence.py` | Gamma comment only; piece markers already Whisper-safe |
| `backend/app/config.py` | `STT_MODEL` |
| `backend/app/main.py` | `WhisperQuranRecognizer` singleton comment |
| `backend/app/api/memorization.py` | import / fallback type only (no Transformers) |
| `backend/app/api/memorization_stream.py` | same |
| `backend/prefetch_model.py` | default + env |
| `frontend/src/highlight.js` | strip in `heardTextFromMessage` |
| `frontend/tests/highlight.test.js` | §6.5 cases |
| Ops / docs | §7.2 |

Routers must still import **only** `SpeechRecognizer` + the recognizer class from `speech_service`. No `transformers`, no `WhisperForConditionalGeneration` in `api/`.

`stream_session.py`, `assessor.py`, `normalizer.py`, `audio.py` (ffmpeg WebM), Vue recording / WS capture: **no edits** except Heard sanitizer.

---

## 9. Tests (must fail if Moonshine is still wired; must not load Tarteel)

Do not add GPU/HF network tests to pytest. Prefer fakes.

### 9.1 Prefetch / config

| ID | Assert |
|----|--------|
| T-P1 | `DEFAULT_MODEL` / `settings.STT_MODEL` == `tarteel-ai/whisper-tiny-ar-quran` |
| T-P2 | `repo_cache_dir("tarteel-ai/whisper-tiny-ar-quran")` uses `models--tarteel-ai--whisper-tiny-ar-quran` |
| T-P3 | Existing complete-snapshot fixture still works with the **new** repo id |
| T-P4 | Repo grep in CI optional: no `UsefulSensors/moonshine` / `MOONSHINE_MODEL` left in runtime files (ok in `specs/first-spec.md` and historical specs) |

### 9.2 Decode hygiene (no model)

| ID | Assert |
|----|--------|
| T-S1 | `strip_decoder_special_tokens` on a full Whisper prefix + Arabic returns Arabic only |
| T-S2 | Timestamp `<\|0.00\|>` tokens stripped |
| T-S3 | Empty / whitespace-only after strip → `""` |
| T-S4 | Ordinary `الحمد لله` unchanged (no over-strip) |
| T-S5 | `_collect_special_ids` on a stub with `additional_special_tokens_ids` + `timestamp_begin` drops those ids from a fake `zip(gen_ids, probs)` loop (or unit the collector directly) |

### 9.3 Window clamp (no model)

Extract **or** duplicate a pure function `clamp_whisper_audio(audio, sr=16000) -> ndarray`:

| ID | Assert |
|----|--------|
| T-W1 | `len <= 480_000` unchanged |
| T-W2 | `len > 480_000` → last 480_000 samples (tail), not the head |
| T-W3 | stereo / int16 path still goes through existing `_pcm_to_float32` then clamp (if clamp sits after float conversion) |

### 9.4 Generate kwargs (fake model)

Patch `_load` with a dummy processor + dummy model whose `generate` records kwargs:

| ID | Assert |
|----|--------|
| T-G1 | `generate` called with a tensor named/positioned as features, **not** `attention_mask` |
| T-G2 | `language == "ar"` and `task == "transcribe"` (or `forced_decoder_ids` fallback only) |
| T-G3 | `max_new_tokens == 64`; `output_scores` / `return_dict_in_generate` follow `STT_CONFIDENCE_FILTER` |
| T-G4 | `batch_decode` receives `skip_special_tokens=True` and output is passed through the sanitizer before `filter_transcription` |

### 9.5 Regression — existing suite

`cd backend && pytest -q` stays green: stream credit, coverage advance, confidence C1–C6, prefetch layout. No test may import Transformers.

Frontend: `vitest` highlight tests including §6.5.

### 9.6 Forbidden tests

Do not assert WER, Quran-specific spelling (`الرحمن` vs `الرحمان`), or tashkeel presence. Those are lab, not CI. Do not instantiate `WhisperQuranRecognizer()._load()` in pytest.

---

## 10. Manual lab (real weights, after prefetch)

Record results in a short note under `docs/` or this spec’s changelog — do not tune frozen thresholds to chase a screenshot.

| Lab | Pass if |
|-----|---------|
| L1 REST Fatihah **1:1** at T=0.85, clean mic | HTTP 200; Heard is Arabic Basmala-like; **no** `<|` tokens; score not artificially 0% from empty Heard |
| L2 Continuous **1:2** (dagger-alef ayah) | Coverage can still reach 1.0 via existing simple-Arabic / recovery path; auto-advance still works (`ayah-advance-fix-spec.md`) |
| L3 Quiet mic / noise | Energy gate or sequence dump → no vulgar / English Heard dump |
| L4 `skip_special_tokens` leak | If raw decode without sanitizer would show `<|ar|>`, UI still clean |
| L5 Gamma | Identity (1.0) vs 0.12: Heard neither empty on L1 nor flooded with junk on L3. Write the chosen gamma into `.env.example` comments |
| L6 CPU | One Continuous session on the same host class as today remains usable (partials ~2 s). If one core pegs because every probe encodes 30 s Mel, **document** — do not silently change `STREAM_PARTIAL_EVERY_MS` in this PR unless the session is unusable; if you must, call it out as an ops exception in the PR, not a product redesign |
| L7 Cache | Second `docker compose up` does not re-download (`Using cached tarteel-ai/whisper-tiny-ar-quran`) |
| L8 WebM | Existing ffmpeg assess path still 200 (model load may be slower first time) |

Orthography note: Tarteel may emit **more** tashkeel / closer-to-Uthmani text than Moonshine’s undiacritized modern Arabic. That is allowed. The normalizer still compares copies. Do **not** add a “strip all harakat from Heard” UI change.

Risk: some Quran fine-tunes prepend Basmala. If L1 Heard is `بسم الله…` plus the target on a non-Basmala ayah, **do not** special-case strip it in this spec (assessor `extra_words` will show it). File a follow-up if it blocks advance.

---

## 11. Acceptance criteria

1. Runtime default model is `tarteel-ai/whisper-tiny-ar-quran`; grep of compose/k8s/config/`prefetch_model.py`/`install.py`/`.env.example` has **no** `UsefulSensors/moonshine-tiny-ar` or `MOONSHINE_MODEL`.
2. `SpeechRecognizer` ABC unchanged; tests still use `MockSpeechRecognizer`.
3. Generate path uses Whisper `input_features` + Arabic `transcribe`; `skip_special_tokens=True` + sanitizer.
4. Vue Heard never renders `<|…|>` even if the backend string is wrong.
5. Tail 30 s clamp is implemented; REST 20 s uploads unchanged.
6. Confidence filter + lexicon recovery still wrap every live decode.
7. `pytest -q` green without HF download; highlight tests cover token strip.
8. L1 and L4 lab signed off before calling the switch done.
9. No Transformers imports outside `speech_service.py` and prefetch.

---

## 12. Implementation order

1. Config/env/ops strings → `STT_MODEL` + Tarteel id (prefetch will start pulling the right repo).
2. Pure helpers: `strip_decoder_special_tokens`, `clamp_whisper_audio`; tests T-S\* T-W\* T-P\*.
3. Rewrite recognizer class + generate kwargs; fake-model tests T-G\*.
4. Wire `main.py` + routers; delete Moonshine class.
5. Vue Heard sanitizer + vitest.
6. Docs / cursor rules / `implementation-spec.md` §9.
7. Prefetch + L1/L4/L5 lab; set gamma.
8. Full `pytest -q` and a Compose rebuild (`images bake code`).

Do not mix unrelated stream-window or assessor work in the same change.

---

## 13. Follow-through docs (same PR)

Update so new agents do not re-prefetch Moonshine:

- `specs/implementation-spec.md` — G2, diagram “SpeechRecognizer (Tarteel Whisper Tiny AR Quran)”, config table, §9.2, reference link to this HF model; drop `moonshine-voice` as the STT runtime alternative.
- `docs/agent-context.md` — Phase 1 bullet 3; §8 title “Speech / STT”; size ~150–160 MB.
- `.cursor/rules/quran-memorization.mdc` and `docker-k8s.mdc`.
- `README.md` architecture sketch.

Historical specs (`stt-confidence-filter-spec.md`, `ayah-advance-fix-spec.md`, …) may keep “Moonshine” as the bug they fixed. Point agents at **this file** for the live model.

---

## 14. Companion specs — what stays true

| Spec | After this switch |
|------|-------------------|
| `implementation-spec.md` | STT still behind ABC; 16 kHz; skip special tokens; empty transcript = fail 200 not 500; model load fail 503 |
| `stt-confidence-filter-spec.md` | Keep floor still Accuracy *T*; sequence min 0.50; mock raw *P*; gamma is the one knob this spec is allowed to retune |
| `realtime-stream-spec.md` | PCM s16le @ 16 kHz; one shared recognizer |
| `uthmani-tanzeel-word-matching-spec.md` | Recovery still after confidence filter; may fire less often if Tarteel is closer to expected — that is OK |
| `ayah-advance-fix-spec.md` | Coverage vs character score unchanged; do not revive Uthmani round-trip mocks as “the model is exact now” |

Non-negotiables (unchanged wording, STT brand swapped):

1. Never mutate stored Quran text.
2. STT only through `SpeechRecognizer` — no model imports in API routers.
3. Assessment uses sequence alignment, not zip-by-index.
4. Clients send audio + target ayah; Arabic logic stays on the backend.

---

## 15. Changelog

| Date | Note |
|------|------|
| 2026-08-16 | Spec v1.0: one-way switch to `tarteel-ai/whisper-tiny-ar-quran`; freeze product behavior; require Whisper generate shape, tail-30s clamp, special-token sanitizer (backend + Heard UI), gamma lab. |

*When implementing, prefer this file over leftover Moonshine strings in older specs.*
