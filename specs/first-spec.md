Yes. I would build this as a **local-first Quran memorization assessor**, with the backend exposing a clean REST API so the same backend can later be consumed by Flutter.

One important correction from my previous answer: the current Moonshine repository documents `moonshine-voice` as the Python package and supports Arabic STT; the Arabic Tiny checkpoint is available on Hugging Face as `UsefulSensors/moonshine-tiny-ar`. ([GitHub][1])

For the Quran corpus, I recommend using an ayah-level Arabic dataset rather than scraping a random webpage. The `quranlab/quran` dataset contains 6,236 ayahs and an `arabic-uthmani` configuration, while `arbml/quran_uthmani` provides surah/ayah/text rows. ([Hugging Face][2])

Below is a complete starter project.

## Architecture

```text
Browser / Vue.js
       │
       │ REST + WebSocket
       ▼
┌─────────────────────────────┐
│        FastAPI Backend      │
│                             │
│  Quran API                  │
│  ├── surahs                 │
│  ├── ayahs                  │
│  └── memorization session   │
│                             │
│  Speech API                 │
│  └── Moonshine Arabic Tiny  │
│                             │
│  Assessment Engine          │
│  ├── Arabic normalization   │
│  ├── alignment              │
│  ├── similarity             │
│  └── mistake detection      │
└──────────────┬──────────────┘
               │
               ▼
         Quran corpus
```

The important design decision is **not to compare raw strings directly**.

Quranic text contains:

* diacritics
* Quranic orthographic marks
* different Unicode representations
* optional `ٱ`
* punctuation/ayah markers

So the assessor should have a normalization layer:

```text
Quran text
     │
     ▼
Arabic normalization
     │
     ├── remove tashkeel
     ├── normalize alef
     ├── normalize ya/alef maqsura
     ├── normalize taa marbuta
     └── remove Quranic marks
             │
             ▼
       token comparison
```

That will make the system much more useful for memorization assessment.

---

# Project structure

```text
quran-memorization/
│
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   │
│   │   ├── api/
│   │   │   ├── quran.py
│   │   │   └── memorization.py
│   │   │
│   │   ├── models/
│   │   │   └── schemas.py
│   │   │
│   │   └── services/
│   │       ├── quran_service.py
│   │       ├── speech_service.py
│   │       ├── normalizer.py
│   │       └── assessor.py
│   │
│   ├── data/
│   │   └── quran.json
│   │
│   ├── requirements.txt
│   └── setup.py
│
├── frontend/
│   ├── src/
│   │   ├── App.vue
│   │   ├── main.js
│   │   └── api.js
│   ├── package.json
│   └── vite.config.js
│
└── README.md
```

---

# 1. Backend requirements

`backend/requirements.txt`

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

I would actually keep `torch` optional initially if we use Moonshine's native runtime rather than Transformers.

The official Moonshine project documents:

```bash
pip install moonshine-voice
```

and provides a Python microphone/transcription interface. ([GitHub][1])

---

# 2. Configuration

`backend/app/config.py`

```python
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Quran Memorization Assistant"

    HOST: str = "0.0.0.0"
    PORT: int = 8000

    QURAN_PATH: str = "data/quran.json"

    # Similarity below this percentage produces a warning.
    DEFAULT_THRESHOLD: float = 0.85

    # Minimum speech segment length.
    MIN_AUDIO_SECONDS: float = 0.5

    # Maximum expected recording segment.
    MAX_AUDIO_SECONDS: float = 20.0

    class Config:
        env_file = ".env"


settings = Settings()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

QURAN_FILE = BASE_DIR / settings.QURAN_PATH
```

---

# 3. Quran normalization

This is extremely important.

`backend/app/services/normalizer.py`

