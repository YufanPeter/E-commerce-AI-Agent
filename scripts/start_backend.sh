#!/usr/bin/env bash
#
# 一键启动后端 REST API（FastAPI / uvicorn）。
#
# 用法：
#   ./scripts/start_backend.sh                  # 本机 venv 前台启动，Ctrl+C 退出
#   ./scripts/start_backend.sh --docker         # Docker Compose 前台启动
#   BACKEND_RUNTIME=docker ./scripts/start_backend.sh -d  # Docker 后台启动
#   HOST=0.0.0.0 ./scripts/start_backend.sh     # 监听局域网，供 iOS 真机连接
#   PORT=8000 ./scripts/start_backend.sh        # 自定义端口
#   USE_RERANK=0 ./scripts/start_backend.sh     # 禁用 API 精排（链路只走向量召回排序）
#   ./scripts/start_backend.sh --reload         # venv 模式：开发热重载（额外参数透传给 uvicorn）
#
# 脚本会自动：
#   1. 定位/创建 .venv 并按需安装依赖
#   2. 检查 storage 数据（sqlite + chroma）是否就绪
#   3. 若目标端口已被占用则提示并退出
#   4. 启动 uvicorn 并轮询 /health 直到服务就绪

set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
BACKEND_RUNTIME="${BACKEND_RUNTIME:-venv}"

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
      printf '\033[0;32m[start_backend]\033[0m 后端就绪 ✅  http://%s:%s\n' "${health_host}" "${health_port}"
      return 0
    fi
    sleep 1
  done
  warn "60 秒内未等到 /health 就绪；服务可能仍在加载模型或初始化索引。"
  return 1
}

if [ "${BACKEND_RUNTIME}" = "docker" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    err "未找到 docker，请先安装 Docker Desktop 或 Docker Engine。"
    exit 1
  fi
  if [ ! -f "${COMPOSE_FILE}" ]; then
    err "未找到 Docker Compose 文件：${COMPOSE_FILE}"
    exit 1
  fi
  if [ ! -f "${REPO_ROOT}/.env" ]; then
    warn "未找到 .env；compose 会继续启动，但豆包/embedding 密钥缺失时对话会失败。"
  fi

  log "Docker Compose 启动后端：http://${HOST}:${PORT}"
  if [ "${PORT}" != "8000" ]; then
    warn "当前 compose 文件默认映射 8000:8000；PORT=${PORT} 只用于健康检查地址。"
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
  log "未找到 .venv，正在创建虚拟环境…"
  python3 -m venv "${VENV_DIR}"
  log "安装依赖（backend/requirements.txt）…"
  "${VENV_PY}" -m pip install --quiet --upgrade pip
  "${VENV_PY}" -m pip install --quiet -r "${BACKEND_DIR}/requirements.txt"
else
  # venv 已存在，确认核心依赖在位；缺失则补装
  if ! "${VENV_PY}" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
    warn "检测到依赖缺失，正在安装 backend/requirements.txt…"
    "${VENV_PY}" -m pip install --quiet -r "${BACKEND_DIR}/requirements.txt"
  fi
fi

# 2) storage ------------------------------------------------------------------
SQLITE_DB="${BACKEND_DIR}/storage/ecommerce_agent.sqlite3"
CHROMA_DIR="${BACKEND_DIR}/storage/chroma"
if [ ! -f "${SQLITE_DB}" ]; then
  warn "未找到 SQLite 商品库：${SQLITE_DB}"
  warn "请先构建：cd backend && python -m store.import_product_data --reset && python -m store.import_image_manifest"
fi
if [ ! -d "${CHROMA_DIR}" ]; then
  warn "未找到 Chroma 索引：${CHROMA_DIR}"
  warn "请先构建：cd backend && python -m rag.build_chroma --reset"
fi

# 3) 端口占用检查 --------------------------------------------------------------
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  EXISTING_PID="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  warn "端口 ${PORT} 已被占用（PID ${EXISTING_PID}），后端可能已在运行。"
  if curl -s -m 3 "http://${HOST}:${PORT}/health" | grep -q '"ok"'; then
    log "现有服务健康检查通过：http://${HOST}:${PORT}/health"
    exit 0
  fi
  err "端口被占用但 /health 无响应，请手动停止该进程后重试：kill ${EXISTING_PID}"
  exit 1
fi

# 4) 启动 + 健康检查 -----------------------------------------------------------
log "启动 uvicorn：http://${HOST}:${PORT}  (workdir=${BACKEND_DIR})"

(
  wait_for_health "${HOST}" "${PORT}" || true
  if [ "${HOST}" = "0.0.0.0" ]; then
    LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -n "${LAN_IP}" ] && printf '\033[0;32m[start_backend]\033[0m iOS 真机请将客户端 baseURL 指向：http://%s:%s\n' "${LAN_IP}" "${PORT}"
  fi
) &

cd "${BACKEND_DIR}"
exec "${VENV_PY}" -m uvicorn api.main:app --host "${HOST}" --port "${PORT}" "$@"
