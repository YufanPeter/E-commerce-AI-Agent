#!/usr/bin/env bash
#
# 一键构建后端运行所需的 backend/storage 离线产物。
#
# 用法：
#   ./scripts/build_storage.sh
#   ./scripts/build_storage.sh --force              # image/compare 索引也全量重建
#   ./scripts/build_storage.sh --skip-image-index   # 跳过拍照找货视觉索引
#   ./scripts/build_storage.sh --skip-compare-index # 跳过商品对比索引
#
# 产物：
#   backend/storage/ecommerce_agent.sqlite3
#   backend/storage/chroma/
#   backend/storage/image_index_dashscope.json（百炼主索引）
#   backend/storage/image_index.json（豆包 fallback，保留已有产物）
#   backend/storage/compare_index.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
STORAGE_DIR="${BACKEND_DIR}/storage"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PY="${VENV_DIR}/bin/python"

FORCE=0
SKIP_IMAGE_INDEX=0
SKIP_COMPARE_INDEX=0
IMAGE_WORKERS="${IMAGE_WORKERS:-8}"
COMPARE_WORKERS="${COMPARE_WORKERS:-6}"

log()  { printf '\033[0;36m[build_storage]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[build_storage]\033[0m %s\n' "$*"; }
err()  { printf '\033[0;31m[build_storage]\033[0m %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
Usage:
  ./scripts/build_storage.sh
  ./scripts/build_storage.sh --force
  ./scripts/build_storage.sh --skip-image-index
  ./scripts/build_storage.sh --skip-compare-index
  ./scripts/build_storage.sh --image-workers 4 --compare-workers 2

Outputs:
  backend/storage/ecommerce_agent.sqlite3
  backend/storage/chroma/
  backend/storage/image_index_dashscope.json
  backend/storage/image_index.json
  backend/storage/compare_index.json
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force)
      FORCE=1
      ;;
    --skip-image-index)
      SKIP_IMAGE_INDEX=1
      ;;
    --skip-compare-index)
      SKIP_COMPARE_INDEX=1
      ;;
    --image-workers)
      IMAGE_WORKERS="${2:?--image-workers requires a value}"
      shift
      ;;
    --compare-workers)
      COMPARE_WORKERS="${2:?--compare-workers requires a value}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "未知参数：$1"
      usage >&2
      exit 2
      ;;
  esac
  shift
done

load_env() {
  if [ -f "${REPO_ROOT}/.env" ]; then
    log "加载 .env"
    set -a
    # shellcheck disable=SC1091
    . "${REPO_ROOT}/.env"
    set +a
  else
    warn "未找到 .env；需要调用 embedding/LLM 的构建步骤可能失败。"
  fi
}

ensure_python_env() {
  if ! command -v python3 >/dev/null 2>&1; then
    err "未找到 python3，请先安装 Python 3.10+。"
    exit 1
  fi

  if [ ! -x "${VENV_PY}" ]; then
    log "创建虚拟环境：${VENV_DIR}"
    if ! python3 -m venv "${VENV_DIR}"; then
      err "创建 .venv 失败。Debian/Ubuntu 上通常需要先安装 python3-venv / python3-pip。"
      err "示例：apt-get update && apt-get install -y python3-venv python3-pip"
      exit 1
    fi
  fi

  if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
    if ! "${VENV_PY}" -m ensurepip --upgrade >/dev/null 2>&1; then
      err ".venv 内没有 pip，且 ensurepip 不可用。请安装 python3-venv / python3-pip 后删除 .venv 重试。"
      exit 1
    fi
  fi

  log "安装/更新后端依赖"
  "${VENV_PY}" -m pip install --quiet --upgrade pip
  "${VENV_PY}" -m pip install --quiet -r "${BACKEND_DIR}/requirements.txt"
}

run_step() {
  local title="$1"
  shift
  log "${title}"
  "$@"
}

verify_outputs() {
  log "校验 storage 产物"
  local missing=0
  for path in \
    "${STORAGE_DIR}/ecommerce_agent.sqlite3" \
    "${STORAGE_DIR}/chroma/chroma.sqlite3"; do
    if [ ! -s "${path}" ]; then
      err "缺少或为空：${path}"
      missing=1
    fi
  done

  if [ "${SKIP_IMAGE_INDEX}" != "1" ] && [ ! -s "${STORAGE_DIR}/image_index_dashscope.json" ]; then
    err "缺少或为空：${STORAGE_DIR}/image_index_dashscope.json"
    missing=1
  fi

  if [ "${SKIP_COMPARE_INDEX}" != "1" ] && [ ! -s "${STORAGE_DIR}/compare_index.json" ]; then
    err "缺少或为空：${STORAGE_DIR}/compare_index.json"
    missing=1
  fi

  if [ "${missing}" = "1" ]; then
    exit 1
  fi

  ls -lh \
    "${STORAGE_DIR}/ecommerce_agent.sqlite3" \
    "${STORAGE_DIR}/chroma/chroma.sqlite3" \
    "${STORAGE_DIR}/image_index_dashscope.json" \
    "${STORAGE_DIR}/image_index.json" \
    "${STORAGE_DIR}/compare_index.json" 2>/dev/null || true
}

main() {
  load_env
  ensure_python_env
  mkdir -p "${STORAGE_DIR}"

  cd "${BACKEND_DIR}"

  run_step "1/5 导入商品 JSON 到 SQLite" \
    "${VENV_PY}" -m store.import_product_data --reset

  run_step "2/5 导入图片 manifest 到 SQLite" \
    "${VENV_PY}" -m store.import_image_manifest

  run_step "3/5 构建 Chroma 文本 RAG 索引" \
    "${VENV_PY}" -m rag.build_chroma --reset

  if [ "${SKIP_IMAGE_INDEX}" = "1" ]; then
    warn "跳过 4/5 百炼视觉索引 image_index_dashscope.json"
  else
    image_args=(--workers "${IMAGE_WORKERS}")
    if [ "${FORCE}" = "1" ]; then
      image_args=(--force "${image_args[@]}")
    fi
    run_step "4/5 构建百炼视觉索引 image_index_dashscope.json" \
      "${VENV_PY}" -m rag.build_image_index "${image_args[@]}"
  fi

  if [ "${SKIP_COMPARE_INDEX}" = "1" ]; then
    warn "跳过 5/5 对比索引 compare_index.json"
  else
    compare_args=(--workers "${COMPARE_WORKERS}")
    if [ "${FORCE}" = "1" ]; then
      compare_args=(--force "${compare_args[@]}")
    fi
    run_step "5/5 构建对比索引 compare_index.json" \
      "${VENV_PY}" -m rag.build_compare_index "${compare_args[@]}"
  fi

  verify_outputs
  log "storage 构建完成"
}

main "$@"