```python
import re
import unicodedata


# Arabic tashkeel
TASHKEEL = re.compile(
    r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]"
)

# Quranic annotation marks
QURANIC_MARKS = re.compile(
    r"[\u06D6-\u06ED]"
)

# Arabic punctuation / Quran markers
PUNCTUATION = re.compile(
    r"[،؛؟,.!?()\[\]{}<>\"'ـ۞۩]"
)


def normalize_arabic(text: str) -> str:
    """
    Normalize Arabic text for speech-to-text comparison.

    This intentionally does NOT modify the canonical Quran corpus.
    It creates a comparison representation only.
    """

    if not text:
        return ""

    # Unicode normalization
    text = unicodedata.normalize("NFKC", text)

    # Remove tashkeel
    text = TASHKEEL.sub("", text)

    # Remove Quranic annotation marks
    text = QURANIC_MARKS.sub("", text)

    # Tatweel
    text = text.replace("ـ", "")

    # Normalize common Arabic forms
    text = text.replace("ٱ", "ا")
    text = text.replace("أ", "ا")
    text = text.replace("إ", "ا")
    text = text.replace("آ", "ا")

    text = text.replace("ى", "ي")

    # punctuation
    text = PUNCTUATION.sub(" ", text)

    # whitespace
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def tokenize(text: str) -> list[str]:
    normalized = normalize_arabic(text)

    if not normalized:
        return []

    return normalized.split()
```

---

# 4. Quran service

`backend/app/services/quran_service.py`

```python
import json
from pathlib import Path
from typing import Optional


class QuranService:

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_surahs(self):
        return [
            {
                "number": surah["number"],
                "name": surah["name"],
                "english_name": surah.get("english_name"),
                "ayah_count": len(surah["ayahs"]),
            }
            for surah in self.data
        ]

    def get_surah(self, surah_number: int):
        for surah in self.data:
            if surah["number"] == surah_number:
                return surah

        return None

    def get_ayah(
        self,
        surah_number: int,
        ayah_number: int
    ) -> Optional[dict]:

        surah = self.get_surah(surah_number)

        if not surah:
            return None

        for ayah in surah["ayahs"]:
            if ayah["number"] == ayah_number:
                return ayah

        return None

    def get_range(
        self,
        surah_number: int,
        start_ayah: int,
        end_ayah: int
    ):
        surah = self.get_surah(surah_number)

        if not surah:
            return []

        return [
            ayah
            for ayah in surah["ayahs"]
            if start_ayah <= ayah["number"] <= end_ayah
        ]
```

---

# 5. Speech service

I'd isolate Moonshine completely behind a service interface.

That is important for your future Flutter application.

`backend/app/services/speech_service.py`

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class SpeechRecognizer(ABC):

    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """
        Convert an audio recording to Arabic text.
        """
        raise NotImplementedError


class MoonshineArabicRecognizer(SpeechRecognizer):

    def __init__(self):
        self._model = None
        self._processor = None

    def _load(self):
        """
        Lazy model initialization.

        Keeping initialization lazy means the API server can start
        before the ~100MB model has been loaded into RAM.
        """

        if self._model is not None:
            return

        from transformers import (
            AutoProcessor,
            AutoModelForSpeechSeq2Seq,
        )

        model_name = "UsefulSensors/moonshine-tiny-ar"

        self._processor = AutoProcessor.from_pretrained(
            model_name
        )

        self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_name
        )

        self._model.eval()

    def transcribe(self, audio_path: str) -> str:

        self._load()

        import torch
        import librosa

        audio, _ = librosa.load(
            audio_path,
            sr=16000,
            mono=True
        )

        inputs = self._processor(
            audio,
            sampling_rate=16000,
            return_tensors="pt"
        )

        with torch.no_grad():

            generated_ids = self._model.generate(
                **inputs
            )

        result = self._processor.batch_decode(
            generated_ids,
            skip_special_tokens=True
        )

        return result[0].strip()
```

The Hugging Face model card explicitly provides the `AutoProcessor` / `AutoModelForSpeechSeq2Seq` approach for `UsefulSensors/moonshine-tiny-ar`. ([Hugging Face][3])

The model repository is around **112 MB**, with the safetensors weights around 108 MB. ([Hugging Face][3])

---

# 6. Memorization assessment engine

This is the heart of the application.

`backend/app/services/assessor.py`

```python
from dataclasses import dataclass
from rapidfuzz import fuzz

from .normalizer import normalize_arabic, tokenize


@dataclass
class AssessmentResult:

    score: float

    passed: bool

    expected: str

    recognized: str

    missing_words: list[str]

    extra_words: list[str]

    wrong_words: list[dict]

    message: str


