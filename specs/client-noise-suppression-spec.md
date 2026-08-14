# Client-Side Microphone Noise Suppression — Implementation Spec

**Status:** Draft  
**Phase:** 2.x (frontend enhancement; no backend protocol change required)  
**Companion:** `realtime-stream-spec.md` (PCM streaming), `implementation-spec.md` (REST `/assess`)  
**Version:** 1.0  
**Last updated:** 2026-08-14

---

## 1. Purpose

Improve **speech-to-text accuracy** and **session reliability** by cleaning microphone audio **in the browser** before it reaches the backend. Background noise (fans, traffic, keyboard, room chatter) degrades Moonshine transcription, which in turn lowers assessment scores and can delay ayah completion detection on the WebSocket stream.

This spec defines:

1. The current audio capture paths and where denoising fits
2. A survey of viable client-side options (native → neural WASM)
3. A recommended phased rollout
4. Integration architecture with the existing `AudioWorklet` PCM pipeline
5. Evaluation criteria specific to Arabic recitation + STT
6. Configuration, fallbacks, and non-goals

**Constraint:** All processing stays **client-side**. No audio is sent to third-party denoising APIs. Backend contracts (`pcm_s16le` 16 kHz mono stream; REST WebM upload) remain unchanged unless explicitly extended later.

---

## 2. Background — current frontend audio paths

The Vue app has **two independent capture paths**:

| Path | Mode | Entry point | Format sent to backend | Native constraints today |
|------|------|-------------|------------------------|--------------------------|
| **Stream** | Continuous (WebSocket) | `startPcmCapture()` in `frontend/src/stream.js` | Binary `pcm_s16le`, 16 kHz, mono, 250 ms chunks | `echoCancellation: true`, `noiseSuppression: true`, `autoGainControl: true` |
| **REST** | Single ayah | `startRecording()` in `frontend/src/App.vue` | WebM blob via `MediaRecorder` | `getUserMedia({ audio: true })` — browser defaults only |

### 2.1 Stream pipeline (continuous)

```text
getUserMedia (constraints)
        │
        ▼
AudioContext (device native rate, typically 44.1/48 kHz)
        │
        ▼
MediaStreamSource ──► pcm-capture-processor (AudioWorklet)
                              │
                              │ linear downsample → 16 kHz
                              │ accumulate → Int16Array chunks
                              ▼
                        postMessage → main thread
                              │
                              ▼
                        WebSocket.send(ArrayBuffer)
                              │
                              ▼
                   backend StreamSession.on_audio_chunk()
```

Relevant files:

- `frontend/src/stream.js` — mic open, graph wiring
- `frontend/public/pcm-worklet.js` — downsample + chunking only (no denoise)
- `backend/app/services/stream_audio.py` — ring buffer + energy VAD
- `backend/app/services/stream_session.py` — requires mono 16 kHz PCM

### 2.2 REST pipeline (single ayah)

```text
getUserMedia({ audio: true })
        │
        ▼
MediaRecorder → WebM chunks → Blob upload
        │
        ▼
backend ffmpeg convert → WAV → Moonshine STT
```

No explicit noise constraints; no client-side processing graph.

### 2.3 Why native suppression alone may be insufficient

Browser `noiseSuppression` is implemented inside the OS/browser capture stack (WebRTC APM on Chromium). It is:

- **Effective** for steady hum and basic room noise
- **Inconsistent** across browsers and devices (Safari vs Chrome vs mobile)
- **Opaque** — no tuning, no metrics, no bypass for A/B testing
- **Sometimes harmful** when stacked with a second denoiser (double-processing artifacts)

For a memorization app where **word-level accuracy** drives pass/fail, upgrading from “best-effort native” to a **controlled, testable denoise stage** is justified — especially on the streaming path where VAD and completion probes depend on clean speech boundaries.

---

## 3. Problem statement

| Symptom | Likely cause | User impact |
|---------|--------------|-------------|
| Low score despite correct recitation | STT drops words in noise | False “needs work” |
| Slow or missed ayah advance | VAD sees silence/noise incorrectly | Awkward pauses, retries |
| Extra / wrong words in transcript | Noise transcribed as speech | Confusing alignment |
| Inconsistent results same room | Browser/OS capture differences | Unpredictable UX |

