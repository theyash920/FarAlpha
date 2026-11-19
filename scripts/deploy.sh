#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in docker minikube kubectl python3; do
  if ! command -v "$tool" >/dev/null; then
    echo "Install $tool before starting the faralpha Minikube profile." >&2
    exit 1
  fi
done

k() { kubectl --context faralpha --namespace faralpha "$@"; }

minikube start -p faralpha --driver=docker --nodes=1 --cpus=4 --memory=4096 --keep-context
minikube -p faralpha addons enable metrics-server
k apply -f k8s/namespace.yaml

image=${APP_IMAGE:-}
if [[ -z "$image" ]]; then
  # A source change gets a new tag and triggers a normal Deployment rollout.
  revision=$(python3 - <<'PY'
import hashlib
from pathlib import Path
digest = hashlib.sha256()
for name in ("Dockerfile", "requirements.txt", "app.py", ".dockerignore"):
    digest.update(name.encode() + b"\0" + Path(name).read_bytes() + b"\0")
print(digest.hexdigest()[:12])
PY
)
  image="faralpha-flask:$revision"
  minikube -p faralpha image build -t "$image" .
fi

existing_secret=$(k get secret mongodb-credentials --ignore-not-found -o name)
if [[ -z "$existing_secret" ]]; then
  # The image bootstraps accounts once. Replacing a lost password on an initialized disk breaks auth.
  disk_state=$(minikube -p faralpha ssh -- "sudo sh -c 'if test -s /data/faralpha/mongodb/WiredTiger; then echo initialized; else echo fresh; fi'")
  if [[ "$disk_state" == *initialized* ]]; then
    echo "The retained MongoDB disk already has users. Restore the matching mongodb-credentials Secret before redeploying." >&2
    exit 1
  fi
  python3 - <<'PY' | k create -f -
import secrets
from pathlib import Path
from string import Template
template = Template(Path("bootstrap/mongodb-secret.template.yaml").read_text())
print(template.substitute(ROOT_PASSWORD=secrets.token_hex(32), APP_PASSWORD=secrets.token_hex(32)))
PY
fi

k apply -k k8s/database
k apply -f k8s/flask-service.yaml
kubectl set image --local -f k8s/flask-deployment.yaml "flask=$image" -o yaml | k apply -f -
k apply -f k8s/hpa.yaml
k rollout status statefulset/mongodb --timeout=6m
k rollout status deployment/flask --timeout=6m
k wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=3m
k get deployment,statefulset,service,pvc,hpa
echo "Run python3 scripts/verify.py to exercise the API, persistence and autoscaling."
