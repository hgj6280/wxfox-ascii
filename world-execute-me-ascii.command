#!/bin/zsh
# 双击即可在 macOS「终端」中播放；也可拖到终端窗口里回车。
set -eu
DIR=${0:A:h}
cd "$DIR"
exec python3 wxfox.py "$@"
