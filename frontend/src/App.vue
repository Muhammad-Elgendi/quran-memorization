<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import api from "./api";
import { startPcmCapture, streamWsUrl } from "./stream";

const surahs = ref([]);
const selectedSurah = ref(null);
const selectedAyah = ref(null);
const endAyah = ref(null);
const ayahOptions = ref([]);
const threshold = ref(0.85);
const mode = ref("single"); // single | continuous
const failPolicy = ref("continue"); // continue | stop (UI only)

const recording = ref(false);
const sessionActive = ref(false);
const micActive = ref(false);
const loading = ref(false);
const error = ref("");
const result = ref(null);
const status = ref("");

const currentAyah = ref(null);
const lastAyahResult = ref(null);
const sessionSummary = ref(null);
const sessionId = ref(null);

let mediaRecorder = null;
let audioChunks = [];
let ws = null;
let pcmCapture = null;

const isContinuous = computed(() => mode.value === "continuous");
const continuousBusy = computed(() => sessionActive.value || micActive.value);

async function loadAyahOptions(surahNumber) {
  if (!surahNumber) {
    ayahOptions.value = [];
    selectedAyah.value = null;
    endAyah.value = null;
    return;
  }
  const response = await api.get(`/api/quran/surahs/${surahNumber}`);
  ayahOptions.value = (response.data.ayahs || []).map((a) => a.number);
  selectedAyah.value = ayahOptions.value[0] ?? null;
  endAyah.value = ayahOptions.value[ayahOptions.value.length - 1] ?? null;
}

watch(selectedSurah, (value) => {
  loadAyahOptions(value).catch(() => {
    error.value = "Could not load ayahs for the selected surah.";
  });
});

async function loadSurahs() {
  error.value = "";
  try {
    const response = await api.get("/api/quran/surahs");
    surahs.value = response.data;
    if (surahs.value.length) {
      selectedSurah.value = surahs.value[0].number;
    }
  } catch {
    error.value = "Could not load surahs. Is the backend running?";
  }
}

function playWarning() {
  const audioContext = new AudioContext();
  const oscillator = audioContext.createOscillator();
  const gain = audioContext.createGain();
  oscillator.connect(gain);
  gain.connect(audioContext.destination);
  oscillator.frequency.value = 660;
  gain.gain.value = 0.2;
  oscillator.start();
  oscillator.stop(audioContext.currentTime + 0.3);
}

// --- Single (REST) ------------------------------------------------------

async function startRecording() {
  if (!selectedSurah.value) {
    error.value = "Select a surah first.";
    return;
  }

  result.value = null;
  error.value = "";

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        audioChunks.push(event.data);
      }
    };
    mediaRecorder.onstop = uploadRecording;
    mediaRecorder.start();
    recording.value = true;
  } catch {
    error.value =
      "Microphone permission denied or unavailable. Allow mic access and try again.";
  }
}

function stopRecording() {
  if (!mediaRecorder) {
    return;
  }
  mediaRecorder.stop();
  mediaRecorder.stream.getTracks().forEach((track) => track.stop());
  recording.value = false;
}

async function uploadRecording() {
  loading.value = true;
  error.value = "";

  const blob = new Blob(audioChunks, { type: "audio/webm" });
  const formData = new FormData();
  formData.append("surah", selectedSurah.value);
  formData.append("ayah", selectedAyah.value);
  formData.append("threshold", threshold.value);
  formData.append("audio", blob, "recitation.webm");

  try {
    const response = await api.post("/api/memorization/assess", formData);
    result.value = response.data;
    if (result.value.warning || !result.value.passed) {
      playWarning();
    }
  } catch (err) {
    const detail = err.response?.data?.detail;
    error.value =
      typeof detail === "string"
        ? detail
        : "Assessment failed. Check audio length and backend logs.";
  } finally {
    loading.value = false;
  }
}

// --- Continuous (WebSocket) ---------------------------------------------

