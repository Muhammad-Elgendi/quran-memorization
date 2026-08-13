"""Audio decode helpers.

Browser MediaRecorder uploads are typically WebM/Opus. Librosa 1.x only
loads via soundfile/libsndfile, which cannot read WebM — convert with ffmpeg
(already installed in the Docker image) before analysis.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import soundfile as sf

logger = logging.getLogger(__name__)


def needs_ffmpeg_conversion(path: str | Path) -> bool:
    try:
        sf.info(str(path))
        return False
    except Exception:
        return True


def convert_to_wav(src: str | Path) -> str:
    """Convert any ffmpeg-readable audio to a mono 16 kHz WAV temp file."""
    src = Path(src)
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    out.close()
    dest = out.name
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        dest,
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        Path(dest).unlink(missing_ok=True)
        raise RuntimeError(
            "ffmpeg is required to decode this audio format"
        ) from exc
    except subprocess.CalledProcessError as exc:
        Path(dest).unlink(missing_ok=True)
        stderr = (exc.stderr or "").strip()
        logger.warning("ffmpeg failed for %s: %s", src, stderr[-500:])
        raise RuntimeError("Could not decode audio file") from exc
    return dest


def prepare_audio(path: str | Path) -> tuple[str, bool]:
    """
    Return a soundfile-readable path.

    Returns (path, owns_file). If owns_file is True, the caller must delete
    the path when finished.
    """
    path = Path(path)
    if not needs_ffmpeg_conversion(path):
        return str(path), False
    return convert_to_wav(path), True
