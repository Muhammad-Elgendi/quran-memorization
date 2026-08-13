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
# Point /etc/hosts (or your DNS) at the ingress IP for quran.local,
# or use: kubectl -n quran-memorization port-forward svc/frontend 8080:80