function handleStreamMessage(msg) {
  switch (msg.type) {
    case "session.ready":
      sessionId.value = msg.session_id;
      currentAyah.value = msg.current;
      sessionActive.value = true;
      status.value = `Ready — ${msg.current.surah}:${msg.current.ayah}. Press Start Recitation.`;
      break;
    case "ayah.result":
      lastAyahResult.value = msg;
      result.value = msg;
      if (msg.warning || !msg.passed) {
        playWarning();
      }
      status.value = msg.passed
        ? `Passed ${msg.surah}:${msg.ayah} (${Math.round(msg.score * 100)}%)`
        : `Needs work on ${msg.surah}:${msg.ayah} (${Math.round(msg.score * 100)}%)`;
      break;
    case "session.advance":
      currentAyah.value = msg.to;
      status.value = micActive.value
        ? `Listening — ${msg.to.surah}:${msg.to.ayah}`
        : `Next — ${msg.to.surah}:${msg.to.ayah}. Press Start Recitation.`;
      break;
    case "session.waiting":
      status.value = msg.hint || "Retry the same ayah.";
      break;
    case "session.summary":
      sessionSummary.value = msg;
      status.value = "Session ended.";
      sessionActive.value = false;
      break;
    case "error":
      error.value = msg.message || "Stream error";
      if (msg.fatal) {
        sessionActive.value = false;
        stopMic();
      }
      break;
    case "pong":
      break;
    default:
      break;
  }
}

function ensureSession() {
  return new Promise((resolve, reject) => {
    if (ws && ws.readyState === WebSocket.OPEN && sessionActive.value) {
      resolve();
      return;
    }

    error.value = "";
    result.value = null;
    lastAyahResult.value = null;
    sessionSummary.value = null;
    currentAyah.value = null;
    status.value = "Connecting…";

    const url = streamWsUrl();
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    let settled = false;

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: "session.start",
          start_surah: selectedSurah.value,
          start_ayah: selectedAyah.value,
          end_surah: endAyah.value ? selectedSurah.value : null,
          end_ayah: endAyah.value || null,
          threshold: threshold.value,
          fail_policy: failPolicy.value,
          cross_surah: false,
          partials: false,
          auto_advance: true,
          audio: {
            format: "pcm_s16le",
            sample_rate: 16000,
            channels: 1,
            chunk_ms: 250,
          },
        }),
      );
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== "string") {
        return;
      }
      try {
        const msg = JSON.parse(event.data);
        handleStreamMessage(msg);
        if (!settled && msg.type === "session.ready") {
          settled = true;
          resolve();
        } else if (!settled && msg.type === "error" && msg.fatal) {
          settled = true;
          reject(new Error(msg.message || "Session failed"));
        }
      } catch {
        error.value = "Invalid message from server.";
      }
    };

    ws.onerror = () => {
      error.value = "WebSocket connection failed. Is the backend running?";
      if (!settled) {
        settled = true;
        reject(new Error("WebSocket connection failed"));
      }
    };

    ws.onclose = () => {
      sessionActive.value = false;
      stopMic();
      if (!sessionSummary.value && !error.value) {
        status.value = "Disconnected.";
      }
      if (!settled) {
        settled = true;
        reject(new Error("WebSocket closed before ready"));
      }
    };
  });
}

async function startMic() {
  if (!selectedSurah.value || !selectedAyah.value) {
    error.value = "Select a surah and start ayah first.";
    return;
  }
  if (micActive.value) {
    return;
  }

  error.value = "";
  try {
    await ensureSession();
  } catch (err) {
    error.value = err?.message || "Could not start continuous session.";
    return;
  }

  try {
    pcmCapture = await startPcmCapture({
      chunkMs: 250,
      onChunk: (buffer) => {
        if (ws && ws.readyState === WebSocket.OPEN && micActive.value) {
          ws.send(buffer);
        }
      },
      onError: () => {
        error.value = "Audio capture failed.";
        stopMic();
      },
    });
    micActive.value = true;
    const cur = currentAyah.value;
    status.value = cur
      ? `Mic on — recite ${cur.surah}:${cur.ayah}`
      : "Mic on — recite continuously.";
  } catch {
    error.value =
      "Microphone permission denied or AudioWorklet unavailable.";
    await stopMic();
  }
}

async function stopMic() {
  micActive.value = false;
  if (pcmCapture) {
    try {
      await pcmCapture.stop();
    } catch {
      /* ignore */
    }
    pcmCapture = null;
  }
  if (sessionActive.value && currentAyah.value) {
    status.value = `Mic off — ${currentAyah.value.surah}:${currentAyah.value.ayah}. Press Start Recitation when ready.`;
  }
}

async function endContinuousSession() {
  await stopMic();
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "session.stop", reason: "user" }));
    } catch {
      ws.close();
    }
  } else if (ws) {
    ws.close();
  }
  sessionActive.value = false;
}

function forceAssess() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "ayah.force_assess" }));
  }
}

function forceAdvance() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "ayah.force_advance", reason: "skip" }));
  }
}

onMounted(loadSurahs);