class MemorizationAssessor:

    def __init__(self, threshold: float = 0.85):

        self.threshold = threshold

    def assess(
        self,
        expected: str,
        recognized: str
    ) -> AssessmentResult:

        expected_normalized = normalize_arabic(expected)
        recognized_normalized = normalize_arabic(recognized)

        expected_words = tokenize(expected_normalized)
        recognized_words = tokenize(recognized_normalized)

        # Overall similarity
        score = fuzz.ratio(
            expected_normalized,
            recognized_normalized
        ) / 100.0

        # Word-level comparison
        missing = []
        extra = []
        wrong = []

        max_len = max(
            len(expected_words),
            len(recognized_words)
        )

        for i in range(max_len):

            expected_word = (
                expected_words[i]
                if i < len(expected_words)
                else None
            )

            recognized_word = (
                recognized_words[i]
                if i < len(recognized_words)
                else None
            )

            if expected_word is None:
                extra.append(recognized_word)
                continue

            if recognized_word is None:
                missing.append(expected_word)
                continue

            similarity = fuzz.ratio(
                expected_word,
                recognized_word
            ) / 100.0

            if similarity < 0.75:

                wrong.append({
                    "expected": expected_word,
                    "recognized": recognized_word,
                    "similarity": similarity
                })

        passed = score >= self.threshold

        if passed:

            message = "Excellent. Your recitation closely matches the selected ayah."

        else:

            message = (
                "There may be a memorization error. "
                "Please review the highlighted words."
            )

        return AssessmentResult(
            score=score,
            passed=passed,
            expected=expected,
            recognized=recognized,
            missing_words=missing,
            extra_words=extra,
            wrong_words=wrong,
            message=message,
        )
```

---

# 7. API schemas

`backend/app/models/schemas.py`

```python
from pydantic import BaseModel
from typing import Optional


class Ayah(BaseModel):

    number: int
    text: str


class Surah(BaseModel):

    number: int
    name: str
    english_name: Optional[str] = None
    ayah_count: int


class AssessmentResponse(BaseModel):

    score: float

    passed: bool

    expected: str

    recognized: str

    missing_words: list[str]

    extra_words: list[str]

    wrong_words: list[dict]

    message: str

    warning: bool
```

---

# 8. Quran API

`backend/app/api/quran.py`

```python
from fastapi import APIRouter, HTTPException

from ..services.quran_service import QuranService


router = APIRouter(
    prefix="/api/quran",
    tags=["Quran"]
)


def create_router(quran_service: QuranService):

    @router.get("/surahs")
    def list_surahs():

        return quran_service.get_surahs()

    @router.get("/surahs/{surah_number}")
    def get_surah(surah_number: int):

        surah = quran_service.get_surah(
            surah_number
        )

        if not surah:
            raise HTTPException(
                status_code=404,
                detail="Surah not found"
            )

        return surah

    @router.get(
        "/surahs/{surah_number}/ayahs/{ayah_number}"
    )
    def get_ayah(
        surah_number: int,
        ayah_number: int
    ):

        ayah = quran_service.get_ayah(
            surah_number,
            ayah_number
        )

        if not ayah:
            raise HTTPException(
                status_code=404,
                detail="Ayah not found"
            )

        return ayah

    return router
```

---

# 9. Memorization API

`backend/app/api/memorization.py`

```python
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    UploadFile,
    File,
    Form,
    HTTPException
)

from ..services.quran_service import QuranService
from ..services.speech_service import MoonshineArabicRecognizer
from ..services.assessor import MemorizationAssessor


router = APIRouter(
    prefix="/api/memorization",
    tags=["Memorization"]
)


recognizer = MoonshineArabicRecognizer()


