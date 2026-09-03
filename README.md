# 短视频复刻工作台（xiangshu-video-replica）

AI 短视频复刻生产工作台：参考视频上传 → AI 拆解分镜 → 人物库与首帧 → Prompt/批次编排 → H3 视频生成 → 成片直链交付 → 按条计费。业务后端 FastAPI，桌面端 Tauri 2，客户生产数据真源 PostgreSQL 16。当前版本 0.1.16（2026-09-04）。

## 两条产品线

| | 客户云版（当前主线） | 内部 P0 单机版（已收口） |
| --- | --- | --- |
| 数据真源 | PostgreSQL 16（客户生产唯一真源，fail-closed） | SQLite（仅本机磁盘） |
| 部署形态 | LB + 双 API + 四 Worker + PG HA + 私有 COS（`deploy/` 模板与 systemd 单元） | 桌面端拉起同机 FastAPI + Worker sidecar |
| 用户身份 | 激活码激活 + 两设备单在线会话 | 内部 Bearer Token / 桌面固定身份 |
| 计费 | 零额度激活 + ZPay 续充 + 管理端调账审计 | 内部价钱包 + ZPay 充值 |
| 桌面构建 | `npm run tauri:build:customer`（直连远程 HTTPS API，独立应用标识） | `npm run tauri:build`（随包 `start-backend` 脚本） |

客户版 V3 主线（任务 T01–T45）：PostgreSQL 全量迁移、激活码与首充、两设备单在线、用户公平队列、多实例生产与灰度基座、安全纵深加固均已交付并达到 `AUTOMATED_VERIFIED`；真实 ZPay / COS / 付费 Provider 小流量验收（T40）、灰度放量（T41）与生产 Go/No-Go（T42）按红线要求待人工授权执行。任务状态以 `docs/客户版任务清单-V3.md` 账本为准。

## 架构与技术栈

- **`server/`** — Python 3.12 · FastAPI · Alembic · psycopg3（同步驱动，`%s` 占位符）· pytest · Ruff · mypy strict。
- **`client/`** — React 19 · TypeScript 5.9 · Vite 8 · Biome · Vitest；API 类型由 FastAPI OpenAPI 生成（`npm run generate:api`，产物不手工修改）。业务工作台、客户端与管理端共用同一 React 构建。
- **`client/src-tauri/`** — Tauri 2 / Rust 桌面端，内部版与客户云版双构建目标（`tauri.conf.json` / `tauri.customer.conf.json`）。
- **存储** — 人物、首帧等业务图片在生产使用腾讯云 COS 私有桶（启动即校验，缺/错配置 fail-closed）；新视频成片只保存供应商结果链接，不再下载或转存。开发机可回退本地文件系统存储。
- **外部 Provider** — 视频拆解（Gemini）、人物图片（GPT Image 2 / Nano Banana）、视频生成（Metaso H3）。开发联调可切 `fake_h3` 模拟链路，不触达付费接口。

### 核心能力（客户版 V3）

- **激活码体系**：批次/发放/暂停/作废/归档管理，CSPRNG 生成 + HMAC key version，掩码展示与受控明文揭示审计，私有 COS 密文导出，防枚举与多实例共享限流，AEAD 幂等恢复。
- **钱包计费**：零额度激活，ZPay 续充回调原子入账，任务 `RESERVE → SETTLE/RELEASE` 终态原子结算，管理端双确认调账，append-only 账本与审计。
- **设备与会话**：一码两设备、首设备批准配对，单在线切换（session epoch fencing），撤销传播与旧会话拒绝，配对/解绑全程审计。
- **用户公平队列**：按用户轮转、每用户默认并发 1，Worker 崩溃恢复与 Provider 提交不确定人工核验；10k 任务四 Worker 压测恰好一次消费。
- **管理端**：per-operator 账号密码 + CSRF，职责分离（管理员不可给自己调账/改价），auditor 只读合规审计，激活码/设备/充值/审计统一管理页。
- **生产运维**：独立 `/health` `/live` `/ready` 探针，结构化请求日志与 request id，双实例私有 Prometheus 指标，11 类集群异常探针（fired/resolved），PG16 物理备份 + PITR 恢复演练脚本，滚动发布与失败回滚。

## 环境要求

- Node.js 24+、Rust stable（Windows 使用 MSVC toolchain）、Python 3.12+、uv。
- Windows 桌面构建另需 Microsoft C++ Build Tools 与 WebView2（Tauri 2 官方要求）。
- PG 集成测试需 Docker（`scripts/pg-fixture.sh`，PG16，固定端口 5433）；浏览器 E2E 另需系统 Chrome 与 ffmpeg。

## 快速开始

```bash
npm install
uv sync --project server --locked
```

本地开发默认使用 SQLite 与开发身份模式：

