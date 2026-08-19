@echo off
rem 启动本地 FastAPI + 生成 Worker,供 Tauri 桌面端自动拉起。
rem 服务端分发形态可通过环境变量覆盖;默认按开发布局(uv + server/ 目录)运行。
setlocal

if "%VIDEO_REPLICA_DB_PATH%"=="" (
  echo VIDEO_REPLICA_DB_PATH is required
  exit /b 1
)
if "%VIDEO_REPLICA_DESKTOP_USER_ID%"=="" (
  echo VIDEO_REPLICA_DESKTOP_USER_ID is required
  exit /b 1
)
if "%VIDEO_REPLICA_AUTH_MODE%"=="" set "VIDEO_REPLICA_AUTH_MODE=desktop"
if "%VIDEO_REPLICA_STORAGE_ROOT%"=="" set VIDEO_REPLICA_STORAGE_ROOT=%CD%\storage

rem 打包命令是一个整体，禁止一部分走 sidecar、另一部分误回退到开发目录。
set "VIDEO_REPLICA_PACKAGED_COMMANDS="
if defined VIDEO_REPLICA_BOOTSTRAP_CMD set "VIDEO_REPLICA_PACKAGED_COMMANDS=1"
if defined VIDEO_REPLICA_SERVER_CMD set "VIDEO_REPLICA_PACKAGED_COMMANDS=1"
if defined VIDEO_REPLICA_WORKER_CMD set "VIDEO_REPLICA_PACKAGED_COMMANDS=1"
if defined VIDEO_REPLICA_PACKAGED_COMMANDS (
  if not defined VIDEO_REPLICA_BOOTSTRAP_CMD goto packaged_command_error
  if not defined VIDEO_REPLICA_SERVER_CMD goto packaged_command_error
  if not defined VIDEO_REPLICA_WORKER_CMD goto packaged_command_error
)

rem 先迁移旧数据库并验证已保存凭据可解密，再并发启动 API 和 Worker。
if "%VIDEO_REPLICA_BOOTSTRAP_CMD%"=="" set VIDEO_REPLICA_BOOTSTRAP_CMD=uv --cache-dir .uv-cache run --project server --locked python -m app.bootstrap
call %VIDEO_REPLICA_BOOTSTRAP_CMD%
if errorlevel 1 exit /b 1

rem PyInstaller 等分发形态下,用 BOOTSTRAP_CMD / SERVER_CMD / WORKER_CMD
rem 分别指向不依赖开发目录的打包产物。
if "%VIDEO_REPLICA_SERVER_CMD%"=="" (
  set VIDEO_REPLICA_SERVER_CMD=uv --cache-dir .uv-cache run --project server --locked python -m uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000
)
if "%VIDEO_REPLICA_WORKER_CMD%"=="" (
  set VIDEO_REPLICA_WORKER_CMD=uv --cache-dir .uv-cache run --project server --locked python -m app.generation_worker
)

start "video-replica-api" cmd /k "%VIDEO_REPLICA_SERVER_CMD%"
start "video-replica-worker" cmd /k "%VIDEO_REPLICA_WORKER_CMD%"
exit /b 0

:packaged_command_error
echo packaged bootstrap, server, and worker commands must be set together 1>&2
exit /b 1