def create_router(quran_service: QuranService):

    @router.post("/assess")
    async def assess(
        surah: int = Form(...),
        ayah: int = Form(...),
        threshold: float = Form(0.85),
        audio: UploadFile = File(...)
    ):

        target = quran_service.get_ayah(
            surah,
            ayah
        )

        if not target:

            raise HTTPException(
                status_code=404,
                detail="Selected ayah does not exist"
            )

        suffix = Path(
            audio.filename or ".wav"
        ).suffix

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix
        ) as temp:

            content = await audio.read()

            temp.write(content)

            temp_path = temp.name

        try:

            recognized = recognizer.transcribe(
                temp_path
            )

            assessor = MemorizationAssessor(
                threshold=threshold
            )

            result = assessor.assess(
                expected=target["text"],
                recognized=recognized
            )

            return {
                "score": result.score,
                "passed": result.passed,
                "expected": result.expected,
                "recognized": result.recognized,
                "missing_words": result.missing_words,
                "extra_words": result.extra_words,
                "wrong_words": result.wrong_words,
                "message": result.message,
                "warning": not result.passed
            }

        finally:

            Path(temp_path).unlink(
                missing_ok=True
            )

    return router
```

---

# 10. FastAPI application

`backend/app/main.py`

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings, QURAN_FILE

from .services.quran_service import QuranService

from .api.quran import create_router as create_quran_router
from .api.memorization import (
    create_router as create_memorization_router
)


app = FastAPI(
    title=settings.APP_NAME,
    description="""
    Local Quran memorization assessment API.

    The backend is intentionally separated from the frontend so
    it can later be consumed by Flutter, Android, iOS, or another
    client.
    """,
    version="1.0.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


quran_service = QuranService(
    QURAN_FILE
)


app.include_router(
    create_quran_router(quran_service)
)

app.include_router(
    create_memorization_router(quran_service)
)


@app.get("/health")
def health():

    return {
        "status": "ok",
        "service": settings.APP_NAME
    }
```

---

# 11. Download the Quran corpus

I would make this a separate script rather than embedding it into FastAPI startup.

`backend/download_quran.py`

```python
import json
from pathlib import Path

from datasets import load_dataset


OUTPUT = Path("data/quran.json")


def main():

    print("Downloading Quran corpus...")

    dataset = load_dataset(
        "arbml/quran_uthmani",
        split="train"
    )

    surahs = {}

    for row in dataset:

        surah_number = int(row["sorah"])
        ayah_number = int(row["ayah"])

        if surah_number not in surahs:

            surahs[surah_number] = {
                "number": surah_number,
                "name": "",
                "ayahs": []
            }

        surahs[surah_number]["ayahs"].append(
            {
                "number": ayah_number,
                "text": row["sentence"]
            }
        )

    result = list(
        surahs.values()
    )

    result.sort(
        key=lambda x: x["number"]
    )

    for surah in result:

        surah["ayahs"].sort(
            key=lambda x: x["number"]
        )

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        OUTPUT,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            result,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        f"Saved {len(result)} surahs to {OUTPUT}"
    )


if __name__ == "__main__":
    main()
```

The `arbml/quran_uthmani` dataset is an ayah-level dataset with `sorah`, `ayah`, and Arabic text fields. ([Hugging Face][4])

**For a production/religious application, I would additionally verify the corpus against a trusted Quran text source before publishing it.** The GitHub `quran-text` project explicitly says its Uthmani text is derived from Tanzil. ([GitHub][5])

---

# 12. Vue frontend

Create it with:

```bash
npm create vite@latest frontend -- --template vue
cd frontend
npm install
npm install axios
```

`frontend/src/api.js`

```javascript
import axios from "axios";

const api = axios.create({
    baseURL: "http://localhost:8000"
});

export default api;
```

---

# 13. Main Vue application

`frontend/src/App.vue`