```powershell
$env:VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER = "1"
$env:VIDEO_REPLICA_AUTH_MODE = "development"
npm run dev:server   # 终端 1：FastAPI（127.0.0.1:8000，启动时自动执行 Alembic 迁移）
npm run dev:worker   # 终端 2：生成 Worker（拆解/人物/首帧/H3 任务领取、Provider 调用与结果交付）

$env:VITE_DEV_USER_ID = "employee_1"   # 必须对应 users 表中已启用的用户
npm run tauri:dev    # 终端 3：桌面端
```

- 开发身份 Header 只在 Vite 开发构建中发送；生产构建即使误设 `VITE_DEV_USER_ID` 也会忽略。
- 切换 PostgreSQL 模式时设置 `VIDEO_REPLICA_DATABASE_URL=postgresql://…`；客户生产（`VIDEO_REPLICA_CUSTOMER_PRODUCTION=true`）下使用 SQLite 或缺少 DSN 会直接拒绝启动。
- 没有 COS 凭据时，设置 `VIDEO_REPLICA_STORAGE_ROOT` 并在管理设置中把 `active_storage_provider` 设为 `local`，即可完成图片等资产的开发联调（仅限开发/内测，生产图片资产只使用 COS）。
- Provider API Key 与云存储凭据经 Fernet 加密写入数据库（Windows 下主密钥由当前用户 DPAPI 保护，macOS 使用钥匙串），启动时校验可解密；主密钥缺失不会覆盖已有配置。任何真实密钥不得进入代码、日志或 PR。
- 只调试浏览器界面时运行 `npm run dev:client`；环境变量样例见 `.env.example`。

## 成片直链、下载与客户记录

`GET /api/generation-tasks/{task_id}/preview-url` 先校验任务所属项目，再返回 `{"url":"..."}`。真实供应商任务直接返回原结果链接，不重新生成、不上传云存储；链接仍受供应商有效期及跨域策略约束。

视频生成成功后只持久化供应商结果链接并完成既有任务/账务结算，API 与 Worker 不再下载成片、执行音频/画面质检或转存云存储。新直链结果为 `archive_status=DIRECT`、`quality_status=NOT_REQUIRED`（未执行质检，不等于质检通过）；历史质检字段保留用于记录，但不再把已成功交付的视频标记为需要处理。真实生成失败、提交结果未知及计费待确认仍保留处理入口。人物图片与置换首帧处理不属于此变更。

Windows 桌面端下载视频前显示保存对话框，取消时不发起下载；实际 WebView 下载完成后才显示文件路径和“打开文件夹”。原生层仅允许本地主窗口为当前下载选择的目标位置，同一时间只允许一份视频保存，防止 Windows 路径别名并发覆盖。网页端只能确认下载已发起，应在浏览器下载列表查看结果。新增下载能力需重新打包桌面端才能生效。

`DELETE /api/generation-batches/{batch_id}` 只写入当前账号的 `customer_batch_visibility` 隐藏标记，即使已计费或仍在生成也可移除；它不取消生成、不退款，不删除任务、结果链接、钱包流水或管理端记录。列表查询在分页前排除当前账号隐藏项，换设备或重新登录仍生效，其他账号不受影响。上线前需执行迁移 `055_customer_batch_visibility`；显式降级只恢复客户列表可见性，不删除生成或费用事实。

仅在非客户生产环境，`fake_h3` 的 `fake://` 测试结果可转换为 `data:video/mp4;base64,...`，便于浏览器播放和下载。API 与 Worker 应使用相同的 `VIDEO_REPLICA_FAKE_H3_RESULT_PATH`；该模式不用于生产成片存储。

## 验证命令（分层，避免双跑全量）

开发期每轮迭代只跑受影响专项（秒级）：

