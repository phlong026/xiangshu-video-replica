# Windows 内测与运维手册

> 适用范围：D2-01 Windows 打包与内测
> 当前状态：可执行手册草案；未声明签名、Windows 构建、10-20 个真实项目试跑已完成
> 产品版本：0.1.0

## 1. 交付边界

D2-01 交付物分为三类：

| 类别 | 当前仓库交付 | 不得提前宣称 |
| --- | --- | --- |
| Windows installer 配置 | Tauri 2 NSIS、Windows x64 构建命令、当前用户安装、禁止降级 | 已完成 Windows 构建、签名、发布 |
| 运维策略 | SQLite 数据目录、备份、恢复、日志、升级验证步骤 | 已完成生产运维演练 |
| 内测记录 | 安装、升级、Provider、成本、失败率、质量问题记录模板 | 已完成 10-20 个真实项目试跑 |

## 2. Windows installer 配置

Tauri 配置位于 `client/src-tauri/tauri.conf.json`：

- `bundle.targets`: `["nsis"]`
- installer 类型：NSIS
- 安装范围：`currentUser`
- 语言：`SimpChinese`
- WebView2：`downloadBootstrapper`
- 降级策略：`allowDowngrades=false`
- 图标：`client/src-tauri/icons/icon.ico`

构建命令：

```powershell
npm install
npm run build
npm run tauri -- build --target x86_64-pc-windows-msvc --bundles nsis
```

输出目录通常为：

```text
client/src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/
```

签名门禁：

- 当前配置未写入 `certificateThumbprint`、`signCommand` 或 `timestampUrl`。
- 内部签名前，安装包状态只能标记为 `UNSIGNED_INTERNAL_TEST`。
- 签名后必须记录证书指纹、时间戳服务、签名命令、安装包 SHA256 和验证截图。

## 3. 运行目录策略

### 3.1 应用目录

桌面端由 NSIS 安装到当前用户目录，避免普通员工安装时需要管理员权限。应用程序目录只放客户端二进制和静态资源，不保存业务数据。

### 3.2 SQLite 数据目录

服务端必须显式设置 `VIDEO_REPLICA_DB_PATH`，不要依赖工作目录。

推荐内测目录：

```powershell
$env:VIDEO_REPLICA_HOME = "$env:LOCALAPPDATA\VideoReplicaWorkbench"
$env:VIDEO_REPLICA_DB_PATH = "$env:VIDEO_REPLICA_HOME\data\app.db"
$env:VIDEO_REPLICA_DESKTOP_USER_ID = "内部用户ID"
# 可选：仅安全部署系统注入；留空则由当前 Windows 用户 DPAPI 持久化
# $env:VIDEO_REPLICA_SETTINGS_KEY = "<由 Secret 系统注入的稳定 Fernet 主密钥>"
```

`VIDEO_REPLICA_DESKTOP_USER_ID` 必须对应 `users` 表中 `is_active = 1` 的内部用户。桌面端以 `/api/auth/me` 返回结果作为身份真源；发布环境不设置 `VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER`，也不使用 `VITE_DEV_USER_ID`。服务端配置桌面身份后会忽略客户端伪造的 `X-Dev-User-Id`。

目录结构：

```text
%LOCALAPPDATA%\VideoReplicaWorkbench\
  data\app.db
  backups\
  logs\
  secrets\settings-key.dpapi
  storage-cache\
```

约束：

- SQLite 只放本机磁盘。
- 禁止放在 COS、NAS、网盘同步目录或网络共享目录。
- 开启 WAL、foreign_keys、busy_timeout 由服务端连接层负责。
- 桌面端不直接读写 SQLite，只访问 `http://127.0.0.1:8000` 的业务 API。
- `settings-key.dpapi` 只能由创建它的 Windows 用户通过 DPAPI 解密；升级和修复安装不得删除 `secrets\` 目录。
- API/Worker 启动前会先运行 `python -m app.bootstrap`，自动迁移数据库并验证已保存配置可解密。

### 3.3 备份目录

每日备份放在：

```text
%LOCALAPPDATA%\VideoReplicaWorkbench\backups\
```

手动备份命令：

```powershell
uv --cache-dir .uv-cache run --project server --locked python -m app.backup backup `
  "$env:VIDEO_REPLICA_DB_PATH" `
  "$env:VIDEO_REPLICA_HOME\backups\app-before-upgrade.db"
```

每日备份命令：

```powershell
uv --cache-dir .uv-cache run --project server --locked python -m app.backup daily `
  "$env:VIDEO_REPLICA_DB_PATH" `
  "$env:VIDEO_REPLICA_HOME\backups"
```

恢复命令：

```powershell
uv --cache-dir .uv-cache run --project server --locked python -m app.backup restore `
  "$env:VIDEO_REPLICA_HOME\backups\app-before-upgrade.db" `
  "$env:VIDEO_REPLICA_DB_PATH"
