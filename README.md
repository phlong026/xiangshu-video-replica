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
$env:VIDEO_REPLICA_AUTH_MODE = "development"
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

开发 Header 只在 Vite 开发构建中发送；生产构建即使误设 `VITE_DEV_USER_ID` 也会忽略它。桌面发布/内测运行必须由服务端设置 `VIDEO_REPLICA_AUTH_MODE=desktop` 和 `VIDEO_REPLICA_DESKTOP_USER_ID`，`/api/auth/me` 再从数据库读取显示名称和角色，客户端不能自行声明 admin 身份。

### 内部云端 P0 身份

内部云端模式使用受控 CLI 创建账号和钱包，并签发只显示一次的 Bearer Token。数据库只保存令牌 SHA-256 摘要：

```powershell
Set-Location server
.venv\Scripts\python.exe -m app.internal_accounts --db-path C:\video-replica\data\app.db create-user --username operator_1 --display-name "运营一号"
.venv\Scripts\python.exe -m app.internal_accounts --db-path C:\video-replica\data\app.db issue-token --user-id "上一步输出的 user_id"
.venv\Scripts\python.exe -m app.internal_accounts --db-path C:\video-replica\data\app.db revoke-token --token-id "签发时输出的 token_id"
```

认证默认采用 fail-closed 的内部令牌模式；部署时仍应显式设置 `VIDEO_REPLICA_AUTH_MODE=internal`，并且不要设置 `VIDEO_REPLICA_DESKTOP_USER_ID` 或 `VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER`。该模式下业务 API 只接受 `Authorization: Bearer <token>`；令牌撤销后立即失效。只有显式设置 `desktop` 或 `development` 才会启用旧身份路径。原始令牌不会再次显示，应由客户端系统安全存储或受控 Secret 工具保管，不得写入仓库、日志或普通配置文件。

### ZPay 内部充值下单

`POST /api/recharge-orders` 只接收整数分 `amount_fen`，服务端根据当前内部价格计算条数并生成 ZPay 表单。商户号、商户密钥、支付渠道、商户订单号和回调地址都不接受客户端覆盖。

