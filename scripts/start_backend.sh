#!/usr/bin/env bash
#
# Start the backend REST API with FastAPI and Uvicorn.
#
# Usage:
#   ./scripts/start_backend.sh                  # Start the local venv in the foreground; Ctrl+C to stop
#   ./scripts/start_backend.sh --docker         # Start Docker Compose in the foreground
#   BACKEND_RUNTIME=docker ./scripts/start_backend.sh -d  # Start Docker in the background
#   HOST=0.0.0.0 ./scripts/start_backend.sh     # Listen on the LAN for a physical iOS device
#   PORT=8000 ./scripts/start_backend.sh        # Use a custom port
#   USE_RERANK=0 ./scripts/start_backend.sh     # Disable API reranking and keep vector-retrieval order
#   BACKEND_WARMUP=0 ./scripts/start_backend.sh # Skip background warmup; startup is quieter but the first query is slower
#   ./scripts/start_backend.sh --reload         # Enable development reload in venv mode; extra arguments pass to Uvicorn
#   ./scripts/dev.sh                            # Recommended development mode with reload and stale-process cleanup
#   ./scripts/stop_backend.sh                   # Stop the process using the backend port
#   ./scripts/restart_backend.sh                # Stop and restart in development mode
#
# The script automatically:
#   1. Locates or creates `.venv` and installs dependencies when needed.
#   2. Checks that SQLite and Chroma storage are ready.
#   3. Reports an occupied target port and exits.
#   4. Starts Uvicorn and polls `/health` until the service is ready.

set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
BACKEND_RUNTIME="${BACKEND_RUNTIME:-venv}"
export BACKEND_WARMUP="${BACKEND_WARMUP:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.yml"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PY="${VENV_DIR}/bin/python"

log()  { printf '\033[0;36m[start_backend]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[start_backend]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[start_backend]\033[0m %s\n' "$*" >&2; }

if [ "${1:-}" = "--docker" ]; then
  BACKEND_RUNTIME="docker"
  shift
fi

wait_for_health() {
  local health_host="$1"
  local health_port="$2"
  for _ in $(seq 1 60); do
    if curl -s -m 2 "http://${health_host}:${health_port}/health" | grep -q '"ok"'; then
      printf '\033[0;32m[start_backend]\033[0m Backend ready: http://%s:%s\n' "${health_host}" "${health_port}"
      return 0
    fi
    sleep 1
  done
  warn "/health did not become ready within 60 seconds; models or indexes may still be loading."
  return 1
}

