#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 凭据目录由 converter.py / checkin.py 自动扫描：
#   WSL ~/.local/share/CodeBuddyExtension/... + Windows 宿主机挂载目录，按账号 uid 去重。

# 启动前自动执行签到领积分（遍历账号池中所有账号）
echo "🎁 正在执行启动时签到与积分领取（所有账号）..."
"$DIR/.venv/bin/python" "$DIR/checkin.py" || true

echo "🚀 启动 workbuddy2api 服务..."
exec "$DIR/.venv/bin/python" "$DIR/converter.py" "$@"
