#!/usr/bin/env bash
#
# 重启开发后端：先停再启（带热重载）。
#
# 用法：
#   ./scripts/restart_backend.sh
#   HOST=0.0.0.0 ./scripts/restart_backend.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/stop_backend.sh" || true
exec "${SCRIPT_DIR}/dev.sh" "$@"