**Success criteria for this feature:**

| ID | Criterion |
|----|-----------|
| NS1 | Measurable WER/score improvement on a fixed noisy test set (see §8) |
| NS2 | No regression on clean mic recordings |
| NS3 | Added end-to-end latency ≤ 50 ms (streaming path) |
| NS4 | Graceful fallback when WASM/AudioWorklet unavailable |
| NS5 | User-toggle or auto-detect noisy environment (optional UX) |
| NS6 | No backend API or protocol changes for v1 |

---

## 4. Option landscape

Options fall into four tiers. **Tier A** is configuration-only; **Tiers B–D** add client-side DSP/neural processing before encode/downsample.

### 4.1 Tier A — Native browser constraints (zero dependency)

Enable WebRTC capture processing via `getUserMedia` constraints:

```javascript
const stream = await navigator.mediaDevices.getUserMedia({
  audio: {
    channelCount: 1,
    echoCancellation: true,   // speaker feedback (less critical here)
    noiseSuppression: true,   // OS/browser noise filter
    autoGainControl: true,    // level normalization
  },
});
```

| Pros | Cons |
|------|------|
| Zero bundle size | Quality varies by platform |
| Hardware-accelerated / low CPU | Cannot disable per-session for A/B |
| Already partially used on stream path | Weak vs non-stationary noise (typing, voices) |
| No WASM/CSP issues | REST path not using explicit constraints today |

**Recommendation:** Always apply explicit constraints on **both** paths. When a Tier B+ denoiser is active, set `noiseSuppression: false` (and usually `autoGainControl: false`) to avoid **double processing** — keep `echoCancellation: true` unless it causes artifacts.

References: [MDN MediaTrackConstraints](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints), WebRTC APM.

---

### 4.2 Tier B — Lightweight classical / small neural (open source, standalone)

Libraries that integrate with plain `getUserMedia` + Web Audio, without a video-call SDK.

#### B1. `@workadventure/noise-suppression` (DTLN + LiteRT.js)

| Attribute | Value |
|-----------|-------|
| License | MIT |
| Model | DTLN (dual-signal transformation LSTM network) |
| Sample rate | **16 kHz native** — aligns with backend PCM contract |
| Integration | `AudioWorklet` between `MediaStreamSource` and downstream node |
| Bundle | Models + LiteRT Wasm bundled; Vite plugin for dev |
| Extras | Optional background-noise **detector** (Silero VAD) for UX prompts |
| Maturity | v0.1.x (2026); active WorkAdventure project |

```javascript
import { createNoiseSuppressionAudioWorklet } from "@workadventure/noise-suppression/audio-worklet";

const context = new AudioContext({ sampleRate: 16000 });
const worklet = await createNoiseSuppressionAudioWorklet(context, { bypassUntilReady: true });
source.connect(worklet.node).connect(destination);
await worklet.ready;
```

**Fit for this project:** **High.** 16 kHz matches stream target; can sit **before** existing `pcm-capture-processor` or replace downsample if graph is rebuilt at 16 kHz.

