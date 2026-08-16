# 短视频复刻工作台

内部员工使用的 Windows 桌面端。当前实现按 V1.3 任务清单逐项收口；本地质量、受保护主分支、人物领域契约和桌面身份/RBAC 已进入可验证状态，真实 Provider、签名和 Windows 实机打包仍需按门禁记录。

## 环境要求

- Node.js 24+
- Rust stable（Windows 使用 MSVC toolchain）
- Python 3.12+
- uv 0.11+
- Gate 1 本地纵向验收另需系统 Chrome 与 ffmpeg

Windows 还需按照 Tauri 2 官方要求安装 Microsoft C++ Build Tools 和 WebView2。

## 初始化

```powershell
npm install
uv sync --project server --locked
```

## 本地开发

开发身份 Header 默认关闭。先把服务端显式切到开发身份模式；`VITE_DEV_USER_ID` 必须对应当前 SQLite `users` 表中一个已启用的用户，未设置时客户端开发服务器默认使用 `employee_1`：

```powershell
$env:VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER = "1"
npm run dev:server
```

另开一个终端启动生成 Worker；它与 API 使用同一个 SQLite 文件，并负责真实 H3 任务的提交、查询和结果归档：

```powershell
npm run dev:worker
```

再启动桌面端：

```powershell
$env:VITE_DEV_USER_ID = "employee_1" # 调试设置页时改为现有 admin 用户 ID
npm run tauri:dev
```

开发 Header 只在 Vite 开发构建中发送；生产构建即使误设 `VITE_DEV_USER_ID` 也会忽略它。发布/内测运行必须由服务端设置 `VIDEO_REPLICA_DESKTOP_USER_ID`，`/api/auth/me` 再从数据库读取显示名称和角色，客户端不能自行声明 admin 身份。

### 本地存储（无云存储凭据的开发机）

开发机没有 COS/OSS 凭据时，可将运行设置切换为本地文件系统存储，走完完整上传/归档流程：

```powershell
$env:VIDEO_REPLICA_STORAGE_ROOT = "C:\video-replica-storage"   # 本地存储根目录（必填）
```

在管理员设置中将 `active_storage_provider` 设为 `local`。local 模式下上传不依赖云厂商签名 URL，而是经 `http://127.0.0.1:8000/api/assets/local-objects/...` 由服务端落盘到 `VIDEO_REPLICA_STORAGE_ROOT`。该模式仅用于本地/内测，不应在生产启用（生产保持 COS/OSS）。

如只调试浏览器界面，可运行 `npm run dev:client`。

## 检查与构建

```powershell
npm run check
npm run build
npm run tauri:build
```

`npm run check` 会依次运行前端格式检查、类型检查和测试，Tauri/Rust 格式与编译检查，以及服务端 Ruff、Mypy 和 Pytest。

### Gate 1 桌面 FakeProvider 纵向验收

```powershell
npm run test:gate1
```

该命令要求开始和结束时均处于同一提交的干净 Git 工作树，从全新 Alembic 数据库启动隔离的 FastAPI 与 Vite，使用系统 Chrome 执行 Playwright，并在退出时清理本次 API/Vite 进程组。运行日志、临时媒体、截图、trace、录屏、下载产物和 SHA256 清单写入 `output/playwright/gate1/<run-id>/`，该目录不进入版本控制。只有不附加 Playwright 过滤参数的完整套件会在 manifest 中标记为 `passed`；`--grep` 等局部调试运行会记录参数并标记为 `diagnostic_passed`，不能作为正式 Gate 1 证据。它只证明 macOS 本地 FakeProvider 桌面链路，不替代 Windows WebView2、真实 Provider、云存储或生产灰度验收。

Windows x64 内测安装包使用 Tauri NSIS：

```powershell
npm run tauri -- build --target x86_64-pc-windows-msvc --bundles nsis
```

当前 installer 配置见 `client/src-tauri/tauri.conf.json`：

- 只生成 NSIS installer；
- 默认当前用户安装，普通员工无需管理员权限；
- 禁止安装旧版本覆盖新版本；
- WebView2 使用下载引导器，离线内测机需预装 WebView2 Runtime；
- 当前未配置签名证书，未签名前只能作为内部未签名测试包分发。

Windows 内测、升级、卸载、SQLite 备份恢复和日志策略见 `docs/Windows内测与运维手册.md`。真实 Provider 证据记录见 `docs/真实Provider验收记录模板.md`。

## Windows 内测运行目录

服务端必须显式设置数据目录，避免升级时因工作目录变化造成数据丢失：

```powershell
$env:VIDEO_REPLICA_HOME = "$env:LOCALAPPDATA\VideoReplicaWorkbench"
$env:VIDEO_REPLICA_DB_PATH = "$env:VIDEO_REPLICA_HOME\data\app.db"
$env:VIDEO_REPLICA_LOG_DIR = "$env:VIDEO_REPLICA_HOME\logs"
$env:VIDEO_REPLICA_DESKTOP_USER_ID = "内部用户ID" # 必须对应 users 表中的已启用用户
```

SQLite 数据库只允许放本机磁盘，不放 COS、OSS、NAS、网盘同步或网络共享目录。升级前先用 `server/app/backup.py` 生成备份，升级后比对项目、任务、版本和审计计数；未完成 Windows 安装包测试、真实 Provider 验收和 10-20 个真实项目试跑前，只能标记为 `LOCALLY_VERIFIED` 或 `UNSIGNED_INTERNAL_TEST`。

## API 类型同步

服务端运行后执行：

```powershell
npm run generate:api
```

该命令从 FastAPI 的 `/openapi.json` 生成 `client/src/generated/api.ts`。生成文件由服务端契约派生，不手工修改。