```bash
# server/ 目录；PG fixture 未启动时 PG 专项按 skip 运行
uv run python -m pytest tests/test_<受影响文件>.py -q
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

任务收尾、提 PR 前跑一次全仓门禁（等价于 CI Linux 质量门）：

```bash
scripts/pg-fixture.sh start   # Docker PG16 fixture（脚本必须带子命令）
npm run check                 # secret 扫描 → 前端 Biome/tsc/vitest → Tauri cargo 检查 → 服务端 Ruff/mypy/全量 pytest
scripts/pg-fixture.sh stop
```

- 服务端全量 pytest（约 1550 用例，21–30 分钟）每任务只跑一次，由 `npm run check` 统一承载，不要单独重复执行。
- 严禁两个全量 pytest 实例同时打同一个 PG fixture（共享 `customer_v3_test` 库会互踩造成假性失败）。
- `cargo test`、`npm audit`、客户浏览器 E2E 与 `npm run build` 只在 CI 三门禁执行，本地 `npm run check` 不含；涉及 Rust/构建/依赖变更以 CI 为准。
- 当前本地验证规模（2026-09-04）：服务端 1555 passed / 1 skipped（本机无 ffmpeg）、客户端 vitest 715、直链播放与列表隐藏 Playwright 1、Tauri 模块测试两种配置各 16。

## 构建与发布

```bash
npm run tauri:build            # 内部版 Windows NSIS 安装包（当前用户安装，未签名内测包）
npm run tauri:build:customer   # 客户云版 NSIS（--no-default-features，强制非 loopback HTTPS API origin）
npm run test:customer-e2e      # 客户浏览器 E2E（激活 / 设备配对 / 充值）
npm run test:gate1             # 内部 FakeProvider 桌面纵向验收（隔离 API+Vite+Chrome，产物写 output/playwright/）
```

客户生产发布：

- `deploy/customer-git-rollout.sh` 从明确的 40 位提交拉取代码、本机构建前端并滚动更新 API 与 Worker，失败自动恢复旧镜像与静态站点；私有仓库经只读 Deploy Key + `VIDEO_REPLICA_GIT_REPO_URL` 访问。
- 发布前必须运行 `scripts/customer_release_preflight.py` 且输出 `T45_PREFLIGHT_OK`；真实 ZPay / COS / Provider 联测未在同一 SHA 完成前一律 No-Go。
- 部署模板见 `deploy/`（nginx / systemd / PG PITR 脚本 / `customer.env.example`），操作正本见 `docs/客户版部署与灰度手册.md`。

## CI 与分支模型

- 三门禁 CI（`.github/workflows/ci.yml`）：Secret scan → Linux 质量门（含 PG16 service 的全量测试）→ Windows Tauri/NSIS。
- `main` 受保护，仅接受 squash merge；任务分支从最新 main 切出，命名 `feat/customer-v3-tXX-短横线描述`，同一时间只开一个任务分支。
- 一个 PR 只承载一个任务；评审评论逐条实质修复后 resolve；同一 PR 内更新任务账本与证据（`docs/客户版任务清单-V3.md`、`docs/CUSTOMER-TASK-EVIDENCE-V3.md`、`docs/evidence/TXX-EVIDENCE.md`）。

## 红线（摘要）

- 禁止引入 ORM、Redis、消息队列框架；禁止 SQLite/PG 双真源与双写。
- 已发布 Alembic revision 只可追加修复，不得篡改；当前迁移链 head 为 `055_customer_batch_visibility`。
- 任何真实 API key、激活码明文、设备/session token 不得进入代码、日志、测试夹具或 PR。
- 证据层级逐级推进：`CODE_PRESENT → AUTOMATED_VERIFIED → STAGING_VERIFIED → REAL_CHAIN_VERIFIED → PRODUCTION_GO`；未过真实链路不得标 `PRODUCTION_GO`。
- 真实 ZPay、付费 Provider、生产 COS 变更、对外发码、灰度扩大、公网发布必须先取得用户明确授权。

## 文档索引（正本优先）

| 文档 | 用途 |
| --- | --- |
| `docs/客户版任务清单-V3.md` | 唯一任务状态账本（DoD、红线、§12 工作包、§14 证据模板） |
| `docs/客户版代码开发清单-V3.md` | 唯一文件映射（新文件名已冻结，不得自创） |
| `docs/客户版开发计划-V3.md` | 里程碑与禁止并行项 |
| `docs/客户版激活码完整开发文档-V3.md` | 激活码业务与架构正本 |
| `docs/客户版测试与验收规格-V3.md` | 测试与验收正本 |
| `docs/客户版部署与灰度手册.md` | 客户生产部署、滚动更新与回滚 |
| `docs/CUSTOMER-TASK-EVIDENCE-V3.md` | 证据账本 |
| `docs/evidence/` | 各任务证据文件（含 §14 模板） |
| `AGENTS.md` | AI 开发代理协作说明与验证命令 |
| `CHANGELOG.md` | 版本更新日志 |

内部 P0 运营（账号 CLI、钱包与 ZPay 计费、单机部署验收）见 `docs/内部运营P0单机部署与验收记录.md` 与 `docs/内部运营与ZPay计费管理文档-P0.md`。`docs/剩余开发工作清单.md`、`docs/Windows内测与运维手册.md` 等为历史快照，仅作参考，不得作为实施依据。

## 仓库结构

```text
server/           FastAPI 业务后端
  app/            路由与服务（客户 lane / 内部 lane）、生成 Worker、运维探针
  migrations/     Alembic 迁移链（001 → 055）
  tests/          服务端测试（含 PG 专项，pytest 标记 pg）
client/           React 19 + Vite 工作台（业务工作台 / 客户端 / 管理端）
  src-tauri/      Tauri 2 桌面端（内部版 / 客户云版双配置）
e2e/              Playwright 套件（customer：激活/配对/充值；gate1：内部纵向验收）
deploy/           部署模板（nginx / systemd / PG PITR / customer-git-rollout.sh）
scripts/          PG fixture、secret 扫描、发布 preflight
docs/             正本文档、任务账本、评审报告与证据
```
