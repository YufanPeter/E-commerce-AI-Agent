#!/usr/bin/env bash
#
# 本地开发模式：后端改 Python 自动重载（类似 React 的 dev server）。
#
# 用法：
#   ./scripts/dev.sh                # 后端热重载（默认 127.0.0.1:8000）
#   HOST=0.0.0.0 ./scripts/dev.sh   # 局域网，供 iOS 真机
#
# iOS UI 调试：在 Xcode 打开 GuideView.swift → Canvas 里的 #Preview("推荐消息")，
# 改 SwiftUI 代码保存后 Preview 会自动刷新（无需第三方 Inject 包）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

printf '\033[0;36m[dev]\033[0m 开发模式：Python 文件变更后 uvicorn 自动重载\n'
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