if [ "${BACKEND_RUNTIME}" = "docker" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    err "Docker was not found. Install Docker Desktop or Docker Engine first."
    exit 1
  fi
  if [ ! -f "${COMPOSE_FILE}" ]; then
    err "Docker Compose file not found: ${COMPOSE_FILE}"
    exit 1
  fi
  if [ ! -f "${REPO_ROOT}/.env" ]; then
    warn ".env was not found. Compose will continue, but model-dependent requests may fail without credentials."
  fi

  log "Starting the backend with Docker Compose at http://${HOST}:${PORT}"
  if [ "${PORT}" != "8000" ]; then
    warn "The current Compose file maps 8000:8000; PORT=${PORT} affects only the health-check address."
  fi

  if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--detach" ]; then
    docker compose -f "${COMPOSE_FILE}" up -d --build
    wait_for_health "${HOST}" "${PORT}" || true
    exit 0
  fi

  (
    wait_for_health "${HOST}" "${PORT}" || true
  ) &
  exec docker compose -f "${COMPOSE_FILE}" up --build "$@"
fi

# 1) venv ---------------------------------------------------------------------
if [ ! -x "${VENV_PY}" ]; then
  log ".venv not found; creating a virtual environment..."
  python3 -m venv "${VENV_DIR}"
  log "Installing dependencies from backend/requirements.txt..."
  "${VENV_PY}" -m pip install --quiet --upgrade pip
  "${VENV_PY}" -m pip install --quiet -r "${BACKEND_DIR}/requirements.txt"
else
  # Verify core dependencies in an existing venv and install any that are missing.
  if ! "${VENV_PY}" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
    warn "Missing dependencies detected; installing backend/requirements.txt..."
    "${VENV_PY}" -m pip install --quiet -r "${BACKEND_DIR}/requirements.txt"
  fi
fi

# 2) storage ------------------------------------------------------------------
SQLITE_DB="${BACKEND_DIR}/storage/ecommerce_agent.sqlite3"
CHROMA_DIR="${BACKEND_DIR}/storage/chroma"
if [ ! -f "${SQLITE_DB}" ]; then
  warn "SQLite product store not found: ${SQLITE_DB}"
  warn "Build it first: cd backend && python -m store.import_product_data --reset && python -m store.import_image_manifest"
fi
if [ ! -d "${CHROMA_DIR}" ]; then
  warn "Chroma index not found: ${CHROMA_DIR}"
  warn "Build it first: cd backend && python -m rag.build_chroma --reset"
fi

# 3) Port availability check ----------------------------------------------------
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  EXISTING_PID="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  if [ "${FORCE_RESTART:-0}" = "1" ]; then
    warn "Development mode: stopping stale PID ${EXISTING_PID} on port ${PORT}..."
    kill "${EXISTING_PID}" 2>/dev/null || true
    sleep 0.5
    if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
      err "Could not release port ${PORT}; stop PID ${EXISTING_PID} manually"
      exit 1
    fi
  else
    warn "Port ${PORT} is already used by PID ${EXISTING_PID}; the backend may already be running."
    if curl -s -m 3 "http://${HOST}:${PORT}/health" | grep -q '"ok"'; then
      log "The existing service passed its health check: http://${HOST}:${PORT}/health"
      if [ "${DEV:-0}" = "1" ]; then
        warn "For hot reload, use ./scripts/dev.sh or FORCE_RESTART=1 ./scripts/start_backend.sh --reload"
      fi
      exit 0
    fi
    err "The port is occupied but /health is unresponsive. Stop the process and retry: kill ${EXISTING_PID}"
    exit 1
  fi
fi

# Enable development reload when DEV=1 unless `--reload` was provided explicitly.
# With macOS Bash 3.2 and `set -u`, an empty "${arr[@]}" raises an unbound-variable error and must be initialized explicitly.
UVICORN_ARGS=()
if [ "$#" -gt 0 ]; then
  UVICORN_ARGS=("$@")
fi
if [ "${DEV:-0}" = "1" ]; then
  has_reload=0
  if [ "${#UVICORN_ARGS[@]}" -gt 0 ]; then
    for arg in "${UVICORN_ARGS[@]}"; do
      if [ "$arg" = "--reload" ]; then has_reload=1; break; fi
    done
  fi
  if [ "${has_reload}" -eq 0 ]; then
    if [ "${#UVICORN_ARGS[@]}" -gt 0 ]; then
      UVICORN_ARGS=(--reload "${UVICORN_ARGS[@]}")
    else
      UVICORN_ARGS=(--reload)
    fi
    log "DEV=1: automatically enabled uvicorn --reload"
  fi
fi

# 4) Startup and health check ---------------------------------------------------
log "Starting Uvicorn at http://${HOST}:${PORT} (workdir=${BACKEND_DIR})"
if [ "${BACKEND_WARMUP}" = "1" ] || [ "${BACKEND_WARMUP}" = "true" ]; then
  log "Background warmup enabled: /health becomes ready while Agent and SearchService continue loading"
else
  log "Background warmup disabled: the first real query will lazily load Agent and SearchService"
fi

(
  wait_for_health "${HOST}" "${PORT}" || true
  if [ "${HOST}" = "0.0.0.0" ]; then
    LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -n "${LAN_IP}" ] && printf '\033[0;32m[start_backend]\033[0m For a physical iOS device, set the client base URL to http://%s:%s\n' "${LAN_IP}" "${PORT}"
  fi
) &

cd "${BACKEND_DIR}"
UVICORN_CMD=(
  "${VENV_PY}" -m uvicorn api.main:app
  --host "${HOST}" --port "${PORT}"
)
if [ "${#UVICORN_ARGS[@]}" -gt 0 ]; then
  UVICORN_CMD+=("${UVICORN_ARGS[@]}")
fi
exec "${UVICORN_CMD[@]}"
