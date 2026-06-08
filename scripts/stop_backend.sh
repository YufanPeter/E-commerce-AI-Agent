#!/usr/bin/env bash
#
# 停止本机占用后端端口的进程（默认 8000）。
#
# 用法：
#   ./scripts/stop_backend.sh
#   PORT=8000 ./scripts/stop_backend.sh

set -euo pipefail

PORT="${PORT:-8000}"

log()  { printf '\033[0;36m[stop_backend]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[stop_backend]\033[0m %s\n' "$*"; }

if ! lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  log "端口 ${PORT} 无监听进程，无需停止。"
  exit 0
fi

PIDS="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"
if [ -z "${PIDS}" ]; then
  log "端口 ${PORT} 无监听进程，无需停止。"
  exit 0
fi

for pid in ${PIDS}; do
  warn "停止 PID ${pid}（端口 ${PORT}）…"
  kill "${pid}" 2>/dev/null || true
done

sleep 0.5
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "进程未退出，发送 SIGKILL…"
  for pid in ${PIDS}; do
    kill -9 "${pid}" 2>/dev/null || true
  done
fi

if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "端口 ${PORT} 仍被占用，请手动检查。"
  exit 1
fi

log "后端已停止 ✅"
