#!/usr/bin/env bash
#
# Build the Docker image locally and deploy it to the configured ECS host.
#
# Usage:
#   ./scripts/deploy_to_server.sh
#   IMAGE_TAG=v1 ./scripts/deploy_to_server.sh
#   SKIP_BUILD=1 ./scripts/deploy_to_server.sh   # Skip the build; sync Compose configuration and restart
#
# Environment variables:
#   REMOTE_HOST   Defaults to 118.196.64.197
#   REMOTE_USER   Defaults to root
#   SSH_KEY       Defaults to ~/ecomm.pem
#   REMOTE_DIR    Defaults to /opt/ecommerce-ai-agent
#   IMAGE_NAME    Defaults to cartpilot-backend
#   IMAGE_TAG     Defaults to latest
#   SKIP_BUILD    Set to 1 to skip Docker build, save, and load

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-118.196.64.197}"
REMOTE_USER="${REMOTE_USER:-root}"
SSH_KEY="${SSH_KEY:-${HOME}/ecomm.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/ecommerce-ai-agent}"
IMAGE_NAME="${IMAGE_NAME:-cartpilot-backend}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
SKIP_BUILD="${SKIP_BUILD:-0}"
BUILD_ON_REMOTE="${BUILD_ON_REMOTE:-0}"
COMPOSE_FILE="docker-compose.prod.yml"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FULL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"

SSH_OPTS=(-o StrictHostKeyChecking=no -i "${SSH_KEY}")
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

log()  { printf '\033[0;36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[deploy]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[deploy]\033[0m %s\n' "$*" >&2; }

ensure_docker_mirror() {
  local target="$1"
  ssh "${SSH_OPTS[@]}" "${target}" 'set -eu
    mkdir -p /etc/docker
    if [ ! -f /etc/docker/daemon.json ]; then
      cat > /etc/docker/daemon.json <<EOF
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.ketches.cn"
  ]
}
EOF
      systemctl restart docker
      sleep 2
    fi
  '
}

if [ ! -f "${SSH_KEY}" ]; then
  err "SSH key not found: ${SSH_KEY}"
  exit 1
fi

if [ ! -f "${REPO_ROOT}/.env" ]; then
  err "${REPO_ROOT}/.env not found; configure API credentials first"
  exit 1
fi

if [ "${SKIP_BUILD}" != "1" ] && [ "${BUILD_ON_REMOTE}" != "1" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    err "Docker is not installed locally"
    exit 1
  fi
  log "Building image ${FULL_IMAGE}..."
  if ! docker build -f "${REPO_ROOT}/deploy/Dockerfile" -t "${FULL_IMAGE}" "${REPO_ROOT}"; then
    warn "Local build failed; switching to a remote build..."
    BUILD_ON_REMOTE=1
  fi
fi

log "Checking the remote Docker environment..."
ensure_docker_mirror "${REMOTE}"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "set -eu
  if ! command -v docker >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2
    systemctl enable --now docker
  fi
  mkdir -p '${REMOTE_DIR}'
"

log "Synchronizing Compose configuration and .env..."
scp "${SSH_OPTS[@]}" "${REPO_ROOT}/deploy/docker-compose.prod.yml" "${REMOTE}:${REMOTE_DIR}/${COMPOSE_FILE}"
scp "${SSH_OPTS[@]}" "${REPO_ROOT}/.env" "${REMOTE}:${REMOTE_DIR}/.env"

if [ "${SKIP_BUILD}" != "1" ] && [ "${BUILD_ON_REMOTE}" = "1" ]; then
  log "Synchronizing source and building ${FULL_IMAGE} remotely..."
  rsync -az --delete \
    --exclude '.git/' --exclude '.venv/' --exclude 'client/' --exclude 'docs/' \
    --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.DS_Store' \
    -e "ssh -o StrictHostKeyChecking=no -i ${SSH_KEY}" \
    "${REPO_ROOT}/" "${REMOTE}:${REMOTE_DIR}/build-context/"
  ssh "${SSH_OPTS[@]}" "${REMOTE}" "set -eu
    cd '${REMOTE_DIR}/build-context'
    docker build -f deploy/Dockerfile -t '${FULL_IMAGE}' .
  "
elif [ "${SKIP_BUILD}" != "1" ]; then
  log "Transferring the image to the remote host; this may take a while..."
  docker save "${FULL_IMAGE}" | gzip | ssh "${SSH_OPTS[@]}" "${REMOTE}" "gunzip | docker load"
fi

log "Starting containers..."
ssh "${SSH_OPTS[@]}" "${REMOTE}" "set -eu
  cd '${REMOTE_DIR}'
  IMAGE_REF='${FULL_IMAGE}' docker compose -f '${COMPOSE_FILE}' up -d --remove-orphans
"

log "Waiting for /health..."
for _ in $(seq 1 40); do
  if ssh "${SSH_OPTS[@]}" "${REMOTE}" "curl -fsS --max-time 5 http://127.0.0.1:8000/health" 2>/dev/null; then
    printf '\n'
    log "Deployment succeeded: http://${REMOTE_HOST}:8000/health"
    exit 0
  fi
  sleep 3
done

warn "Health check timed out. Remote logs:"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "cd '${REMOTE_DIR}' && docker compose -f '${COMPOSE_FILE}' logs --tail=100 backend" || true
exit 1
