#!/usr/bin/env bash
# Simulate the Jenkinsfile local-transfer pipeline without a Jenkins instance.
# Usage: ./scripts/jenkins_local_transfer_dryrun.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-118.196.64.197}"
REMOTE_USER="${REMOTE_USER:-root}"
SSH_KEY="${SSH_KEY:-${HOME}/ecomm.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/ecommerce-ai-agent}"
IMAGE_NAME="${IMAGE_NAME:-cartpilot-backend}"
IMAGE_TAG="${IMAGE_TAG:-jenkins-dryrun}"
COMPOSE_FILE="docker-compose.prod.yml"
DEPLOY_PORT="${DEPLOY_PORT:-8000}"
FULL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
BASE_IMAGE="${BASE_IMAGE:-docker.1ms.run/library/python:3.11-slim}"

SSH_OPTS=(-o StrictHostKeyChecking=no -i "${SSH_KEY}")
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

log() { printf '\033[0;36m[jenkins-dryrun]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[jenkins-dryrun]\033[0m %s\n' "$*" >&2; exit 1; }

cd "${REPO_ROOT}"

log "Stage: Test backend"
"${REPO_ROOT}/.venv/bin/python" -m pip install -q -r backend/requirements.txt
(cd backend && "${REPO_ROOT}/.venv/bin/python" -m pytest -q)

log "Stage: Build image (local docker)"
command -v docker >/dev/null 2>&1 || fail "docker is required on the Jenkins node for local-transfer mode"
docker build \
  --build-arg BASE_IMAGE="${BASE_IMAGE}" \
  -f deploy/Dockerfile \
  -t "${FULL_IMAGE}" \
  .
docker save "${FULL_IMAGE}" | gzip > image.tar.gz

log "Stage: Deploy to server"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "mkdir -p '${REMOTE_DIR}'"
scp "${SSH_OPTS[@]}" deploy/docker-compose.prod.yml "${REMOTE}:${REMOTE_DIR}/${COMPOSE_FILE}"
scp "${SSH_OPTS[@]}" image.tar.gz "${REMOTE}:/tmp/cartpilot-image.tar.gz"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "set -eu
  gunzip -c /tmp/cartpilot-image.tar.gz | docker load
  rm -f /tmp/cartpilot-image.tar.gz
  cd '${REMOTE_DIR}'
  test -f .env
  IMAGE_REF='${FULL_IMAGE}' docker compose -f '${COMPOSE_FILE}' up -d --remove-orphans
  for i in \$(seq 1 30); do
    if curl -fsS --max-time 5 http://127.0.0.1:${DEPLOY_PORT}/health; then exit 0; fi
    sleep 2
  done
  docker compose -f '${COMPOSE_FILE}' logs --tail=80 backend || true
  exit 1
"

log "Public health check"
curl -fsS "http://${REMOTE_HOST}:${DEPLOY_PORT}/health"
printf '\n'
log "Jenkins local-transfer dry run OK (${FULL_IMAGE})"
