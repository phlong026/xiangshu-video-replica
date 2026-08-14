# 短视频复刻工作台

内部员工使用的 Windows 桌面端。当前实现范围为 V1.2 计划的本地可验证骨架，Windows 内测交付配置已补齐；真实 Provider、签名和 Windows 实机打包仍需按门禁记录。

## 环境要求

- Node.js 24+
- Rust stable（Windows 使用 MSVC toolchain）
- Python 3.12+
- uv 0.11+

Windows 还需按照 Tauri 2 官方要求安装 Microsoft C++ Build Tools 和 WebView2。

## 初始化

```powershell
npm install
uv sync --project server --locked
```

## 本地开发

先启动 FastAPI：

```powershell
npm run dev:server
```

另开一个终端启动生成 Worker；它与 API 使用同一个 SQLite 文件，并负责真实 H3 任务的提交、查询和结果归档：

```powershell
npm run dev:worker
```

再启动桌面端：

```powershell
npm run tauri:dev
```

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
```

SQLite 数据库只允许放本机磁盘，不放 COS、OSS、NAS、网盘同步或网络共享目录。升级前先用 `server/app/backup.py` 生成备份，升级后比对项目、任务、版本和审计计数；未完成 Windows 安装包测试、真实 Provider 验收和 10-20 个真实项目试跑前，只能标记为 `LOCALLY_VERIFIED` 或 `UNSIGNED_INTERNAL_TEST`。

## API 类型同步

服务端运行后执行：

```powershell
npm run generate:api
```

该命令从 FastAPI 的 `/openapi.json` 生成 `client/src/generated/api.ts`。生成文件由服务端契约派生，不手工修改。
