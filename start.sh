#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 优先探测 Windows 宿主机与 WSL2 的 auth 文件
WIN_AUTH="/mnt/c/Users/winuser/AppData/Local/CodeBuddyExtension/Data/Public/auth"
WSL_AUTH="$HOME/.local/share/CodeBuddyExtension/Data/Public/auth"

if [ -d "$WIN_AUTH" ] && [ "$(ls -A "$WIN_AUTH"/*.info 2>/dev/null)" ]; then
    echo "💡 检测到 Windows 宿主机 CodeBuddy 凭据，使用宿主机凭据目录..."
    export CODEBUDDY_AUTH_DIR="$WIN_AUTH"
elif [ -d "$WSL_AUTH" ] && [ "$(ls -A "$WSL_AUTH"/*.info 2>/dev/null)" ]; then
    echo "💡 检测到本地凭据文件..."
    export CODEBUDDY_AUTH_DIR="$WSL_AUTH"
fi

# 启动前自动执行一次签到领积分
echo "🎁 正在执行启动时签到与积分领取..."
"$DIR/.venv/bin/python" "$DIR/checkin.py" || true

echo "🚀 启动 workbuddy2api 服务..."
exec "$DIR/.venv/bin/python" "$DIR/converter.py" "$@"
