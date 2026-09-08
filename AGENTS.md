# Agent 工作说明（短视频复刻 · 客户版 V3）

> 当前执行清单已更新为[本地实现去重V3](outputs/customer-cloud-convergence-analysis-2026-09-08/v3/客户版收敛剩余任务清单与验收完工标准-V3.md)：57项剩余排程，复用既有代码；原60项及CW-006/008/011保留追溯，当前状态仅见任务账本§18。此更新不代表代码或数据迁移已完成。

> 2026-09-08 PostgreSQL 全面统一增量：用户已确定开发、业务数据库测试、CI、staging、生产均使用 PostgreSQL；SQLite 仅限精确登记的离线历史输入、归档与兼容工具。
> 实施与验收以[唯一数据库规范](docs/PostgreSQL唯一数据库实施与验收规范.md)及 CW-001—060 为准。此前仅客户生产 PG、默认开发 SQLite、SQLite 业务测试可作为当前验收的口径不再适用。
> 本次更新只确认规范和任务定义；原代码仍有 SQLite 分支，历史任务/测试记录保留原文，不据此声明实际迁移或生产切换已完成。

> 面向任何接手本仓库的 AI 开发代理（ChatGPT/Codex 云端、本地 CLI、IDE agent）。
> 完整协作流程与最新进度快照见 `docs/ChatGPT网页端开发交接提示词-V3.md`；任务进度一律以仓库账本为准，本文件不维护进度快照。

## 项目速览

- 产品：短视频复刻工作台（参考视频上传 → AI 拆解分镜 → 人物库/首帧 → Prompt/批次 → H3 视频生成 → COS 归档质检 → 钱包计费）。
- 两条线：内部 P0 单机版已完成收口（SQLite + 内部身份 + 单机 Worker）；当前主线为客户版 V3（任务 T01–T42）：PostgreSQL 全量迁移 → 激活码与首充 → 两设备单在线 → 用户公平队列 → 多实例生产与灰度。
- 技术栈：Python 3.12 / FastAPI / Alembic / pytest（`server/`）；React 19 / TypeScript 5.9 / Vite 8 / Biome / Vitest（`client/`）；Tauri 2 / Rust（`client/src-tauri/`）；PG 层用 psycopg3 同步驱动（`%s` 占位符）。

## 必读正本（按序，冲突时序号小者优先）

1. `docs/客户版任务清单-V3.md` —— 唯一任务状态账本（DoD、红线、§12 工作包、§14 证据模板）
2. `docs/客户版代码开发清单-V3.md` —— 唯一文件映射（新文件名已冻结，不得自创）
3. `docs/客户版开发计划-V3.md` —— 里程碑与禁止并行项
4. `docs/客户版激活码完整开发文档-V3.md` —— 业务与架构正本
5. `docs/客户版测试与验收规格-V3.md` —— 测试与验收正本
6. `docs/CUSTOMER-TASK-EVIDENCE-V3.md` —— 证据账本

`docs/剩余开发工作清单.md`、`docs/Windows内测与运维手册.md` 等为历史快照，不得作为实施依据。

## 标准工作流（每任务）

1. 从最新 main 切分支 `feat/customer-v3-tXX-短横线描述`；同一时间只开一个任务分支（多分支同改账本会连环冲突）。
2. 测试先行：先写失败测试（红），再实现（绿）。
3. push 前核对提交清单（防并发会话污染任务 PR）：先 `git fetch origin --prune`，再确认 `git log origin/main..HEAD --oneline` 中是且仅是本任务的提交；发现其他会话/他人的提交时，先摘出到独立分支（或与所有者确认处理方式），严禁混入任务 PR——squash 合并后外来提交将不可逆地进入 main（2026-08-22 曾有并发会话提交混入 PR #39）。push 后、合并前再核对一次 PR 的 Commits 列表。
4. 全量验证零回归后提 PR：标题 `TXX: <英文摘要>`；三门禁 CI 全绿（secret 扫描 / Linux 质量门 / Windows NSIS）；受保护 main 仅接受 squash merge（所有者账号 phlong026）。
5. 同一 PR 内更新账本：任务清单任务状态 + 头部状态行 + §12 工作包状态、`docs/CUSTOMER-TASK-EVIDENCE-V3.md` 登记、`docs/evidence/TXX-EVIDENCE.md`（证据文件统一存放于 `docs/evidence/`，结构参照 `docs/evidence/T06-EVIDENCE.md`，含 §14 模板全文；2026-08-21 M0 评审 M8 起不再放仓库根目录）。
6. 一个 PR 只承载一个任务；评审评论逐条实质修复后 resolve，不得当作流程噪音跳过。
7. 需人工授权的动作（真实 ZPay / 付费 Provider / 生产 COS 变更 / 对外发码 / 灰度扩大 / 公网发布）必须先取得用户明确授权。

