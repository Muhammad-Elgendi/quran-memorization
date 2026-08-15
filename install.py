"""Local install helper: deps + Quran corpus (+ optional model prefetch)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(command: list[str]) -> None:
    print("\n>>>", " ".join(command))
    subprocess.check_call(command, cwd=ROOT)


def main() -> None:
    print("Installing Quran Memorization Assistant...")

    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "backend" / "requirements.txt"),
        ]
    )

    print("\nDownloading Quran corpus...")
    run([sys.executable, str(ROOT / "backend" / "download_quran.py")])

    print("\nPrefetching Tarteel Whisper Tiny AR Quran into the Hugging Face cache...")
    try:
        run([sys.executable, str(ROOT / "backend" / "prefetch_model.py")])
    except Exception as exc:  # noqa: BLE001
        print(f"Model prefetch skipped ({exc}). It will download on first assess.")

    print("\nInstallation complete.")
    print("\nStart backend:")
    print("  cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload")
    print("\nStart frontend:")
    print("  cd frontend && npm install && npm run dev")
    print("\nOr with Docker Compose:")
    print("  docker compose up --build")


if __name__ == "__main__":
    main()