```vue
<script setup>

import { ref, computed, onMounted } from "vue";
import api from "./api";

const surahs = ref([]);
const selectedSurah = ref(null);
const selectedAyah = ref(1);

const threshold = ref(0.85);

const recording = ref(false);
const loading = ref(false);

const result = ref(null);

let mediaRecorder = null;
let audioChunks = [];


const currentSurah = computed(() => {

    return surahs.value.find(
        s => s.number === Number(selectedSurah.value)
    );

});


async function loadSurahs() {

    const response = await api.get(
        "/api/quran/surahs"
    );

    surahs.value = response.data;

}


function startRecording() {

    result.value = null;

    navigator.mediaDevices
        .getUserMedia({
            audio: true
        })
        .then(stream => {

            mediaRecorder =
                new MediaRecorder(stream);

            audioChunks = [];

            mediaRecorder.ondataavailable =
                event => {

                    audioChunks.push(
                        event.data
                    );

                };

            mediaRecorder.onstop =
                uploadRecording;

            mediaRecorder.start();

            recording.value = true;

        });

}


function stopRecording() {

    if (!mediaRecorder) {
        return;
    }

    mediaRecorder.stop();

    mediaRecorder.stream
        .getTracks()
        .forEach(track => track.stop());

    recording.value = false;

}


async function uploadRecording() {

    loading.value = true;

    const blob = new Blob(
        audioChunks,
        {
            type: "audio/webm"
        }
    );

    const formData = new FormData();

    formData.append(
        "surah",
        selectedSurah.value
    );

    formData.append(
        "ayah",
        selectedAyah.value
    );

    formData.append(
        "threshold",
        threshold.value
    );

    formData.append(
        "audio",
        blob,
        "recitation.webm"
    );

    try {

        const response = await api.post(
            "/api/memorization/assess",
            formData
        );

        result.value = response.data;

        if (result.value.warning) {

            playWarning();

        }

    } finally {

        loading.value = false;

    }

}


function playWarning() {

    const audioContext =
        new AudioContext();

    const oscillator =
        audioContext.createOscillator();

    const gain =
        audioContext.createGain();

    oscillator.connect(gain);

    gain.connect(
        audioContext.destination
    );

    oscillator.frequency.value = 660;

    gain.gain.value = 0.2;

    oscillator.start();

    oscillator.stop(
        audioContext.currentTime + 0.3
    );

}


onMounted(
    loadSurahs
);

</script>


<template>

<div class="container">

    <h1>
        Quran Memorization Assistant
    </h1>

    <div class="controls">

        <label>
            Surah
        </label>

        <select v-model="selectedSurah">

            <option
                v-for="surah in surahs"
                :key="surah.number"
                :value="surah.number"
            >
                {{ surah.number }} -
                {{ surah.name }}
            </option>

        </select>


        <label>
            Start Ayah
        </label>

        <select v-model="selectedAyah">

            <option
                v-for="n in (currentSurah?.ayah_count || 0)"
                :key="n"
                :value="n"
            >
                {{ n }}
            </option>

        </select>


        <label>
            Accuracy threshold:
            {{ Math.round(threshold * 100) }}%
        </label>

        <input
            type="range"
            min="0.5"
            max="1"
            step="0.01"
            v-model.number="threshold"
        />

    </div>


    <div class="recording">

        <button
            v-if="!recording"
            @click="startRecording"
            :disabled="loading"
        >
            🎙️ Start Recitation
        </button>


        <button
            v-else
            @click="stopRecording"
        >
            ⏹ Stop
        </button>

    </div>


    <div
        v-if="loading"
        class="loading"
    >
        Analyzing your recitation...
    </div>


    <div
        v-if="result"
        class="result"
    >

        <h2>
            Score:
            {{ Math.round(result.score * 100) }}%
        </h2>


        <div
            :class="{
                correct: result.passed,
                incorrect: !result.passed
            }"
        >

            {{ result.message }}

        </div>


        <h3>
            Recognized
        </h3>

        <p dir="rtl">
            {{ result.recognized }}
        </p>


        <h3>
            Expected
        </h3>

        <p dir="rtl">
            {{ result.expected }}
        </p>


        <div
            v-if="result.wrong_words.length"
        >

            <h3>
                Possible mistakes
            </h3>

            <ul>

                <li
                    v-for="word in result.wrong_words"
                    :key="word.expected"
                >

                    Expected:
                    <strong>
                        {{ word.expected }}
                    </strong>

                    →

                    Heard:
                    <strong>
                        {{ word.recognized }}
                    </strong>

                </li>

            </ul>

        </div>

    </div>

</div>

</template>


<style>

body {

    font-family:
        Arial,
        sans-serif;

    margin: 0;

    background: #f5f5f5;

}

.container {

    max-width: 900px;

    margin:
        40px auto;

    padding:
        20px;

    background: white;

    border-radius: 12px;

}

.controls {

    display: flex;

    flex-direction: column;

    gap: 10px;

    margin-bottom: 30px;

}

select,
input {

    padding: 10px;

    font-size: 16px;

}

button {

    padding: 15px 25px;

    font-size: 18px;

    cursor: pointer;

}

.recording {

    text-align: center;

    margin: 30px;

}

.result {

    margin-top: 30px;

}

.correct {

    padding: 15px;

    background: #d8f5dc;

}

.incorrect {

    padding: 15px;

    background: #ffd9d9;

}

p {

    line-height: 2;

    font-size: 24px;

}

</style>
```

