# T07 — SQLite → PostgreSQL 一次性导入、对账与回滚窗口（DB-05 / DB-06）

> 状态：`IN_PROGRESS`。本文件先固化评审修复与本地专项验证；GitHub Actions PG16、Linux、Windows 三门禁通过后再更新为最终证据。

## 任务信息

| 字段 | 值 |
| --- | --- |
| 任务 | T07 / DB-05 / DB-06 |
| Owner | DB / Backend Agent |
| Reviewer | chatgpt-codex-connector + 独立最终复核 |
| 分支 | `feat/customer-v3-t07-sqlite-postgres-import` |
| 基线 | `main@35e341833e1de3096d1728c98375523d1dd46982` |
| 当前实现 SHA | `4b3a9bb6c543d350a102802ee73dba37ad578bd8` |
| 日期 | 2026-08-21 |
| 当前证据层级 | `CODE_PRESENT`；CI 与真实 PG16 集成待验证 |

## 目标与边界

T07 只提供维护窗口中的一次性迁移，不建立 SQLite/PostgreSQL 双写：

1. 对已停止写入并完成 checkpoint 的 SQLite 建立只读、不可覆盖、权限 `0600` 的快照；
2. PostgreSQL 必须位于同一 Alembic head、外键全部 validated、且不得包含分叉的非种子数据；
3. 按外键依赖顺序分批导入，所有写入位于单一 PostgreSQL 事务；
4. 对账表行数、主键集合、规范化行指纹、钱包、PAID/CHARGE、生成任务账务轮次及资产引用；
5. 任一校验失败整体回滚；再次执行完全一致的目标返回 `already_reconciled`；
6. 不输出 DSN 密码、原始业务行、存储 URL、token 或其他敏感值。

## 已关闭的 PR 评审风险

| 等级 | 风险 | 实现处置 |
| --- | --- | --- |
| P1 | 测试与实现导出 API 不一致，pytest 无法收集 | 测试与公开迁移 API 统一；本地测试已完成收集和执行 |
| P1 | WAL 提交可能只改变 `-wal`，主库哈希无法发现 | 快照前后拒绝任何 `-wal` / `-shm` / `-journal`；源库以 `mode=ro&immutable=1` 打开，并复核 hash/size/mtime/data_version |
| P1 | `os.replace` 覆盖既有回滚快照 | 目标已存在直接 `FileExistsError`；以同目录 hard-link 原子发布且禁止覆盖 |
| P1 | 快照默认 `0644` 泄露客户数据 | 临时文件通过 `O_EXCL` 以 `0600` 创建，发布后再次 chmod 与权限断言 |
| P1 | JSON 数组中的资产引用未校验 | 覆盖 `reference_asset_ids_json`、`recommended_asset_ids_json`、`selected_asset_ids_json` 及通用 `*_asset_ids_json`；格式错误或孤儿引用 fail-closed |
| P2 | 对账把最大表全部加载到内存 | SQLite `fetchmany`、PostgreSQL server-side named cursor；使用有界内存、顺序无关且保留重复计数的增量多重集指纹 |

## 文件与完整性

| 文件 | Git blob SHA |
| --- | --- |
| `server/app/backup.py` | `29cad8a1b3ca7405d7dfc60bcdf285a09ff43dd2` |
| `server/scripts/reconcile_customer_billing.py` | `fa7518e246a69f43863c5cfe21ea3f157bab4c9e` |
| `server/scripts/sqlite_to_postgres.py` | `00fe2666182e4982a8ea2ccb40bb7b75dd1dd885` |
| `server/tests/test_sqlite_to_postgres.py` | `cad23d2ab50a13101092916705a6de2feac9e2ab` |

临时展开载荷与一次性 workflow 已在同一分支提交中自删除，不属于 PR 最终差异。

## 当前专项验证

本地隔离验证（外部依赖用最小导入 stub，仅验证本任务纯 Python 逻辑和测试可收集性）：

```text
python -m py_compile <4 个 T07 文件>                         → PASS
AST 未使用 import 扫描                                      → 0
100 字符行长扫描                                             → 0
pytest server/tests/test_sqlite_to_postgres.py               → 10 passed, 4 skipped
```

4 个 skipped 为依赖真实 PostgreSQL 16 的集成用例，必须由 CI fixture 执行后方可升级证据层级：

