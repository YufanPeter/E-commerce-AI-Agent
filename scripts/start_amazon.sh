#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
python="$repo/.venv/bin/python"
if [ ! -x "$python" ]; then
  echo "Set up the Python environment first (see README.md)." >&2
  exit 1
fi
export CARTPILOT_CATALOG=amazon
export PYTHONPATH="$repo/backend${PYTHONPATH:+:$PYTHONPATH}"
export BACKEND_WARMUP="${BACKEND_WARMUP:-0}"
catalog="$("$python" -c 'from catalog_config import AMAZON_DB_PATH; print(AMAZON_DB_PATH)')"
if [ ! -f "$catalog" ]; then
  "$python" -m store.import_amazon
fi
exec "$python" -m uvicorn api.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"