Docs: [npm package](https://www.npmjs.com/package/@workadventure/noise-suppression), [live demo](https://workadventure.github.io/noise-suppression/).

#### B2. `fastenhancer-web` (FastEnhancer, ICASSP 2026)

| Attribute | Value |
|-----------|-------|
| License | Check repo (open source) |
| Model | FastEnhancer tiny/base/small |
| Sample rate | **48 kHz native** — requires resample to 16 kHz for backend |
| Bundle (gzip) | Tiny 124 KB, Base 391 KB, Small 780 KB |
| Integration | `createStreamDenoiser(micStream)` or AudioWorklet |
| Special headers | None (no COOP/COEP required) |

**Fit for this project:** **Medium–High.** Excellent size/latency; extra resample stage needed after denoise (or run capture worklet at 48 kHz then downsample — same as today).

Docs: [npm](https://www.npmjs.com/package/fastenhancer-web), [demo](https://ryyr-ry.github.io/fastenhancer-web/).

#### B3. `denoise-voice-clarity` (DeepFilterNet 3)

| Attribute | Value |
|-----------|-------|
| License | MIT (stated on npm) |
| Model | DeepFilterNet 3 (+ optional clarity chain: HPF, EQ, AGC, compressor) |
| Sample rate | Typically 48 kHz pipeline |
| Bundle | **~18 MB** WASM (lazy-loaded) |
| Integration | `createDenoisedStream()`, AudioWorklet |

**Fit for this project:** **Medium.** Strong quality and LiveKit-agnostic, but **large** download for a Quran MVP; clarity/AGC chain may interact badly with STT if too aggressive.

Docs: [npm](https://www.npmjs.com/package/denoise-voice-clarity).

#### B4. RNNoise (`rnnoise-wasm` and forks)

| Attribute | Value |
|-----------|-------|
| License | BSD (RNNoise) |
| Model | Recurrent net (2018 era) |
| Bundle | ~95 KB + separate `.wasm` hosting |
| Quality | Adequate for hum/hiss; weaker on complex noise vs modern DL |

**Fit for this project:** **Low–Medium.** Small, but superseded by B1/B2 for quality; extra asset hosting.

---

### 4.3 Tier C — SDK-coupled Krisp processors (not recommended standalone)

These require their vendor’s call/client SDK. They are **not drop-in** for a custom WebSocket PCM app unless you adopt the full SDK.

| Package | Coupling | Notes |
|---------|----------|-------|
| `@livekit/krisp-noise-filter` | **Requires LiveKit** `LocalAudioTrack.setProcessor()` | Models download at runtime; BVC model web-only; Safari < 17.4 unsupported |
| `@stream-io/audio-filters-web` | **Requires Stream Video SDK** `call.microphone.enableNoiseCancellation()` | Krisp.ai under the hood; default model from unpkg CDN |
| VideoSDK noise suppressor | VideoSDK client | Same class of integration |

**Fit for this project:** **Poor** unless the product pivots to LiveKit/Stream for media. Documented here because they appear in generic “JS noise cancellation” guides, but they **do not** integrate with `navigator.mediaDevices` + raw `WebSocket` binary PCM without major architectural adoption.

---

### 4.4 Tier D — Commercial on-device SDKs

| Product | License | Notes |
|---------|---------|-------|
| **Picovoice Koala** | Commercial (AccessKey) | Cross-platform, consistent API, `@picovoice/web-voice-processor` |
| **Krisp SDK** (direct) | Commercial | What LiveKit/Stream wrap |
| **ai-coustics** | Commercial | LiveKit agent-level integration |

**Fit for this project:** **Deferred.** Local-first MVP prefers OSS or native; revisit if OSS quality insufficient and budget allows.

---

## 5. Comparison matrix

Scored for **this codebase** (Vue + custom PCM WebSocket + Moonshine Arabic STT):

| Option | Standalone | 16 kHz fit | Bundle cost | Quality | Latency | License | Verdict |
|--------|------------|------------|-------------|---------|---------|---------|---------|
| Native constraints | ✅ | ✅ | 0 | ★★☆ | ~0 ms | — | **Baseline — always on** |
| `@workadventure/noise-suppression` | ✅ | ✅ native | ~MB (bundled) | ★★★☆ | ~32 ms/frame | MIT | **Primary candidate** |
| `fastenhancer-web` tiny | ✅ | ⚠ resample | 124 KB gzip | ★★★☆ | <1 ms/frame | OSS | **Strong alternate** |
| `denoise-voice-clarity` | ✅ | ⚠ resample | ~18 MB lazy | ★★★★ | ~10 ms | MIT | Quality option; heavy |
| RNNoise wasm | ✅ | ⚠ | ~95 KB + wasm file | ★★☆ | low | BSD | Fallback / legacy |
| `@livekit/krisp-noise-filter` | ❌ | ⚠ | runtime model | ★★★★ | low | ToS | **Reject** (SDK lock-in) |
| `@stream-io/audio-filters-web` | ❌ | ⚠ | runtime model | ★★★★ | low | Proprietary | **Reject** (SDK lock-in) |
| Picovoice Koala | ✅ | configurable | model + SDK | ★★★★ | low | Paid | Future commercial tier |

---

## 6. Recommended approach

### 6.1 Phased rollout

| Phase | Scope | Deliverable |
|-------|-------|-------------|
| **NS-P0** | Config hygiene | Explicit `getUserMedia` constraints on REST + stream; env flag to tune AEC/AGC/NS |
| **NS-P1** | Stream path neural denoise | Insert denoise `AudioWorklet` before PCM downsample/chunk; feature flag `VITE_NOISE_SUPPRESSION=dtln\|fastenhancer\|off` |
| **NS-P2** | REST path parity | Route REST recording through same `AudioCaptureService` → `MediaRecorder` on processed stream |
| **NS-P3** | UX polish | Noisy-environment detector → suggest enabling enhancement; settings toggle in UI |
| **NS-P4** | Evaluation harness | Automated + manual test suite (§8); pick default model |

### 6.2 Primary recommendation (NS-P1)

**Default engine:** `@workadventure/noise-suppression` (DTLN @ 16 kHz)

**Rationale:**

1. **Sample-rate alignment** — backend requires 16 kHz; denoise at capture rate avoids an extra high-rate buffer.
2. **MIT license** — compatible with local-first deployment.
3. **AudioWorklet-native** — matches existing `pcm-worklet.js` architecture.
4. **Bundled assets** — no CDN dependency (contrast Stream.io Krisp default).
5. **Optional noise detector** — supports NS-P3 UX without separate integration.

**Fallback engine:** `fastenhancer-web` model `tiny` when DTLN init fails or CPU budget exceeded on low-end mobile.

**Fallback of last resort:** Tier A native only (`noiseSuppression: true`).

### 6.3 Explicit non-choice

Do **not** adopt LiveKit/Stream SDK solely for Krisp filters. Cost and architectural mismatch outweigh benefits.

---

## 7. Target architecture

### 7.1 Module layout (new frontend code)

```text
frontend/src/audio/
├── capture.js           # unified getUserMedia + constraint profiles
├── denoise/
│   ├── index.js         # factory: createDenoiseNode(context, stream, options)
│   ├── native.js        # passthrough + constraint tuning only
│   ├── dtln.js          # @workadventure/noise-suppression adapter
│   └── fastenhancer.js  # fastenhancer-web adapter
├── pcm-worklet.js       # move from public/ OR keep public/ for static URL
└── capture-service.js   # orchestrates graph for stream + REST
```

Refactor `stream.js` to delegate to `capture-service.js` so REST and WebSocket share one graph.

### 7.2 Stream path — proposed Web Audio graph

**Option A (recommended):** Denoise at 16 kHz, then chunk.

```text
getUserMedia (NS off if DTLN on; EC on; AGC off)
        │
        ▼
AudioContext({ sampleRate: 16000 })
        │
        ▼
MediaStreamSource
        │
        ▼
DTLN NoiseSuppression AudioWorklet     ← NEW
        │
        ▼
pcm-capture-processor (chunk @ 16 kHz)  ← existing logic; may simplify (no downsample)
        │
        ▼
WebSocket binary PCM
```

**Option B:** Denoise at device rate, then downsample (use if 16 kHz `AudioContext` fails on some devices).

```text
AudioContext()  // default device rate
MediaStreamSource → DTLN worklet* → pcm-capture-processor (downsample to 16 kHz)
```

\* Confirm DTLN worklet supports non-16 kHz contexts; if not, Option A is required.

### 7.3 REST path — proposed graph

```text
getUserMedia → … → Denoise worklet → MediaStreamDestination
                                            │
                                            ▼
                                   MediaRecorder(processedStream)
                                            │
                                            ▼
                                   WebM → POST /assess
```

Backend ffmpeg path unchanged.

### 7.4 Double-processing rule

When neural denoise is **enabled**:

```javascript
{
  echoCancellation: true,
  noiseSuppression: false,  // IMPORTANT
  autoGainControl: false,   // let STT see natural dynamics; optional clarity chain later
}
```

When neural denoise is **disabled** (fallback):

```javascript
{
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
}
```

Log active profile once at mic start for supportability.

### 7.5 Feature flags / env

| Variable | Values | Default |
|----------|--------|---------|
| `VITE_AUDIO_DENOISE` | `off` \| `native` \| `dtln` \| `fastenhancer` | `dtln` |
| `VITE_AUDIO_DENOISE_FALLBACK` | `native` \| `off` | `native` |
| `VITE_AUDIO_BYPASS_UNTIL_READY` | `true` \| `false` | `true` |
| `VITE_AUDIO_NOISE_DETECT` | `true` \| `false` | `false` (enable in NS-P3) |

Optional `session.start` extension (future, **not required v1**):

```json
{
  "audio": {
    "format": "pcm_s16le",
    "sample_rate": 16000,
    "client_denoise": "dtln"
  }
}
```

Backend would log only; no behavior change initially.

---

## 8. Evaluation plan

### 8.1 Test corpus

Build a small **noisy recitation dataset** (local, not committed if large):

| Clip | Content | Noise profile |
|------|---------|---------------|
| C1–C5 | 5 ayahs, clean mic | Baseline |
| N1–N5 | Same ayahs | Fan / AC hum |
| N6–N10 | Same ayahs | Keyboard + mouse |
| N11–N15 | Same ayahs | Café / background speech (low) |

Record once; reuse across denoise settings via offline re-processing where possible.

### 8.2 Metrics

| Metric | Source | Pass threshold |
|--------|--------|----------------|
| **Word error rate (WER)** | Moonshine transcript vs expected ayah | ≥ 15% relative WER reduction vs native on noisy set |
| **Assessment score** | `MemorizationAssessor` on noisy clips | Mean score ≥ clean baseline − 5 pp |
| **False advance rate** | Stream session tests | No increase vs baseline |
| **Init time** | Client boot → mic ready | ≤ 3 s on mid-tier laptop (p95) |
| **CPU** | Browser performance tab during 60 s recitation | ≤ +15% vs native-only |
| **Latency** | Chunk timestamp skew | ≤ 50 ms added |

### 8.3 Manual QA checklist

- [ ] Chrome / Firefox / Edge desktop
- [ ] Android Chrome (if supported target)
- [ ] Safari ≥ 17.4 (note: many Krisp-based libs exclude older Safari; DTLN path must be tested explicitly)
- [ ] Headset vs laptop mic
- [ ] Toggle denoise on/off mid-session → defined behavior (restart mic or disallow)
- [ ] CSP: only `wasm-unsafe-eval` if required; document for K8s Ingress headers
- [ ] Docker dev: Vite serves worklet modules correctly (`@workadventure/noise-suppression/vite` plugin)

### 8.4 Regression guard

Add frontend unit tests for factory selection + constraint profiles. Add optional integration test (Playwright) that mocks `getUserMedia` and asserts graph wiring.

Backend: **no change required** for NS-P1; existing `backend/tests/test_stream.py` should pass unchanged.

---

## 9. Implementation tasks

### 9.1 NS-P0 — Constraint hygiene (1 PR)

1. Extract `getAudioConstraints(profile)` to `frontend/src/audio/capture.js`.
2. Apply `explicit` profile in `stream.js`.
3. Apply same profile in `App.vue` REST path (or shared helper).
4. Document profiles in this spec §7.4.

### 9.2 NS-P1 — Stream denoise (1–2 PRs)

1. Add dependency: `@workadventure/noise-suppression`.
2. Add Vite plugin in `frontend/vite.config.js` for dev worklet serving.
3. Implement `createDenoiseNode()` with `dtln` + `native` backends.
4. Wire into `startPcmCapture()` before `pcm-capture-processor`.
5. Consider `AudioContext({ sampleRate: 16000 })` — test fallback if `getUserMedia` rejects.
6. Lazy-init denoise on first mic open (dynamic `import()`).
7. Surface init errors → fallback to native profile + user-visible hint.
8. Copy bundled worklet assets in production build (verify `k8s` frontend image).

### 9.3 NS-P2 — REST parity

1. Refactor `startRecording()` to use `capture-service.startProcessedStream()`.
2. `MediaRecorder` on processed `MediaStream`.
3. Verify ffmpeg/WebM path still decodes correctly.

### 9.4 NS-P3 — UX (optional)

1. Enable `@workadventure/noise-suppression/background-noise` detector.
2. UI: “Background noise detected — enhance microphone?” toggle.
3. Persist preference in `localStorage`.

---

## 10. Risks and mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Over-suppression removes Arabic consonants | STT deletions | A/B on Arabic clips; prefer mild models; allow user off |
| AGC + denoise + STT triple dynamics | Distorted levels | Disable browser AGC when neural denoise on |
| Large WASM download on mobile data | Slow first mic open | Lazy load; show “Preparing audio…” ; cache in service worker (future) |
| AudioWorklet CSP blocked | Feature dead | Feature detect; fallback native; document CSP |
| 16 kHz AudioContext unsupported | Graph fail | Option B graph at device rate |
| Vite dev transforms worklet | Dev-only break | Official Vite plugin / serve raw worklet from `public/` |
| SDK confusion (LiveKit/Stream) | Wasted effort | This spec explicitly rejects for MVP |

---

## 11. Security and privacy

- All denoise processing remains **on-device** (aligns with local-first MVP).
- No new third-party network calls if models are **bundled/self-hosted** (prefer `@workadventure/noise-suppression` or embedded `fastenhancer-web` over CDN-loaded Krisp models).
- Review license files before shipping in Docker/K8s images.

---

## 12. Non-goals

1. **Server-side denoise** — out of scope; client sends cleaned audio; backend STT unchanged.
2. **Tajweed-aware enhancement** — denoise only; no articulation scoring.
3. **Full LiveKit/Stream/WebRTC migration** — not justified for noise alone.
4. **Real-time spectral UI** — optional future polish.
5. **Replacing Moonshine** — orthogonal (Phase 3 ASR).

---

## 13. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-08-14 | Spec created | Explore options; recommend DTLN @ 16 kHz for stream path |
| TBD | Confirm default engine after eval | Run §8 harness on Arabic clips |
| TBD | REST parity priority | May ship NS-P1 stream-only first |

---

## 14. References

- [WorkAdventure noise-suppression (DTLN + LiteRT)](https://github.com/workadventure/noise-suppression) — primary candidate
- [fastenhancer-web](https://github.com/ryyr-ry/fastenhancer-web) — lightweight alternate
- [denoise-voice-clarity (DeepFilterNet 3)](https://www.npmjs.com/package/denoise-voice-clarity)
- [LiveKit Krisp noise filter](https://docs.livekit.io/transport/media/noise-cancellation/) — SDK-coupled; not standalone
- [Stream.io audio filters](https://getstream.io/video/docs/javascript/guides/noise-cancellation/) — SDK-coupled; Krisp
- [Picovoice Koala NS guide](https://picovoice.ai/blog/complete-guide-to-noise-suppression/)
- Project: `frontend/src/stream.js`, `frontend/public/pcm-worklet.js`, `specs/realtime-stream-spec.md`

---

## Appendix A — Sketch API (`capture-service.js`)

```javascript
/**
 * @typedef {'off'|'native'|'dtln'|'fastenhancer'} DenoiseMode
 */

export async function openMic({
  denoise = import.meta.env.VITE_AUDIO_DENOISE ?? 'dtln',
  chunkMs = 250,
  onPcmChunk,
  onError,
} = {}) {
  const constraints = buildConstraints(denoise);
  const rawStream = await navigator.mediaDevices.getUserMedia({ audio: constraints });

  const sampleRate = denoise === 'dtln' ? 16000 : undefined;
  const audioContext = new AudioContext(sampleRate ? { sampleRate } : undefined);

  const source = audioContext.createMediaStreamSource(rawStream);
  let head = source;

  if (denoise === 'dtln' || denoise === 'fastenhancer') {
    const denoiseNode = await createDenoiseNode(audioContext, denoise);
    head.connect(denoiseNode.input);
    head = denoiseNode.output;
    await denoiseNode.ready;
  }

  await audioContext.audioWorklet.addModule('/pcm-worklet.js');
  const pcmNode = new AudioWorkletNode(audioContext, 'pcm-capture-processor', {
    processorOptions: { targetRate: 16000, chunkMs },
  });
  pcmNode.port.onmessage = (e) => onPcmChunk?.(e.data);

  head.connect(pcmNode);
  // mute sink …

  return { audioContext, rawStream, stop: async () => { /* … */ } };
}
```

---

## Appendix B — When **not** to add neural denoise

Skip Tier B+ if:

- Deployment targets environments that block Wasm (`wasm-unsafe-eval` CSP)
- User base is exclusively quiet-room desktop with wired headsets **and** eval shows native meets NS1
- Bundle size budget is extremely strict (< 100 KB) — stay on Tier A + `fastenhancer-web` tiny only

In all cases, still ship **NS-P0** constraint hygiene — low cost, measurable benefit on REST path today.