---

# 14. Installation script

You asked for the Python script to **install the requirements and set everything up**, so I would add:

`install.py`

```python
import os
import subprocess
import sys


def run(command):

    print(
        "\n>>>",
        " ".join(command)
    )

    subprocess.check_call(
        command
    )


def main():

    print(
        "Installing Quran Memorization Assistant..."
    )

    run([
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip"
    ])

    run([
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        "backend/requirements.txt"
    ])

    print(
        "\nDownloading Quran corpus..."
    )

    run([
        sys.executable,
        "backend/download_quran.py"
    ])

    print(
        "\nDownloading Moonshine Arabic Tiny..."
    )

    run([
        sys.executable,
        "-m",
        "huggingface_hub.commands.huggingface_cli",
        "download",
        "UsefulSensors/moonshine-tiny-ar"
    ])

    print(
        "\nInstallation complete."
    )

    print(
        "\nStart backend with:"
    )

    print(
        "cd backend && "
        "uvicorn app.main:app --reload"
    )

    print(
        "\nStart frontend with:"
    )

    print(
        "cd frontend && npm run dev"
    )


if __name__ == "__main__":
    main()
```

One adjustment I'd make in practice: the Hugging Face CLI invocation can vary between `huggingface-cli`/`hf` versions, so the **model can also simply be downloaded automatically by `from_pretrained()` on first use**. The model card supports that workflow. ([Hugging Face][3])

---

# 15. Running it

Backend:

```bash
cd quran-memorization

python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows
# .venv\Scripts\activate

python install.py

cd backend

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend

npm install

npm run dev
```

Then:

```text
http://localhost:5173
```

FastAPI documentation will automatically be available at:

```text
http://localhost:8000/docs
```

That API separation is intentional: a Flutter application can later call exactly the same endpoints.

---

# But I would improve the assessment algorithm

The basic implementation above is a **working prototype**, but for an actual Quran memorization application I would **not rely on one overall Levenshtein/Fuzzy score**.

I'd make the next version:

```text
                    Speech
                      │
                      ▼
                 Moonshine
                      │
                      ▼
             Arabic transcript
                      │
                      ▼
              Arabic Normalizer
                      │
                      ▼
        ┌─────────────────────────┐
        │ Quran Alignment Engine  │
        └────────────┬────────────┘
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       Missing     Wrong       Extra
        words      words       words
          │          │           │
          └──────────┼───────────┘
                     ▼
               Score Engine
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
       Correct               Warning
                               │
                               ▼
                           🔔 Tone
```

More importantly, I'd compare **the expected sequence of words against the recognized sequence**, rather than comparing the entire strings.

For example:

```text
Expected:

وَاللَّهُ غَفُورٌ رَّحِيمٌ

Recognized:

وَاللَّهُ غَفُورٌ رَحِيمٌ
```

should be considered essentially correct despite a diacritic difference.

But:

```text
Expected:

وَاللَّهُ غَفُورٌ رَّحِيمٌ

Recognized:

وَاللَّهُ غَفُورٌ عَلِيمٌ
```

should produce:

```text
✓ وَاللَّهُ
✓ غَفُورٌ
✗ رَّحِيمٌ → عَلِيمٌ
```

rather than simply:

```text
83%
```

---

## Even better: don't wait for the entire ayah

For a **memorization trainer**, I'd eventually make it streaming:

