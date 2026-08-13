#!/bin/sh
set -eu

cd /app/backend

if [ ! -f data/quran.json ]; then
  echo "Quran corpus missing — downloading..."
  python download_quran.py
else
  # Fix known gaps in volumes created before the Fatihah ayah-1 patch.
  python download_quran.py --repair
fi

if [ "${PREFETCH_MODEL:-0}" = "1" ]; then
  echo "Prefetching speech model..."
  python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(repo_id="UsefulSensors/moonshine-tiny-ar")
print("Model ready.")
PY
fi

exec "$@"