## 硬红线（摘要）

- 迁移文件名冻结：`025_postgres_runtime_compatibility` … `030_user_fair_queue`；已发布 revision 只可追加修复，不得篡改。
- 禁止引入 ORM、Redis、消息队列框架；禁止 SQLite/PG 双真源与双写。
- 并行红线：T13 不得早于 T08/T10；T20 不得早于 T19；T21 逐写路由验证 fencing；T25 公平队列 PG-first；T36 不得单实例冒充多实例；T40 真实付费需人工授权。
- 任何真实 API key、激活码明文、设备/session token 不得进入代码、日志、测试夹具或 PR。
- 证据层级逐级推进（`CODE_PRESENT → AUTOMATED_VERIFIED → STAGING_VERIFIED → REAL_CHAIN_VERIFIED → PRODUCTION_GO`）；未过真实链路不得标 `PRODUCTION_GO`。

## 验证命令（分层：开发期快速反馈，任务收尾全量门禁）

> **分层原则**：全量 pytest（账本口径 1446+ 用例，历史全绿耗时约 21–30 分钟）每任务只跑**一次**——收尾提交 PR 前，由第 3 步的 `npm run check` 统一承载（其脚本末尾已包含服务端全量 pytest；不要在第 2 步再单独跑一遍全量，否则一次收尾 = 两次全量）。开发期每轮迭代只跑受影响专项（秒级）。**严禁两个全量 pytest 实例同时打同一个 PG fixture**（各 PG 测试文件有独立库，但 5 个文件共享 `customer_v3_test`，并发会互踩造成假性失败——2026-08-23 T19 实测教训）。注意：`cargo test`、`npm audit`、客户浏览器 E2E 与 `npm run build` 四项**只在 CI 三门禁里执行**，本地 `npm run check` 不含——涉及 Rust/构建/依赖变更时以 CI 结果为准。

### 开发期（每任务每轮迭代，快）

```bash
# server/ 目录；CW-007 硬门：fixture 未启动时 PG 套件直接失败（非 skip）。
# 开发期快速反馈可显式放行跳过（不得用于验收证据）：
#   VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1 uv run python -m pytest tests/test_<受影响文件>.py -q
uv run python -m pytest tests/test_<受影响文件>.py -q   # 只跑专项，秒级
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

### 任务收尾（每任务一次，PR 前必须全绿）

```bash
# 1) 先启动 PostgreSQL fixture（Docker PG16，端口 5433；脚本必须带子命令，无参数会打印 usage 并退出 1）
scripts/pg-fixture.sh start

# 2) 服务端专项复验（server/ 目录；全量 pytest 不在这一步跑——它由第 3 步统一承载，避免双跑；
#    CW-007 硬门：fixture 未启动时 PG 套件失败而非 skip，缺库伪绿/缺库 skip 均不得声明 AUTOMATED_VERIFIED）
uv run ruff check . && uv run ruff format --check . && uv run mypy app

# 3) 全仓门禁（仓库根目录，等价于 CI Linux 质量门，覆盖前端/Tauri/服务端全套；
#    其脚本末尾含服务端全量 pytest——这是每任务唯一的一次全量）
npm run check

# 收尾：scripts/pg-fixture.sh stop；DSN 覆盖用环境变量 TEST_POSTGRESQL_URL
```

## 环境变量备忘

- PG唯一数据库目标：所有运行环境显式配置`VIDEO_REPLICA_DATABASE_URL`；SQLite/DB_PATH/缺URL/不可达PG拒绝启动，不依赖生产开关。现有代码尚需CW-025/042实现全环境约束。
- 旧`VIDEO_REPLICA_DB_PATH`/内部固定身份仅作迁移识别，不再作为推荐开发路径；业务测试遵循TEST-*分类，SQLite历史例外不得承载当前业务。
- 密钥只进服务端密钥存储（Fernet 加密），永不入库、入码、入 PR。
