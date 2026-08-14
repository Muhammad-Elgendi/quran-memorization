<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import api from "./api";
import {
  startPcmCapture,
  startProcessedStream,
  streamWsUrl,
} from "./stream";
import { describeDenoiseMode } from "./audio/capture-service";
import { heardTextFromMessage, wordsFromAlignment } from "./highlight";
import {
  createAttemptWarner,
  playWarningTone,
  primeWarningAudio,
} from "./audio/warning-tone";

const labTrace =
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).has("lab");

const surahs = ref([]);
const selectedSurah = ref(null);
const selectedAyah = ref(null);
const endAyah = ref(null);
const ayahOptions = ref([]);
const threshold = ref(0.85);
const mode = ref("single"); // single | continuous
const failPolicy = ref("retry"); // retry | continue | stop

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
const wsTrace = ref([]);
const liveAlignment = ref([]);
const liveProgress = ref(0);
const liveRecognized = ref("");
const liveSequenceConfidence = ref(null);
const liveFromPartial = ref(false);

let mediaRecorder = null;
let audioChunks = [];
let ws = null;
let pcmCapture = null;
let processedCapture = null;
let captureAudioContext = null;
const attemptWarner = createAttemptWarner();

const isContinuous = computed(() => mode.value === "continuous");
const continuousBusy = computed(() => sessionActive.value || micActive.value);

const liveHighlightedWords = computed(() => {
  if (!currentAyah.value?.text) {
    return [];
  }
  return wordsFromAlignment(currentAyah.value.text, liveAlignment.value, {
    provisional: liveFromPartial.value,
  });
});

const resultHighlightedWords = computed(() => {
  if (!result.value?.expected) {
    return [];
  }
  return wordsFromAlignment(result.value.expected, result.value.alignment);
});

function clearLiveHighlights() {
  liveAlignment.value = [];
  liveProgress.value = 0;
  liveRecognized.value = "";
  liveSequenceConfidence.value = null;
  liveFromPartial.value = false;
}

async function loadAyahOptions(surahNumber, { preserveSelection = false } = {}) {
  if (!surahNumber) {
    ayahOptions.value = [];
    if (!preserveSelection) {
      selectedAyah.value = null;
      endAyah.value = null;
    }
    return;
  }
  const response = await api.get(`/api/quran/surahs/${surahNumber}`);
  ayahOptions.value = (response.data.ayahs || []).map((a) => a.number);
  if (!preserveSelection) {
    selectedAyah.value = ayahOptions.value[0] ?? null;
    // Continuous default: open end (until stop), not last ayah of surah.
    endAyah.value = null;
  }
}

watch(selectedSurah, (value) => {
  // Do not reset start/end ayah mid-session (cross-surah advance syncs surah).
  if (sessionActive.value || continuousBusy.value) {
    loadAyahOptions(value, { preserveSelection: true }).catch(() => {
      error.value = "Could not load ayahs for the selected surah.";
    });
    return;
  }
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

function warnFromResult(msg) {
  attemptWarner.maybeWarn(msg, { audioContext: captureAudioContext });
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
    // Unlock speakers during the click gesture — assess returns later.
    await primeWarningAudio();
    processedCapture = await startProcessedStream();
    mediaRecorder = new MediaRecorder(processedCapture.stream);
    audioChunks = [];

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        audioChunks.push(event.data);
      }
    };
    mediaRecorder.onstop = uploadRecording;
    mediaRecorder.start();
    recording.value = true;
    if (processedCapture.fallbackUsed) {
      status.value =
        "Enhanced mic unavailable — using browser noise filter instead.";
    } else {
      status.value = `Mic: ${describeDenoiseMode(processedCapture.denoiseMode)}`;
    }
  } catch {
    error.value =
      "Microphone permission denied or unavailable. Allow mic access and try again.";
    if (processedCapture) {
      try {
        await processedCapture.stop();
      } catch {
        /* ignore */
      }
      processedCapture = null;
    }
  }
}

