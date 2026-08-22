# T07 — SQLite → PostgreSQL 一次性导入、对账与回滚窗口（DB-05 / DB-06）

> 状态：`AUTOMATED_VERIFIED`。SQLite → PostgreSQL 一次性导入、对账、回滚契约及三门禁已自动化验证；未执行真实生产存量库切换。

## 任务信息

| 字段 | 值 |
| --- | --- |
| 任务 | T07 / DB-05 / DB-06 |
| Owner | DB / Backend Agent |
| Reviewer | chatgpt-codex-connector + 独立最终复核 |
| 分支 | `feat/customer-v3-t07-sqlite-postgres-import` |
| 基线 | `main@35e341833e1de3096d1728c98375523d1dd46982` |
| 当前实现 SHA | `c26bc0732d9fe66142dae3c50ac9c908bdf578a8` |
| 日期 | 2026-08-21 |
| 当前证据层级 | `AUTOMATED_VERIFIED`；真实生产切换与 staging 待验证 |

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

Blob SHA 随 PR #38 第二轮评审修复刷新（writer fence / bounded errors / SQL 化对账 / 计数化报告）；
最终以 squash 合并结果为准。

| 文件 | Git blob SHA |
| --- | --- |
| `server/app/backup.py` | `18b3008cf3d2083565af46e64e69c7ed3ca92282` |
| `server/scripts/reconcile_customer_billing.py` | `65b551b9656a2b65658ccd460b5a1523a42766d0` |
| `server/scripts/sqlite_to_postgres.py` | `ea20e9f31fe71f034475a89b24adee559860c97c` |
| `server/tests/test_sqlite_to_postgres.py` | `2e2a14a241e1858caaaf0e03b1651ab77a0008f7` |

临时展开载荷与一次性 workflow 已在同一分支提交中自删除，不属于 PR 最终差异。

## PR #38 第二轮评审修复（2026-08-22）

| 等级 | 问题 | 处置 |
| --- | --- | --- |
| P1 | `final_stat` 在最终哈希之前采样且无 writer fence，存在 TOCTOU 窗口 | 新增 `_writer_fence`：从首次 hash 到元数据计算完成全程持有源库 `BEGIN IMMEDIATE`（意外恢复的 writer 提交即 SQLITE_BUSY；进程崩溃锁自动释放；不写库故不产生 journal）；最终复核改为 hash→stat 顺序；快照哈希计算移入 fence 内 |
| P1 | `str(error)` 可能携带业务行（PG `DETAIL: Failing row contains (...)`、存储 URI、token 摘要），现有脱敏只移除 DSN 派生串 | `safe_error_message` 改为 allowlist：仅本工具守卫异常（RuntimeError/FileNotFoundError/FileExistsError/PermissionError）保留 verbatim 消息（仍 DSN 脱敏 + 600 字符硬截断）；其余异常降级为「异常类名 + 阶段提示」，两个 CLI 均接入 `stage=` |
| P2 | 钱包/充值/账务轮次/owner/资产 ID 检查仍用 `fetchall()` 物化全表 | 五组不变量全部改为 SQL 聚合/反连接（LEFT JOIN + COALESCE、NOT EXISTS、SUM(CASE)/COUNT(DISTINCT)）；JSON 资产引用改为数据库端展开（SQLite `json_valid`/`json_type`/`json_each`；PG16 `pg_input_is_valid` + `jsonb_array_elements_text`，`CASE` 守卫防止对非数组求值抛错）；Python 端不再物化任何业务表或资产 ID 集合 |
| P2 | `wallet_balance_mismatch` detail 携带精确余额（actual/expected） | 全部 invariant detail 计数化：仅报告不匹配条数，不含任何业务值 |
| P2 | 证据文件完整性哈希仍指向旧 `4b3a9bb` 版本 blob | 已按本轮修复后的最终实现重新生成（见上表） |

附带发现：源库持久化 `journal_mode=WAL` 标志（header 字节 18/19）在 WAL 文件被清理后仍存在，
普通连接（含 fence）打开会重新产生 `-wal`/`-shm`。新增 `_assert_delete_journal_mode`
（直读 header，不开连接）fail-closed：维护窗口契约明确要求源库以 DELETE 模式收尾
（`wal_checkpoint(TRUNCATE)` + `journal_mode = DELETE`，与 `_create_head_source` 实践一致）。

## 当前专项验证

本地隔离验证（真实 PG16 fixture 已启动，`scripts/pg-fixture.sh start`）：

