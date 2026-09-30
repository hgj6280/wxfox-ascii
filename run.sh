#!/usr/bin/env sh
# 任意 Unix（Linux/macOS/WSL/ssh）通用启动脚本
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-}
if [ -z "$PY" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then PY=$candidate; break; fi
    done
fi
[ -n "$PY" ] || { echo "找不到 Python 3，请先安装。" >&2; exit 1; }
exec "$PY" "$DIR/wxfox.py" "$@"