onBeforeUnmount(() => {
  if (recording.value) {
    stopRecording();
  }
  if (sessionActive.value || micActive.value || ws) {
    endContinuousSession();
  }
});
</script>

<template>
  <div class="page">
    <header class="hero">
      <p class="eyebrow">Local-first · REST + realtime stream</p>
      <h1>Quran Memorization Assistant</h1>
      <p class="lede">
        Practice one ayah at a time, or recite continuously and advance
        automatically as each ayah is accepted.
      </p>
    </header>

    <main class="panel">
      <section class="mode-toggle" role="group" aria-label="Practice mode">
        <button
          type="button"
          :class="{ active: mode === 'single' }"
          :disabled="recording || continuousBusy"
          @click="mode = 'single'"
        >
          Single ayah
        </button>
        <button
          type="button"
          :class="{ active: mode === 'continuous' }"
          :disabled="recording || continuousBusy"
          @click="mode = 'continuous'"
        >
          Continuous
        </button>
      </section>

      <section class="controls">
        <label>
          Surah
          <select v-model="selectedSurah" :disabled="recording || continuousBusy">
            <option
              v-for="surah in surahs"
              :key="surah.number"
              :value="surah.number"
            >
              {{ surah.number }} — {{ surah.name }}
              <template v-if="surah.english_name">
                ({{ surah.english_name }})
              </template>
            </option>
          </select>
        </label>

        <label>
          {{ isContinuous ? "Start ayah" : "Ayah" }}
          <select
            v-model="selectedAyah"
            :disabled="!ayahOptions.length || recording || continuousBusy"
          >
            <option v-for="n in ayahOptions" :key="n" :value="n">
              {{ n }}
            </option>
          </select>
        </label>

        <label v-if="isContinuous">
          End ayah
          <select
            v-model="endAyah"
            :disabled="!ayahOptions.length || recording || continuousBusy"
          >
            <option v-for="n in ayahOptions" :key="'e' + n" :value="n">
              {{ n }}
            </option>
          </select>
        </label>

        <label>
          Accuracy threshold: {{ Math.round(threshold * 100) }}%
          <input
            v-model.number="threshold"
            type="range"
            min="0.5"
            max="1"
            step="0.01"
            :disabled="recording || continuousBusy"
          />
        </label>

        <label v-if="isContinuous">
          On fail
          <select v-model="failPolicy" :disabled="continuousBusy">
            <option value="continue">Continue to next ayah</option>
            <option value="stop">Stop session</option>
          </select>
        </label>
      </section>

      <section
        v-if="isContinuous && currentAyah"
        class="live-ayah"
        dir="rtl"
      >
        <p class="live-meta">
          {{ currentAyah.surah }}:{{ currentAyah.ayah }}
          <span v-if="micActive" class="mic-live"> · mic on</span>
        </p>
        <p class="ayah">{{ currentAyah.text }}</p>
      </section>

      <section class="recording">
        <template v-if="!isContinuous">
          <button
            v-if="!recording"
            class="primary"
            :disabled="loading || !selectedSurah || !selectedAyah"
            @click="startRecording"
          >
            Start Recitation
          </button>
          <button v-else class="danger" @click="stopRecording">Stop</button>
        </template>
        <template v-else>
          <button
            v-if="!micActive"
            class="primary"
            :disabled="!selectedSurah || !selectedAyah"
            @click="startMic"
          >
            Start Recitation
          </button>
          <button v-else class="danger" @click="stopMic">Stop</button>
          <button
            v-if="micActive"
            class="secondary"
            @click="forceAssess"
          >
            Check now
          </button>
          <button
            v-if="sessionActive"
            class="secondary"
            @click="forceAdvance"
          >
            Skip ayah
          </button>
          <button
            v-if="sessionActive"
            class="secondary"
            @click="endContinuousSession"
          >
            End session
          </button>
        </template>
      </section>

      <p v-if="loading" class="status">Analyzing your recitation…</p>
      <p v-if="status && isContinuous" class="status">{{ status }}</p>
      <p v-if="error" class="error">{{ error }}</p>

      <section v-if="sessionSummary" class="result summary">
        <h2>Session summary</h2>
        <p>
          Passed {{ sessionSummary.ayahs_passed }} · Failed
          {{ sessionSummary.ayahs_failed }} · Skipped
          {{ sessionSummary.ayahs_skipped }}
          ({{ sessionSummary.reason }})
        </p>
        <ul v-if="sessionSummary.results?.length">
          <li
            v-for="(row, idx) in sessionSummary.results"
            :key="idx"
          >
            {{ row.surah }}:{{ row.ayah }} —
            {{ Math.round((row.best_score || 0) * 100) }}%
            <template v-if="row.passed"> · passed</template>
            <template v-else-if="row.skipped"> · skipped</template>
            <template v-else> · not passed</template>
          </li>
        </ul>
      </section>

      <section v-if="result" class="result">
        <h2>Score: {{ Math.round(result.score * 100) }}%</h2>
        <div :class="{ correct: result.passed, incorrect: !result.passed }">
          {{ result.message }}
        </div>

        <h3>Recognized</h3>
        <p class="ayah" dir="rtl">{{ result.recognized || "—" }}</p>

        <h3>Expected</h3>
        <p class="ayah" dir="rtl">{{ result.expected }}</p>

        <div v-if="result.wrong_words?.length">
          <h3>Possible mistakes</h3>
          <ul>
            <li v-for="(word, idx) in result.wrong_words" :key="idx">
              Expected: <strong dir="rtl">{{ word.expected }}</strong>
              → Heard: <strong dir="rtl">{{ word.recognized }}</strong>
            </li>
          </ul>
        </div>

        <div v-if="result.missing_words?.length">
          <h3>Missing words</h3>
          <p class="ayah" dir="rtl">{{ result.missing_words.join(" · ") }}</p>
        </div>

        <div v-if="result.extra_words?.length">
          <h3>Extra words</h3>
          <p class="ayah" dir="rtl">{{ result.extra_words.join(" · ") }}</p>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.page {
  max-width: 920px;
  margin: 0 auto;
  padding: 2.5rem 1.25rem 4rem;
}

