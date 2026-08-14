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
  python prefetch_model.py
fi

exec "$@"
