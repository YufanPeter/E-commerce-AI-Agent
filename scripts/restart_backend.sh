#!/usr/bin/env bash
#
# Restart the development backend with hot reload.
#
# Usage:
#   ./scripts/restart_backend.sh
#   HOST=0.0.0.0 ./scripts/restart_backend.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/stop_backend.sh" || true
exec "${SCRIPT_DIR}/dev.sh" "$@"
