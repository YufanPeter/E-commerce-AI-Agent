#!/usr/bin/env bash
# Build the versioned Amazon catalog artifacts from amazon_dense_10k/.
# An existing database is never overwritten; pass another CARTPILOT_AMAZON_DB to rebuild.
# The index step calls the paid embedding API and skips vectors that already exist.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python"
if [ ! -x "$python" ]; then
  echo "Set up the Python environment first (see README.md)." >&2
  exit 1
fi
cd "$repo/backend"
export PYTHONPATH="$repo/backend${PYTHONPATH:+:$PYTHONPATH}"
"$python" -m store.import_amazon "$@"
"$python" -m rag.build_catalog_index
