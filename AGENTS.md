# AGENTS.md — 面向 AI 开发代理的工程约定

本文件只描述**工程约定**：技术栈、目录职责、验证命令与编码红线。
产品说明、快速开始与文档索引见 [README.md](README.md)。

## 项目速览

- 产品：短视频复刻工作台（参考视频上传 → AI 拆解分镜 → 人物库/首帧 → Prompt/批次 →
  视频生成 → 成片直链交付 → 按条计费）。
- 技术栈：Python 3.12 / FastAPI / Alembic / pytest（`server/`）；
  React 19 / TypeScript 5.9 / Vite 8 / Biome / Vitest（`client/`）；
  Tauri 2 / Rust（`client/src-tauri/`）。
- 数据库：PostgreSQL 16 是唯一业务数据库真源，PG 层用 psycopg3 同步驱动（`%s` 占位符）。

## 目录职责

| 目录 | 职责 |
| --- | --- |
| `server/app/` | 路由、业务服务、Worker、计费与权限 |
| `server/migrations/` | Alembic 迁移；已发布 revision 只可追加修复，不得篡改 |
| `server/tests/` | 服务端测试（PG 用例走 `tests/pg_test_kit.py` 的隔离库） |
| `server/scripts/` | 离线维护与兼容工具 |
| `client/src/` | 工作台与管理端界面、API 适配；组件测试与组件同目录 |
| `client/src-tauri/` | Tauri 桌面外壳（Rust）；不放业务逻辑 |
| `e2e/`、`tests/` | 浏览器端到端验收与根级测试 |
| `scripts/ci/` | 质量门、分片清单与 CI 辅助 |
| `deploy/` | 部署模板、systemd 单元、PG PITR 脚本与发布回滚 |

## 验证命令（分层，避免双跑全量）

开发期每轮迭代只跑受影响专项（秒级）：

```bash
# server/ 目录
uv run python -m pytest tests/test_<受影响文件>.py -q
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

收尾提交前跑一次全仓门禁（等价于 CI 的 Linux 质量门）：

```bash
npm run check:static     # secret 扫描 → 前端 Biome/tsc/vitest → e2e lint → Tauri cargo → Ruff/mypy
npm run check:sharded    # 四分片并行 pytest，每片独立 PG 容器
# 顺序回退：scripts/pg-fixture.sh start && npm run check && scripts/pg-fixture.sh stop
```

- 服务端全量 pytest 每任务只跑一次，由收尾门禁统一承载；不要在开发期重复跑全量。
- 严禁两个全量 pytest 实例同时打同一个 PG 夹具（共享测试库会互踩，产生假性失败）。
- 新增测试文件后必须重生成分片清单：`python scripts/ci/build-test-shards.py --shards 4`。
  清单覆盖是 fail-closed 的，漏一个 `test_*.py` 就会报错。
- `npm run check:full`（`scripts/ci/run-full-gate.sh`）逐段等价复刻 CI 的 Linux
  质量门：sharded 门禁 + 分片清单覆盖断言 + 浏览器 E2E + `cargo test` +
  双前端构建 + `npm audit`。Windows NSIS 打包保留在 CI 手动触发。
- 改动 `client/src-tauri/**` 里 `#[cfg(windows)]` 的代码后，除 `npm run check:tauri`
  外还要跑 `scripts/verify_windows_target.sh`：宿主目标的 `cargo check` 不编译 Windows
  专用代码，而 NSIS 打包在 CI 手动触发——漏编译、类型错误与只在 Windows 运行期发作
  的问题，本地门禁都看不见（2026-10-15「下载不弹保存对话框」即属此类）。

## 本地集成与推送纪律（2026-10-08 起）

GitHub 账号因高频自动化操作被风控封禁后，工作流调整为
「本地门禁 → 本地合并 → 低频推送」，GitHub 降级为备份出口：

- 合入 main 一律走 `scripts/local-integrate.sh <feature-branch>`：在**合并结果**
  上跑 `check:full`（merge-queue 语义），全绿才落 merge commit（message 附门禁
  证据路径与耗时），失败自动还原。不允许手工 merge 绕过门禁。
- 推送纪律：main 每日批量推送 2-4 次（正常人类节奏）；feature 分支默认不推；
  **永久停用 Git Data API push 兜底**；gh api 仅限低频读操作，禁止轮询循环。
- 集成者单点制：合并 main 与推送由当日值守会话独占执行，其他会话只交付
  feature 分支并跑受影响专项。集成窗口（check:full 运行期间）工作区归集成者独占。
- 每日运行 `scripts/backup-bundle.sh`（全引用 git bundle + 滚动保留 14 份），
  补偿低频推送拉长的丢失窗口；bundle 只含已提交内容，工作区成果需及时 commit。
- GitHub Actions 保留为事后验证与 Windows 打包（workflow_dispatch 手动触发），
  不再作为合并前置。

### AI 代码评审纪律（Codex，2026-11-03 起）

自动化门禁验证「代码能否正确运行」，评审验证「代码是否该这么写」——门禁替代不了
评审。合入 main 前由值守集成会话对**合并结果 diff** 调 Codex 评审：

- 评审单元是**变更集**（一个分支合入 main 的整体 diff），不逐提交评审；
  纯 docs/格式化/门禁脚本变更可豁免。
- 按风险分档定深度：**S 级**（资金/计费、会话与数据库迁移、权限、不可逆删除、
  生产发布线）逐行精读且意见逐条闭环；**A 级**（新功能与对外契约变更）全量
  diff；**B 级**（死代码删除、测试修复）抽查，门禁为主防线。
- 时机：集成窗口内、`check:full` 之前；评审未闭环不得起门禁。
- 证据：评审档位与发现项闭环情况写入 merge commit message，与门禁证据并列。

## 编码红线

- 禁止引入 ORM、Redis、消息队列框架；禁止 SQLite/PG 双真源与双写。
- 已发布的 Alembic revision 只可追加修复，不得篡改其内容。
- 任何真实 API key、激活码明文、设备/session token 不得进入代码、日志、测试夹具或 PR。
  密钥只进服务端加密存储（Fernet）。
- 对外响应文案与字段保持中性：不在 API 响应、界面文案与日志里出现数据源供应商名称。
- 中文注释与中文错误文案；注释解释**为什么**，不复述代码在做什么。

## 提交与评审

分支命名、提交信息与 PR 流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 说明

本仓库公开发布，**任务台账、验收证据与排班记录不在仓库内**（有意排除，见 `.gitignore`）。
代码与文档里出现的 `CW-` / `T-` 编号指向项目内部的任务跟踪系统；看到这类编号时按
「某个已归档任务的编号」理解即可，不要尝试在仓库里寻找对应文件。
