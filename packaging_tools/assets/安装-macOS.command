#!/bin/sh
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  python3 install_skill.py
elif command -v python >/dev/null 2>&1; then
  python install_skill.py
else
  echo "安装失败：未找到 Python 3。请先安装 Python 3 后重试。"
  status=1
fi

status=${status:-$?}
echo
printf "按回车键关闭窗口..."
read -r _answer
exit "$status"
