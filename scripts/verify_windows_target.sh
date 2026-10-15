#!/usr/bin/env bash
# 交叉检查 Windows 目标的编译。
#
# 为什么需要：常规门禁（check:tauri / check:full）只在**宿主目标**上 cargo check，
# `#[cfg(windows)]` 的代码在 macOS / Linux 上根本不参与编译，而 Windows 安装包由
# CI 手动触发——Windows 专用代码漏编译、类型错误、以及只在运行期发作的问题，都不会
# 在本地门禁里出现。2026-10-15 修「Windows 下载不弹保存对话框」时确认了这个盲区。
#
# 用法：scripts/verify_windows_target.sh [target]（默认 x86_64-pc-windows-gnu）
# 依赖：rustup target add x86_64-pc-windows-gnu
#       macOS 上 C 依赖需要 mingw-w64（brew install mingw-w64）
set -euo pipefail

TARGET="${1:-x86_64-pc-windows-gnu}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! rustup target list --installed 2>/dev/null | grep -qx "${TARGET}"; then
  printf '[windows-target] 缺少目标 %s：先执行 rustup target add %s\n' \
    "${TARGET}" "${TARGET}" >&2
  exit 1
fi

cd "${REPO_ROOT}/client/src-tauri"
printf '[windows-target] cargo check --target %s --locked\n' "${TARGET}"
cargo check --target "${TARGET}" --locked
printf '[windows-target] OK：Windows 专用代码通过类型检查\n'