部署必须设置不含路径和查询参数的 HTTPS `PUBLIC_BASE_URL`，以及经程序白名单校验的 `ZPAY_GATEWAY_URL`（当前支持官方公开文档的 `https://zpayz.cn/submit.php` 和用户 Demo 的 `https://z-pay.cn/submit.php`）。ZPay `pid`/`key`/`enabled_channels` 使用现有 SettingsRepository 加密保存；可视化配置入口在内部 P0 管理页任务中提供，不得通过 SQL 写入明文密钥。签名契约以 [ZPay 官方开发文档](https://api.z-pay.cn/doc.html) 为准。

### ZPay 回调与手动查单

`GET /api/payments/zpay/notify` 是唯一自动入账入口；它验签并核对商户、订单、金额、渠道和成功状态，在一个短 SQLite 事务内完成订单、`CHARGE` 流水和钱包更新。`return_url` 只显示确认提示，不会改余额。部署时由同机反向代理单独公开 notify/return 路径，FastAPI 端口仍只监听回环地址。

管理端手动同步使用 `POST /api/control/recharge-orders/{order_no}/sync`。API 只保存 `CONTROL_PROXY_TOKEN_DIGEST`（原始高熵令牌的 SHA-256）和 `CONTROL_ADMIN_USER_ID`；反向代理必须移除外部传入的 `X-Control-Proxy-Token`，完成管理认证后再注入原始令牌。业务 Bearer Token 不能代替控制代理令牌。

### 内部用户端与管理页

业务工作台新增“余额与充值”：显示 10 元/条的当前内部价、可用/冻结条数、最近充值订单和追加式钱包流水；支持 100/200/500/1000 元快捷充值，以及不低于 100 元、按 10 元递增的自定义整数金额。待确认订单号只保存在浏览器本地，页面返回后每 2 秒查询一次本地订单；`PAID` 后刷新钱包，前端状态本身不触发入账。

云端内部账号第一次打开业务工作台时，在登录页输入 CLI 签发的 Bearer Token。Token 只保存在当前页面内存，不写入 `localStorage`；页面刷新后需要重新输入。桌面开发模式可以留空，仍按既有服务端身份模式验证。

同一份 React/Vite 构建在 `/admin` 渲染独立内部管理页，只提供账号与钱包只读列表、充值订单与查单、只读对账/CSV、ZPay 配置和内部价格设置。密钥只展示掩码，新密钥留空表示保留旧值；网关、异步回调和同步返回地址来自部署环境，只读不可提交。管理页没有手工改余额、补单或令牌签发入口。

生产构建建议显式把 `VITE_API_BASE_URL` 设为与页面同源的 HTTPS 地址，便于发布审计；未设置时，HTTPS Web 生产页会安全回退到 `window.location.origin`，桌面端和开发构建仍使用本机 API。参考 `deploy/nginx/internal-p0.conf.example` 保护 `/admin` 和 `/api/control/*`：IP/VPN 白名单与 Basic Auth 必须同时通过，浏览器永远接触不到控制代理原始令牌。FastAPI 继续只监听 `127.0.0.1`；若 Nginx 前面还有负载均衡或 CDN，必须先按可信代理范围正确恢复客户端地址，否则不要直接复用样例中的 IP 白名单。

Linux 单机部署的环境模板、systemd 单元、SQLite 检查/备份/恢复和验收命令统一见 `docs/内部运营P0单机部署与验收记录.md`。部署文件只覆盖一个 API、一个 Worker、一个本机 SQLite 文件和同机静态页；它们不代表真实 ZPay、COS 或 Provider 已验收。

### 内部钱包与按条计费

`GET /api/wallet` 返回当前内部用户的可用条数和冻结条数；`GET /api/wallet/transactions` 用 `limit`、`offset` 分页返回当前用户自己的追加式流水。创建一条生成任务会在同一个 SQLite 事务内写入 `RESERVE`，并把 1 条从可用余额移到冻结余额；余额不足返回 `402 INSUFFICIENT_CREDITS`，批次、任务、Prompt 状态和钱包不会部分提交。

所有任务终态只经过 `finalize_internal_billing(task_id, outcome)`：成片写入已配置的结果存储并通过对象元数据和下载签名检查后写 `SETTLE`；失败或取消写 `RELEASE`；Provider 已成功但归档失败时继续冻结，等待原任务归档重试。付费重生成会创建新任务并重新冻结，安全的原任务重试在上一轮已返还后进入下一计费轮次。请求中的旧字段 `payment_confirmed`、`payment_confirmation_version` 已被拒绝，不能代替服务端钱包校验。

生成结果存储现在跟随业务主存储：内部云端配置 COS 后成片进入 COS；未配置 COS 时仅供本地开发回退本地盘。已产生钱包流水的批次为不可变账务记录，API 不允许删除。

### 本地存储（无 COS 凭据的开发机）

开发机没有 COS 凭据时，可将运行设置切换为本地文件系统存储，走完完整上传/归档流程：

```powershell
$env:VIDEO_REPLICA_STORAGE_ROOT = "C:\video-replica-storage"   # 本地存储根目录（必填）
```

在管理员设置中将 `active_storage_provider` 设为 `local`。local 模式下上传不依赖云厂商签名 URL，而是经 `/api/assets/local-objects/...` 由服务端落盘到 `VIDEO_REPLICA_STORAGE_ROOT`：本地开发未设 `PUBLIC_BASE_URL` 时使用 `http://127.0.0.1:8000`，服务器设置合法 `PUBLIC_BASE_URL` 后使用该 HTTPS origin。该模式仅用于本地/内测，不应在生产启用（生产只使用腾讯云 COS）。

### 管理员配置持久化

Provider API Key 和云存储凭证加密后写入 `VIDEO_REPLICA_DB_PATH` 指向的 SQLite，不写入浏览器 `localStorage`。启动前 `app.bootstrap` 会自动执行 Alembic 迁移并验证已保存配置可解密；失败时 API 和 Worker 不会启动，也不会覆盖原配置。

从历史 OSS 版本升级时，若 `assets.storage_uri` 仍存在 `oss://` 对象，迁移会拒绝继续，保留 OSS 凭证和原数据。必须先将对象及 URI 迁移到 COS 或受控本地存储，再重新执行升级；不允许通过删除凭证来静默遗弃历史素材。

- macOS 未显式设置 `VIDEO_REPLICA_SETTINGS_KEY` 时，主密钥自动创建并复用于当前用户钥匙串。
- Windows 未显式设置该变量时，主密钥由当前用户 DPAPI 保护，密文保存在 `%LOCALAPPDATA%\VideoReplicaWorkbench\secrets\settings-key.dpapi`。
- 服务器或集中部署可由安全的 Secret 注入机制显式提供 `VIDEO_REPLICA_SETTINGS_KEY`；桌面系统只会在该值成功解密当前数据库后将其导入系统密钥存储。不支持系统密钥存储的服务器每次启动必须复用同一值；主密钥不得写入仓库、启动脚本或日志。

重启 API、Worker 或桌面端时必须继续使用同一个数据库路径和同一个系统用户。主密钥缺失或不匹配时，设置页会明确提示“配置仍在，未被覆盖”；数据库和系统密钥存储两者都需纳入备份/恢复验收。

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
$env:VIDEO_REPLICA_AUTH_MODE = "desktop"
$env:VIDEO_REPLICA_DESKTOP_USER_ID = "内部用户ID" # 必须对应 users 表中的已启用用户
# 可选：仅安全部署系统注入；留空则由当前 Windows 用户 DPAPI 持久化
# $env:VIDEO_REPLICA_SETTINGS_KEY = "<由 Secret 系统注入的稳定 Fernet 主密钥>"
```

SQLite 数据库只允许放本机磁盘，不放 COS、NAS、网盘同步或网络共享目录。升级前先用 `server/app/backup.py` 生成备份，升级后比对项目、任务、版本和审计计数；未完成 Windows 安装包测试、真实 Provider 验收和 10-20 个真实项目试跑前，只能标记为 `LOCALLY_VERIFIED` 或 `UNSIGNED_INTERNAL_TEST`。

## API 类型同步

服务端运行后执行：

```powershell
npm run generate:api
```

该命令从 FastAPI 的 `/openapi.json` 生成 `client/src/generated/api.ts`。生成文件由服务端契约派生，不手工修改。