async function stopRecording() {
  if (!mediaRecorder) {
    return;
  }
  mediaRecorder.stop();
  recording.value = false;
  if (processedCapture) {
    try {
      await processedCapture.stop();
    } catch {
      /* ignore */
    }
    processedCapture = null;
  }
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
      void playWarningTone({ audioContext: captureAudioContext });
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

function recordWsMessage(direction, payload) {
  if (!labTrace) {
    return;
  }
  wsTrace.value.push({
    direction,
    ts: new Date().toISOString(),
    payload,
  });
}

function downloadWsTrace() {
  const blob = new Blob([JSON.stringify(wsTrace.value, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `ws-trace-${sessionId.value || "session"}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function handleStreamMessage(msg) {
  recordWsMessage("in", msg);
  switch (msg.type) {
    case "session.ready":
      sessionId.value = msg.session_id;
      currentAyah.value = msg.current;
      sessionActive.value = true;
      attemptWarner.reset();
      clearLiveHighlights();
      status.value = `Ready — ${msg.current.surah}:${msg.current.ayah}. Press Start Recitation.`;
      break;
    case "partial.transcript":
      liveRecognized.value = heardTextFromMessage(msg);
      if (typeof msg.sequence_confidence === "number") {
        liveSequenceConfidence.value = msg.sequence_confidence;
      }
      break;
    case "partial.alignment":
      liveAlignment.value = msg.alignment || [];
      liveProgress.value =
        typeof msg.progress === "number" ? msg.progress : liveProgress.value;
      liveFromPartial.value = true;
      if (micActive.value && currentAyah.value) {
        const pct = Math.round(liveProgress.value * 100);
        status.value = `Listening — ${currentAyah.value.surah}:${currentAyah.value.ayah} · ${pct}% in progress`;
      }
      break;
    case "ayah.result":
      lastAyahResult.value = msg;
      result.value = msg;
      liveAlignment.value = msg.alignment || [];
      liveFromPartial.value = false;
      liveProgress.value =
        typeof msg.coverage === "number"
          ? msg.coverage
          : typeof msg.score === "number"
            ? msg.score
            : liveProgress.value;
      if (msg.recognized || msg.stt_words || msg.words) {
        liveRecognized.value = heardTextFromMessage({
          recognized: msg.recognized,
          words: msg.stt_words || msg.words,
        });
      }
      if (typeof msg.sequence_confidence === "number") {
        liveSequenceConfidence.value = msg.sequence_confidence;
      }
      if (msg.warning || !msg.passed) {
        warnFromResult(msg);
      }
      status.value = msg.passed
        ? `Passed ${msg.surah}:${msg.ayah} (${Math.round(msg.score * 100)}%)`
        : `Needs work on ${msg.surah}:${msg.ayah} (${Math.round(msg.score * 100)}%)`;
      break;
    case "session.advance":
      currentAyah.value = msg.to;
      attemptWarner.reset();
      clearLiveHighlights();
      if (msg.to?.surah != null && msg.to.surah !== selectedSurah.value) {
        selectedSurah.value = msg.to.surah;
      }
      if (msg.to?.ayah != null) {
        selectedAyah.value = msg.to.ayah;
      }
      status.value = micActive.value
        ? `Listening — ${msg.to.surah}:${msg.to.ayah}`
        : `Next — ${msg.to.surah}:${msg.to.ayah}. Press Start Recitation.`;
      break;
    case "session.waiting":
      status.value = msg.hint || "Retry the same ayah.";
      break;
    case "session.listening":
      if (msg.cleared) {
        liveRecognized.value = "";
        const keepCredit =
          typeof msg.credit_cursor === "number" && msg.credit_cursor > 0;
        if (keepCredit) {
          if (typeof msg.progress === "number") {
            liveProgress.value = msg.progress;
          }
          liveFromPartial.value = true;
        } else {
          clearLiveHighlights();
        }
      }
      status.value =
        msg.hint ||
        (currentAyah.value
          ? `Still listening — ${currentAyah.value.surah}:${currentAyah.value.ayah}`
          : "Still listening.");
      break;
    case "session.summary":
      sessionSummary.value = msg;
      status.value =
        msg.reason === "range_complete"
          ? "Range complete."
          : msg.reason === "quran_complete"
            ? "Quran complete."
            : msg.reason === "surah_complete"
              ? "Surah complete."
              : msg.reason === "session_timeout"
                ? "Session timed out."
                : "Session ended.";
      sessionActive.value = false;
      attemptWarner.reset();
      captureAudioContext = null;
      clearLiveHighlights();
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
    clearLiveHighlights();
    status.value = "Connecting…";

    const url = streamWsUrl();
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    let settled = false;

    ws.onopen = () => {
      const closedEnd = endAyah.value != null && endAyah.value !== "";
      const startPayload = {
        type: "session.start",
        start_surah: selectedSurah.value,
        start_ayah: selectedAyah.value,
        end_surah: closedEnd ? selectedSurah.value : null,
        end_ayah: closedEnd ? endAyah.value : null,
        threshold: threshold.value,
        fail_policy: failPolicy.value,
        cross_surah: true,
        partials: true,
        auto_advance: true,
        audio: {
          format: "pcm_s16le",
          sample_rate: 16000,
          channels: 1,
          chunk_ms: 250,
        },
      };
      recordWsMessage("out", startPayload);
      ws.send(JSON.stringify(startPayload));
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== "string") {
        if (labTrace) {
          recordWsMessage("in", { type: "binary", bytes: event.data.byteLength });
        }
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
    // Unlock the dedicated warning-tone context on this click (capture graph
    // alone is often inaudible for oscillators while the mic is live).
    await primeWarningAudio();
    pcmCapture = await startPcmCapture({
      chunkMs: 250,
      onChunk: (buffer) => {
        if (ws && ws.readyState === WebSocket.OPEN && micActive.value) {
          recordWsMessage("out", { type: "binary", bytes: buffer.byteLength });
          ws.send(buffer);
        }
      },
      onError: () => {
        error.value = "Audio capture failed.";
        stopMic();
      },
    });
    captureAudioContext = pcmCapture.audioContext || null;
    micActive.value = true;
    const cur = currentAyah.value;
    const denoiseHint = pcmCapture.fallbackUsed
      ? " (browser filter)"
      : ` (${describeDenoiseMode(pcmCapture.denoiseMode)})`;
    status.value = cur
      ? `Mic on${denoiseHint} — recite ${cur.surah}:${cur.ayah}`
      : `Mic on${denoiseHint} — recite continuously.`;
  } catch {
    error.value =
      "Microphone permission denied or AudioWorklet unavailable.";
    await stopMic();
  }
}

async function stopMic() {
  micActive.value = false;
  captureAudioContext = null;
  attemptWarner.reset();
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
    const payload = { type: "ayah.force_assess" };
    recordWsMessage("out", payload);
    ws.send(JSON.stringify(payload));
  }
}

function forceAdvance() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    const payload = { type: "ayah.force_advance", reason: "skip" };
    recordWsMessage("out", payload);
    ws.send(JSON.stringify(payload));
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
            <option :value="null">Until I stop</option>
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
            <option value="retry">Retry same ayah (recommended)</option>
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
          <span v-if="liveProgress > 0" class="live-progress-label">
            · {{ Math.round(liveProgress * 100) }}%
            <template v-if="liveFromPartial"> in progress</template>
          </span>
          <span
            v-if="labTrace && liveSequenceConfidence != null"
            class="live-progress-label"
          >
            · STT {{ Math.round(liveSequenceConfidence * 100) }}%
          </span>
        </p>
        <div
          v-if="liveProgress > 0 || liveHighlightedWords.some((w) => w.status !== 'pending')"
          class="progress-track"
          dir="ltr"
          role="progressbar"
          :aria-valuenow="Math.round(liveProgress * 100)"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div
            class="progress-fill"
            :style="{ width: `${Math.min(100, Math.round(liveProgress * 100))}%` }"
          />
        </div>
        <p class="ayah ayah-words">
          <span
            v-for="(item, idx) in liveHighlightedWords"
            :key="idx"
            class="word"
            :class="`word-${item.status}`"
          >{{ item.word }}</span>
        </p>
        <p v-if="liveRecognized" class="live-heard" dir="ltr">
          Heard:
          <span dir="rtl">{{ liveRecognized }}</span>
        </p>
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
          <button
            v-if="labTrace && wsTrace.length"
            class="secondary"
            type="button"
            @click="downloadWsTrace"
          >
            Download WS trace ({{ wsTrace.length }})
          </button>
        </template>
      </section>

      <p v-if="loading" class="status">Analyzing your recitation…</p>
      <p v-if="status" class="status">{{ status }}</p>
      <p v-if="error" class="error">{{ error }}</p>

      <section v-if="sessionSummary" class="result summary">
        <h2>Session summary</h2>
        <p>
          Passed {{ sessionSummary.ayahs_passed }} · Failed
          {{ sessionSummary.ayahs_failed }} · Skipped
          {{ sessionSummary.ayahs_skipped }}
          ({{ sessionSummary.reason }})
        </p>
        <p
          v-if="sessionSummary.stt_ms_total != null"
          class="meta"
        >
          STT {{ sessionSummary.stt_ms_total }} ms /
          {{ sessionSummary.wall_ms }} ms wall
          <template v-if="sessionSummary.busy_errors">
            · {{ sessionSummary.busy_errors }} busy skips
          </template>
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
        <p class="ayah" dir="rtl">
          {{
            heardTextFromMessage({
              recognized: result.recognized,
              words: result.stt_words,
            }) || "—"
          }}
        </p>

        <h3>Expected</h3>
        <p
          v-if="resultHighlightedWords.length"
          class="ayah ayah-words"
          dir="rtl"
        >
          <span
            v-for="(item, idx) in resultHighlightedWords"
            :key="idx"
            class="word"
            :class="`word-${item.status}`"
          >{{ item.word }}</span>
        </p>
        <p v-else class="ayah" dir="rtl">{{ result.expected }}</p>

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

.live-progress-label {
  color: var(--accent);
  font-weight: 600;
}

.progress-track {
  height: 0.35rem;
  margin: 0 0 0.75rem;
  border-radius: 999px;
  background: color-mix(in srgb, var(--border) 70%, white);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width 0.25s ease;
}

.ayah-words {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.55rem;
  line-height: 1.85;
}

.word {
  border-radius: 0.35rem;
  padding: 0.05rem 0.2rem;
  transition: background-color 0.2s ease, color 0.2s ease;
}

.word-pending {
  color: var(--ink);
  opacity: 0.55;
}

.word-match {
  background: var(--pass-soft);
  color: var(--pass);
}

.word-wrong {
  background: var(--fail-soft);
  color: var(--fail);
  text-decoration: underline;
  text-decoration-thickness: 2px;
}

.word-missing {
  background: color-mix(in srgb, var(--accent) 14%, white);
  color: var(--muted);
  outline: 1px dashed color-mix(in srgb, var(--accent) 40%, var(--border));
}

.live-heard {
  margin: 0.65rem 0 0;
  font-size: 0.9rem;
  color: var(--muted);
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