```

恢复前必须停止 API 和 Worker 进程。不要在运行时直接复制 `app.db`、`app.db-wal` 或 `app.db-shm`。

### 3.4 日志目录

推荐内测日志目录：

```powershell
$env:VIDEO_REPLICA_LOG_DIR = "$env:VIDEO_REPLICA_HOME\logs"
```

日志要求：

- API、Worker、安装升级记录分开保存。
- 日志不得包含 Authorization、完整 API Key、完整签名 URL、完整临时下载地址。
- Provider 请求/响应样本只保存脱敏字段。
- 失败任务要保留批次 ID、任务 ID、Provider、错误码、耗时和可复现步骤。

### 3.5 设置页诊断日志

内测管理员在桌面端“设置”页填写 H3、Apilio 和 COS 参数后，点击“测试设置”。系统会对已保存的配置逐项执行诊断，并生成可下载的 `settings-diagnostic-<id>.json`。已配置的 COS 会创建并立即删除一个小测试对象，因此可能产生云厂商请求费用；不会发起 H3、视频或图片生成任务。

- 密钥输入框不会回显原值；留空保存不会覆盖已保存密钥。
- 日志只记录 Provider、已配置字段名、测试类型、适配器能力、耗时、HTTP 状态、错误码、失败阶段和清理失败标记，不记录 API Key、Secret、Authorization、Provider 原始错误文本或完整签名 URL。
- `通过` 表示当前适配器的连接测试通过；COS 的 `通过` 已完成真实写入、元数据读取、读取和删除校验。`仅配置校验` 表示参数已保存，但该服务未发起外部调用；`未配置` 和 `失败` 均应先下载日志再调整参数或本地服务。出现清理失败时，日志会提示可能残留测试对象。
- 设置中的 API Key 和云凭证加密保存在本机 SQLite。默认主密钥由当前 Windows 用户 DPAPI 保护并跨重启复用；如由部署系统显式注入 `VIDEO_REPLICA_SETTINGS_KEY`，启动引导仅在该值成功解密当前数据库后将其导入 DPAPI 存储。密钥不可用时系统必须停止启动或返回“配置仍在”错误，不得覆盖旧配置。
- “测试设置”不会自动创建 H3 付费视频任务。真实生成只能通过项目页中明确的生成操作发起。

## 4. 安装验证

每台 Windows 内测机记录：

1. Windows 版本、CPU 架构、内存。
2. WebView2 Runtime 是否已安装；若未安装，确认安装器是否可联网拉取。
3. installer 文件名、版本、SHA256。
4. 安装路径和开始菜单快捷方式。
5. 首次启动是否打开桌面端。
6. API `/health` 是否返回 `ok`。
7. 普通员工账号是否可以进入项目页。

安装包未签名前，应明确提示内测人员这是未签名内部测试包。

## 5. 升级不丢数据验证

升级测试必须至少覆盖一次旧版本到新版本。

升级前：

1. 停止 API 和 Worker。
2. 执行手动备份。
3. 记录数据库 SHA256、表数量、关键业务计数。
4. 记录 1 个未完成批次和 1 个已完成批次的 ID。
5. 查询 `assets.storage_uri` 是否还有 `oss://` 对象；若有，先迁移对象和 URI，不得删除 OSS 凭证后强行升级。

升级：

1. 使用新 NSIS installer 覆盖安装。
2. 不删除 `%LOCALAPPDATA%\VideoReplicaWorkbench\data`。
3. 启动 API 和桌面端。

升级后：

1. 运行 Alembic 升级到 head。
2. 检查登录、项目列表、人物库、任务记录。
3. 检查升级前记录的批次和任务仍存在。
4. 关闭桌面端，保持 API/Worker 运行，确认任务进度继续更新。
5. 重开桌面端，确认进度恢复。
6. 执行一次恢复演练到临时路径，并比对任务、版本、审计计数。

计数检查示例：

```powershell
sqlite3 "$env:VIDEO_REPLICA_DB_PATH" `
  "select 'projects', count(*) from projects union all select 'generation_tasks', count(*) from generation_tasks union all select 'versions', count(*) from versions union all select 'audit_logs', count(*) from audit_logs;"
```

## 6. 卸载验证

卸载只验证程序目录和快捷方式被移除。业务数据目录默认保留，除非公司数据策略要求额外清理。

卸载后记录：

- 应用程序目录是否移除。
- 开始菜单快捷方式是否移除。
- `%LOCALAPPDATA%\VideoReplicaWorkbench\data\app.db` 是否保留。
- 重新安装后是否仍能读取旧数据。

## 7. 内测试跑记录

D2-01 目标是 10-20 个真实项目试跑。每个项目至少记录：

- 内测人员、角色、机器编号。
- 参考视频时长、尺寸、大小。
- 使用 Provider 与模型别名。
- H3 任务数量 N。
- 成功数、失败数、需处理数。
- 总费用与单条均摊费用。
- 失败错误码和重试结果。
- 输出 MP4 是否可播放、是否带原生音轨。
- 质量问题：人物一致性、动作、字幕/口播、画面瑕疵、音轨。
- 是否可接受进入下一轮。

未完成 10-20 个真实项目和真实 Provider 验收前，项目状态只能写为 `LOCALLY_VERIFIED` 或 `UNSIGNED_INTERNAL_TEST`，不能写为 `PROVIDER_VERIFIED` 或 `READY_FOR_INTERNAL_RELEASE`。

## 8. 发布前检查清单

| 检查项 | 状态 | 证据 |
| --- | --- | --- |
| Windows x64 NSIS installer 已生成 | 待填写 | installer 路径、SHA256 |
| installer 已签名 | 待填写 | 证书指纹、签名验证截图 |
| 普通员工可安装 | 待填写 | 机器编号、截图 |
| 升级不丢 SQLite 数据 | 待填写 | 备份路径、计数对比 |
| 卸载后业务数据策略已确认 | 待填写 | 保留或清理记录 |
| 关闭桌面端后任务继续运行 | 待填写 | 批次 ID、时间线 |
| 重开桌面端进度恢复 | 待填写 | 批次 ID、截图 |
| 日志无密钥和完整签名 URL | 待填写 | 抽样记录 |
| 10-20 个真实项目试跑完成 | 待填写 | 内测汇总 |
| 成本、失败率、质量问题已汇总 | 待填写 | 内测汇总 |
