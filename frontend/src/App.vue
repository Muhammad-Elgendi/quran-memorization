<script setup>
import { onMounted, ref, watch } from "vue";
import api from "./api";

const surahs = ref([]);
const selectedSurah = ref(null);
const selectedAyah = ref(null);
const ayahOptions = ref([]);
const threshold = ref(0.85);
const recording = ref(false);
const loading = ref(false);
const error = ref("");
const result = ref(null);

let mediaRecorder = null;
let audioChunks = [];

async function loadAyahOptions(surahNumber) {
  if (!surahNumber) {
    ayahOptions.value = [];
    selectedAyah.value = null;
    return;
  }
  const response = await api.get(`/api/quran/surahs/${surahNumber}`);
  ayahOptions.value = (response.data.ayahs || []).map((a) => a.number);
  selectedAyah.value = ayahOptions.value[0] ?? null;
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
  } catch (err) {
    error.value = "Could not load surahs. Is the backend running?";
  }
}

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
  } catch (err) {
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

onMounted(loadSurahs);
</script>

<template>
  <div class="page">
    <header class="hero">
      <p class="eyebrow">Local-first · REST API ready</p>
      <h1>Quran Memorization Assistant</h1>
      <p class="lede">
        Select an ayah, recite into your microphone, and get a score with
        word-level feedback.
      </p>
    </header>

    <main class="panel">
      <section class="controls">
        <label>
          Surah
          <select v-model="selectedSurah">
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
          Ayah
          <select v-model="selectedAyah" :disabled="!ayahOptions.length">
            <option v-for="n in ayahOptions" :key="n" :value="n">
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
          />
        </label>
      </section>

      <section class="recording">
        <button
          v-if="!recording"
          class="primary"
          :disabled="loading || !selectedSurah || !selectedAyah"
          @click="startRecording"
        >
          Start Recitation
        </button>
        <button v-else class="danger" @click="stopRecording">Stop</button>
      </section>

      <p v-if="loading" class="status">Analyzing your recitation…</p>
      <p v-if="error" class="error">{{ error }}</p>

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

.recording {
  display: flex;
  justify-content: center;
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
    grid-template-columns: 1.4fr 0.7fr 1fr;
    align-items: end;
  }
}
</style>
