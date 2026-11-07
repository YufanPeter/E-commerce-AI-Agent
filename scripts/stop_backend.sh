#!/usr/bin/env bash
#
# Stop the local process using the backend port (8000 by default).
#
# Usage:
#   ./scripts/stop_backend.sh
#   PORT=8000 ./scripts/stop_backend.sh

set -euo pipefail

PORT="${PORT:-8000}"

log()  { printf '\033[0;36m[stop_backend]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[stop_backend]\033[0m %s\n' "$*"; }

if ! lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  log "No process is listening on port ${PORT}; nothing to stop."
  exit 0
fi

PIDS="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"
if [ -z "${PIDS}" ]; then
  log "No process is listening on port ${PORT}; nothing to stop."
  exit 0
fi

for pid in ${PIDS}; do
  warn "Stopping PID ${pid} on port ${PORT}..."
  kill "${pid}" 2>/dev/null || true
done

sleep 0.5
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "The process did not exit; sending SIGKILL..."
  for pid in ${PIDS}; do
    kill -9 "${pid}" 2>/dev/null || true
  done
fi

if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "Port ${PORT} is still in use; inspect it manually."
  exit 1
fi

log "Backend stopped."