.hero {
  margin-bottom: 1.5rem;
}

.eyebrow {
  margin: 0 0 0.5rem;
  color: var(--muted);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-size: 0.8rem;
}

h1 {
  margin: 0 0 0.6rem;
  font-size: clamp(1.8rem, 3vw, 2.4rem);
  line-height: 1.15;
}

.lede {
  margin: 0;
  color: var(--muted);
  max-width: 40rem;
}

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 18px;
  box-shadow: var(--shadow);
  padding: 1.5rem;
}

.mode-toggle {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1.25rem;
}

.mode-toggle button {
  flex: 1;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--ink);
  padding: 0.7rem 1rem;
}

.mode-toggle button.active {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, white);
  color: var(--accent);
  font-weight: 600;
}

.controls {
  display: grid;
  gap: 1rem;
}

label {
  display: grid;
  gap: 0.45rem;
  color: var(--muted);
  font-size: 0.95rem;
}

select,
input[type="range"] {
  width: 100%;
}

select {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0.75rem 0.9rem;
  background: #fff;
  color: var(--ink);
}

.live-ayah {
  margin-top: 1.25rem;
  padding: 1rem 1.1rem;
  border-radius: 12px;
  background: color-mix(in srgb, var(--accent) 8%, white);
  border: 1px solid color-mix(in srgb, var(--accent) 25%, var(--border));
}

.live-meta {
  margin: 0 0 0.35rem;
  font-size: 0.85rem;
  color: var(--muted);
  direction: ltr;
  text-align: start;
}

.mic-live {
  color: var(--fail);
  font-weight: 600;
}

.recording {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.75rem;
  margin: 1.75rem 0 0.5rem;
}

button {
  border: 0;
  border-radius: 999px;
  padding: 0.9rem 1.6rem;
  cursor: pointer;
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.primary {
  background: var(--accent);
  color: #fff;
}

.danger {
  background: var(--fail);
  color: #fff;
}

.secondary {
  background: #fff;
  color: var(--ink);
  border: 1px solid var(--border);
}

.status {
  text-align: center;
  color: var(--muted);
}

.error {
  text-align: center;
  color: var(--fail);
  background: var(--fail-soft);
  padding: 0.75rem 1rem;
  border-radius: 10px;
}

.result {
  margin-top: 1.5rem;
  display: grid;
  gap: 0.75rem;
}

.correct,
.incorrect {
  padding: 0.9rem 1rem;
  border-radius: 10px;
}

.correct {
  background: var(--pass-soft);
  color: var(--pass);
}

.incorrect {
  background: var(--fail-soft);
  color: var(--fail);
}

.ayah {
  margin: 0;
  font-size: 1.45rem;
  line-height: 2;
}

ul {
  margin: 0;
  padding-inline-start: 1.2rem;
}

@media (min-width: 720px) {
  .controls {
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    align-items: end;
  }
}
</style>