```text
uv run python -m pytest tests/test_sqlite_to_postgres.py   → 24 passed（含 5 项真实 PG16 集成）
uv run python -m pytest tests -q                            → 634 passed, 1 warning
ruff check . / ruff format --check .                       → 全部通过（122 文件）
mypy app                                                   → 53 源文件无问题
```

第二轮评审新增红绿测试：fence 阻止恢复 writer 提交、fence 阻止并发快照（释放后可重试）、
绕过 SQLite 锁的直接文件突变仍被 hash→stat 终检捕获、非 allowlist 异常不泄露业务值、
余额 detail 仅计数、generation billing 三类漂移（round/owner/gap）检测。

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

## 正式 CI 与评审收口

- GitHub Actions Run #189 在实现 Head `c26bc0732d9fe66142dae3c50ac9c908bdf578a8` 上完成：
  `Secret scan`、`Linux quality gate`、`Windows Tauri and NSIS` 全部成功。
- Linux 门禁使用 PostgreSQL 16.15 service：客户端 24 个测试文件、324 项测试通过；
  服务端 628 项通过、1 项既有非 T07 用例跳过；`test_sqlite_to_postgres.py` 19 项全部通过。
- 同一 Linux 门禁确认 Ruff 检查通过、122 个文件格式合规、mypy 53 个源文件无问题、
  Rust 测试通过、Web 构建成功、`npm audit --audit-level=high` 为 0 个漏洞。
- Windows 门禁完成 Tauri 检查、unsigned NSIS 构建、SHA-256 记录与产物上传。
- 证据账本写入前置于 Run #195 的三门禁全部成功；6 个既有评审线程均逐条回复并 resolve。
- 结论仅升级至 `AUTOMATED_VERIFIED`，不把自动化证据冒充真实生产迁移。

## DB-06 维护窗口与回滚契约

- R0：保留现有内部 P0 release/tag 和原 SQLite 数据文件，不覆盖、不删除；
- 停写：停止 API/Worker/桌面写入，关闭所有 SQLite 连接，确认 WAL/SHM/journal 均不存在；
- 快照：输出独立 `0600` 文件和 SHA-256，目标存在即停止；
- 导入：目标 PG 必须为空或仅有明确 seed；禁止合并分叉状态；
- 失败：PG 单事务自动回滚，SQLite 快照和 R0 保留；
- R1：未开放客户流量前，可停用 PG 服务并恢复旧 P0；PG 作为未开放影子库保留调查；
- 账务：已确认账务流水不得通过数据库回滚静默删除，后续退款/补账只能走审计化业务流程。

## §14 任务记录（完成）

```text
任务/工作包：T07 / DB-05 / DB-06
Owner / Reviewer：DB/Backend Agent / chatgpt-codex-connector + 独立最终复核
分支 / 基线 SHA：feat/customer-v3-t07-sqlite-postgres-import / 35e341833e1de3096d1728c98375523d1dd46982
上游规格段落：客户版任务清单 V3 §2 T07、§12.1 DB-05/DB-06；代码开发清单 V3 §8.3
改动文件：server/app/backup.py、server/scripts/reconcile_customer_billing.py、server/scripts/sqlite_to_postgres.py、server/tests/test_sqlite_to_postgres.py、docs/evidence/T07-EVIDENCE.md
失败测试或回归锁定：快照权限/覆盖/WAL、JSON 资产引用、增量指纹、导入/重复/事务回滚/分叉目标/版本前置测试
实现结果：一次性快照、导入、全量对账、重复执行、事务回滚与维护窗契约已落盘并通过自动化门禁
验证命令与通过数：本地专项 10 passed、4 PG skipped；GitHub Actions Run #189 三门禁全部成功，PG16 集成在 Linux 门禁真实执行
证据层级：AUTOMATED_VERIFIED
安全与可观测性：快照 0600；DSN 脱敏；报告只含计数/摘要；迁移失败 fail-closed
迁移与回滚：R0/R1、禁止双写、单 PG 事务、快照不可覆盖
外部授权记录：无；未调用生产数据库、COS、ZPay 或付费 Provider
未测试项：真实生产存量库切换、类生产维护窗耗时、STAGING/REAL_CHAIN/PRODUCTION 验证
Lore 提交 SHA：PR #38 implementation head c26bc0732d9fe66142dae3c50ac9c908bdf578a8；最终 squash SHA 以 GitHub merge 结果为准
```
