#!/usr/bin/env bash
#
# Local development mode with automatic reload after Python changes.
#
# Usage:
#   ./scripts/dev.sh                # Hot reload on 127.0.0.1:8000 by default
#   HOST=0.0.0.0 ./scripts/dev.sh   # Listen on the LAN for a physical iOS device
#
# For iOS UI development, open GuideView.swift in Xcode and select a Canvas preview.
# Saving SwiftUI changes refreshes the preview without a third-party injection package.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

printf '\033[0;36m[dev]\033[0m Development mode: Uvicorn reloads after Python changes.\n'
printf '\033[0;36m[dev]\033[0m iOS UI：Xcode Canvas Preview（GuideView → #Preview）\n\n'

export DEV=1
export FORCE_RESTART=1

EXTRA_ARGS=()
if [ "$#" -gt 0 ]; then
  EXTRA_ARGS=("$@")
fi

if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
  exec "${SCRIPT_DIR}/start_backend.sh" --reload "${EXTRA_ARGS[@]}"
else
  exec "${SCRIPT_DIR}/start_backend.sh" --reload
fi
