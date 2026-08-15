# Build images for a local cluster (kind / minikube / k3d), then apply manifests.
#
# kind example:
#   docker build -t quran-memorization-backend:latest -f backend/Dockerfile .
#   docker build -t quran-memorization-frontend:latest ./frontend
#   kind load docker-image quran-memorization-backend:latest
#   kind load docker-image quran-memorization-frontend:latest
#   kubectl apply -f k8s/deploy.yaml
#   kubectl -n quran-memorization rollout status deploy/backend
#
# Persistence:
#   - PVC `quran-data` (1Gi) — Uthmani corpus JSON
#   - PVC `hf-model-cache` (5Gi, RWO) — Tarteel Whisper Tiny AR Quran (~150–160 MB)
#     at /models/huggingface. Label purpose: stt-model-cache.
#
# The backend pod runs initContainer `prefetch-stt-model` first. On a cold PVC it
# downloads ~150–160MB from Hugging Face; later pod starts reuse the volume and skip
# the download. A volume that only holds leftover Moonshine blobs will fetch Tarteel
# into the same 5Gi claim (enough for both). Keep backend replicas=1 while the
# claim is ReadWriteOnce.
#
# Wipe the STT cache only if you want to reclaim Moonshine bytes:
#   kubectl -n quran-memorization delete pvc hf-model-cache
#   kubectl apply -f k8s/deploy.yaml
#
# Point /etc/hosts (or your DNS) at the ingress IP for quran.local,
# or use: kubectl -n quran-memorization port-forward svc/frontend 8080:80
#
# WebSocket: Ingress and frontend nginx both enable Upgrade/Connection for
# /api/memorization/stream. Keep backend replicas=1 (or sticky sessions)
# while session state is in-memory.