- 一次性导入、完整对账、重复执行；
- 注入失败后的全事务回滚；
- 分叉目标与 Alembic revision 不一致 fail-closed；
- 主键/行/钱包漂移；
- JSON 资产孤儿在写入前拒绝。

## 独立安全评审新增红绿证据（2026-08-21）

- DSN 回归：query/fragment 和非法端口不得泄露凭据；修复前专项测试为 `1 failed`，修复后通过。
- 并发回归：两个 PostgreSQL 连接竞争同一 T07 导入时，第二个连接必须由事务级 advisory lock 立即失败关闭；实现前导入符号缺失红测，实现在 PG16 双连接测试中通过。
- 目标侧 JSON 资产引用：导入后篡改 `characters.reference_asset_ids_json` 为孤儿引用，对账必须报告 `target:characters.reference_asset_ids_json`。
- PG16 首次绿测因测试夹具遗留 WAL/SHM 共 `5 failed`；第二次因 SQLite 上下文未关闭连接导致切换 journal mode 时 `database is locked`。夹具现显式关闭连接、checkpoint 并切回 DELETE，生产维护窗口门禁保持不变。
- PG16 第三次绿测进一步发现 `psycopg.Connection` 无 `executemany()`，结果 `2 failed, 14 passed`；实现改为事务连接内 `cursor.executemany()`，继续由真实 PG16 导入覆盖。
- 三个原始格式失败文件已由仓库锁定 Ruff 版本格式化；最终证据层级仍以正式 PR 三门禁为准。

## 最终快照原子性评审红绿证据（2026-08-21）

- 红测：SQLite 上下文只提交、不关闭；hard-link 已发布后临时文件删除失败会残留目标；最终源哈希检查后仍可发生写入。三个专项回归结果为 `3 failed`。
- 绿测：快照源/目标连接及迁移、对账只读连接均显式关闭；发布清理失败时回删目标 link；发布边界后再次检查 sidecar、hash、size、mtime，竞态失败时删除已发布快照。
- PostgreSQL 16 下完整 T07 专项测试在修复后通过；最终任务状态仍以标准三门禁为准。

## DB-06 维护窗口与回滚契约

- R0：保留现有内部 P0 release/tag 和原 SQLite 数据文件，不覆盖、不删除；
- 停写：停止 API/Worker/桌面写入，关闭所有 SQLite 连接，确认 WAL/SHM/journal 均不存在；
- 快照：输出独立 `0600` 文件和 SHA-256，目标存在即停止；
- 导入：目标 PG 必须为空或仅有明确 seed；禁止合并分叉状态；
- 失败：PG 单事务自动回滚，SQLite 快照和 R0 保留；
- R1：未开放客户流量前，可停用 PG 服务并恢复旧 P0；PG 作为未开放影子库保留调查；
- 账务：已确认账务流水不得通过数据库回滚静默删除，后续退款/补账只能走审计化业务流程。

## §14 任务记录（进行中）

```text
任务/工作包：T07 / DB-05 / DB-06
Owner / Reviewer：DB/Backend Agent / chatgpt-codex-connector + 独立最终复核
分支 / 基线 SHA：feat/customer-v3-t07-sqlite-postgres-import / 35e341833e1de3096d1728c98375523d1dd46982
上游规格段落：客户版任务清单 V3 §2 T07、§12.1 DB-05/DB-06；代码开发清单 V3 §8.3
改动文件：server/app/backup.py、server/scripts/reconcile_customer_billing.py、server/scripts/sqlite_to_postgres.py、server/tests/test_sqlite_to_postgres.py、docs/evidence/T07-EVIDENCE.md
失败测试或回归锁定：快照权限/覆盖/WAL、JSON 资产引用、增量指纹、导入/重复/事务回滚/分叉目标/版本前置测试
实现结果：代码已落盘；CI PG16、Linux、Windows 三门禁待运行
验证命令与通过数：本地专项 10 passed、4 PG skipped；最终 CI 数量待补
证据层级：CODE_PRESENT
安全与可观测性：快照 0600；DSN 脱敏；报告只含计数/摘要；迁移失败 fail-closed
迁移与回滚：R0/R1、禁止双写、单 PG 事务、快照不可覆盖
外部授权记录：无；未调用生产数据库、COS、ZPay 或付费 Provider
未测试项：CI PostgreSQL 16、全量 server/frontend/Tauri/Windows NSIS
Lore 提交 SHA：最终 squash merge 后补录
```
