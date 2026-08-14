@echo off
rem 启动本地 FastAPI + 生成 Worker,供 Tauri 桌面端自动拉起。
rem 服务端分发形态可通过环境变量覆盖;默认按开发布局(uv + server/ 目录)运行。
setlocal

if "%VIDEO_REPLICA_DB_PATH%"=="" (
  echo VIDEO_REPLICA_DB_PATH is required
  exit /b 1
)
if "%VIDEO_REPLICA_SETTINGS_KEY%"=="" (
  echo VIDEO_REPLICA_SETTINGS_KEY is required
  exit /b 1
)
if "%VIDEO_REPLICA_STORAGE_ROOT%"=="" set VIDEO_REPLICA_STORAGE_ROOT=%CD%\storage

rem PyInstaller 等分发形态下,用 VIDEO_REPLICA_SERVER_CMD / WORKER_CMD 指向产物可执行文件。
if "%VIDEO_REPLICA_SERVER_CMD%"=="" (
  set VIDEO_REPLICA_SERVER_CMD=uv --cache-dir .uv-cache run --project server --locked uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000
)
if "%VIDEO_REPLICA_WORKER_CMD%"=="" (
  set VIDEO_REPLICA_WORKER_CMD=uv --cache-dir .uv-cache run --project server --locked python -m app.generation_worker
)

start "video-replica-api" cmd /k "%VIDEO_REPLICA_SERVER_CMD%"
start "video-replica-worker" cmd /k "%VIDEO_REPLICA_WORKER_CMD%"
exit /b 0
