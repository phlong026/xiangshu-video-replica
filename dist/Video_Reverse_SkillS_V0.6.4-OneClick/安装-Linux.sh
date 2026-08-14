#!/bin/sh
set -eu
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 install_skill.py
fi
if command -v python >/dev/null 2>&1; then
  exec python install_skill.py
fi
echo "安装失败：未找到 Python 3。请先安装 Python 3 后重试。" >&2
exit 1
