#!/usr/bin/env bash
# 启动本地 FastAPI + 生成 Worker,供 Tauri 桌面端自动拉起。
# 服务端分发形态可通过环境变量覆盖;默认按开发布局(uv + server/ 目录)运行。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export VIDEO_REPLICA_DB_PATH="${VIDEO_REPLICA_DB_PATH:?VIDEO_REPLICA_DB_PATH is required}"
export VIDEO_REPLICA_DESKTOP_USER_ID="${VIDEO_REPLICA_DESKTOP_USER_ID:?VIDEO_REPLICA_DESKTOP_USER_ID is required}"
export VIDEO_REPLICA_AUTH_MODE="${VIDEO_REPLICA_AUTH_MODE:-desktop}"
export VIDEO_REPLICA_STORAGE_ROOT="${VIDEO_REPLICA_STORAGE_ROOT:-$SCRIPT_DIR/storage}"

# 打包命令是一个整体，禁止一部分走 sidecar、另一部分误回退到开发目录。
if [[ -n "${VIDEO_REPLICA_BOOTSTRAP_CMD:-}" || -n "${VIDEO_REPLICA_SERVER_CMD:-}" || -n "${VIDEO_REPLICA_WORKER_CMD:-}" ]]; then
  if [[ -z "${VIDEO_REPLICA_BOOTSTRAP_CMD:-}" || -z "${VIDEO_REPLICA_SERVER_CMD:-}" || -z "${VIDEO_REPLICA_WORKER_CMD:-}" ]]; then
    echo "packaged bootstrap, server, and worker commands must be set together" >&2
    exit 1
  fi
fi

# 先迁移旧数据库并验证已保存凭据可解密，再并发启动 API 和 Worker。
if [[ -n "${VIDEO_REPLICA_BOOTSTRAP_CMD:-}" ]]; then
  sh -c "$VIDEO_REPLICA_BOOTSTRAP_CMD"
else
  uv --cache-dir .uv-cache run --project server --locked python -m app.bootstrap
fi

# PyInstaller 等分发形态下,用 BOOTSTRAP_CMD / SERVER_CMD / WORKER_CMD
# 分别指向不依赖开发目录的打包产物。
# 默认命令使用参数数组直接执行；只有受信任的部署覆盖值需要由 shell 解析。
start_server() {
  if [[ -n "${VIDEO_REPLICA_SERVER_CMD:-}" ]]; then
    exec sh -c "$VIDEO_REPLICA_SERVER_CMD"
  fi
  exec uv --cache-dir .uv-cache run --project server --locked \
    python -m uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000
}

start_worker() {
  if [[ -n "${VIDEO_REPLICA_WORKER_CMD:-}" ]]; then
    exec sh -c "$VIDEO_REPLICA_WORKER_CMD"
  fi
  exec uv --cache-dir .uv-cache run --project server --locked \
    python -m app.generation_worker
}

start_server > api.log 2>&1 &
SERVER_PID=$!
start_worker > worker.log 2>&1 &
WORKER_PID=$!

echo "video-replica backend started: api=$SERVER_PID worker=$WORKER_PID"
trap 'kill "$SERVER_PID" "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM
wait