```text
User starts reciting
        │
        ▼
  Audio chunk 1
        │
        ▼
   Moonshine
        │
        ▼
   "الحمد لله"
        │
        ▼
Compare against expected
        │
        ├── Correct → continue
        │
        └── Wrong ──→ 🔔
                         │
                         ▼
                   Highlight word
```

That gives you a much better UX:

> The user makes a mistake → **immediate tone** → user realizes where they deviated.

rather than:

> User recites 30 seconds → waits → gets 72%.

---

# One major improvement for Quran specifically

There is actually a potentially **much better dataset/model strategy** than using generic Arabic ASR.

There is now a Quran-specific ASR corpus on Hugging Face called **Quran-Ayah-Corpus**, containing Quranic ayahs paired with recitation audio from multiple reciters. It is explicitly intended for Quranic ASR and contains hundreds of thousands of samples. ([Hugging Face][6])

That means the eventual architecture could become:

```text
             Moonshine Arabic Tiny
                      │
                      ▼
             Initial transcription
                      │
                      ▼
          Quran-specific correction
                      │
                      ▼
               Quran corpus
                      │
                      ▼
            Memorization alignment
```

And eventually you could **fine-tune Moonshine Arabic Tiny on Quranic recitation**.

That could be substantially better for this application because Quranic recitation has characteristics that ordinary Arabic speech models aren't necessarily optimized for: tajweed, elongated vowels, recitation cadence, classical Arabic pronunciation, etc. The Quran-Ayah-Corpus dataset specifically describes itself as being designed around Quranic recitation and ASR. ([Hugging Face][6])

### The long-term architecture I'd recommend

```text
                     ┌───────────────────┐
                     │   Vue Web Client  │
                     └─────────┬─────────┘
                               │
                               │ REST/WebSocket
                               ▼
                     ┌───────────────────┐
                     │     FastAPI       │
                     │       API         │
                     └─────────┬─────────┘
                               │
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
           Quran Service   STT Service   Session Service
                 │             │             │
                 ▼             ▼             ▼
             Quran DB      Moonshine      Progress DB
                               │
                               ▼
                     Quran ASR correction
                               │
                               ▼
                      Alignment Engine
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
              Correct words         Wrong words
                    │                     │
                    └──────────┬──────────┘
                               ▼
                      Memorization Score
                               │
                               ▼
                       🔔 Real-time alert
```

And because the STT component is behind:

```python
class SpeechRecognizer:
    def transcribe(...):
        ...
```

you can later replace it with:

```text
MoonshineTinyRecognizer
        ↓
MoonshineBaseRecognizer
        ↓
FineTunedQuranMoonshine
        ↓
WhisperRecognizer
        ↓
Flutter native recognizer
```

without rewriting the rest of the application.

That separation is particularly useful for your eventual **Flutter client**, because Flutter should only need to know about endpoints such as:

```text
GET  /api/quran/surahs
GET  /api/quran/surahs/{id}
GET  /api/quran/surahs/{id}/ayahs/{id}

POST /api/memorization/assess

GET  /api/sessions/{id}
GET  /api/progress
```

rather than knowing anything about Python, Transformers, Moonshine, or the Quran corpus.

[1]: https://github.com/moonshine-ai/moonshine/blob/main/README.md?utm_source=chatgpt.com "moonshine/README.md at main · moonshine-ai/moonshine · GitHub"
[2]: https://huggingface.co/datasets/quranlab/quran?utm_source=chatgpt.com "quranlab/quran · Datasets at Hugging Face"
[3]: https://huggingface.co/UsefulSensors/moonshine-tiny-ar/tree/main?utm_source=chatgpt.com "UsefulSensors/moonshine-tiny-ar at main"
[4]: https://huggingface.co/datasets/arbml/quran_uthmani?utm_source=chatgpt.com "arbml/quran_uthmani · Datasets at Hugging Face"
[5]: https://github.com/mustafa0x/quran-text?utm_source=chatgpt.com "GitHub - nuqayah/quran-text: The Qurʾan’s text · GitHub"
[6]: https://huggingface.co/datasets/rabah2026/Quran-Ayah-Corpus?utm_source=chatgpt.com "rabah2026/Quran-Ayah-Corpus · Datasets at Hugging Face"
