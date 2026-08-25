# M5 代码评审报告与修复证据(客户版 V3,2026-08-25)

> 分支:`feat/customer-v3-m4-consolidation`
> 评审基线:`9e41673`(M4 gate)之后的 M5 四提交:T25 `0618701`、T26 `2096345`、T27 `06ce034`、T34-E2E `756f097`
> 修复提交:`dffb29a`(20 文件,+580/−135)
> 评审方法:分层扫描(正确性/并发/迁移/可移植性/测试形状)→ 置信度门限与指纹去重 → 专家对抗核验(事务边界、WAL、迁移拓扑)

---

## 一、发现汇总

| 编号 | 级别 | 主题 | 结论 |
|---|---|---|---|
| P1-1 | P1 | worker 单 fenced 事务滚全部轮:崩溃回滚丢付费调用、任务回 PENDING 被重发 | **修复(两段式事务)** |
| P1-2 | P1 | `mark_task_submission_uncertain` 转换时未释放用户槽位(活 worker 路径) | **修复** |
| P1-3 | P1 | 迁移拓扑:032 改挂 030,既有已 stamp 040 的库 `upgrade head` 静默跳过公平队列 schema | **修复(041 重挂链尾)** |
| P1-4 | P1 | 队列游标维护用模式开关而非连接通道门控,开关翻转瞬间数据不一致 | **修复(is_postgres)** |
| P1-5 | P1 | reconcile 路径 `_store_and_finalize_archive` 释放槽位会清零替代任务的槽位 | **修复** |
| P1-6 | P1 | `_acquire_global_fifo_lease`/character 领取在 READ COMMITTED 下无双领防护 | **修复(SKIP LOCKED)** |
| P1-7 | P1 | `_fair_queue_enabled` 吞一切异常,PG 未迁移时静默降级 | **修复(fail-loud)** |
| P2-1 | P2 | 并发 sweeper 败者会重复审计/做 stale 释放 | **修复(UPDATE…RETURNING)** |
| P2-2 | P2 | 空游标轮转退化为首个用户轮转(非缺陷,文档级 follow-up) | 记录,不入代码 |
| P2-3 | P2 | SQLite 通道 worker 不重启不重读游标(桌面单进程,非缺陷) | 记录,不入代码 |

## 二、修复映射

### P1-1 两段式任务事务(事务边界)

`app/generation_worker.py::run_pg_worker_once` 重构为每任务两段 fenced 事务:

1. **领取**:短事务内 `acquire_generation_task_lease` 立即提交 SUBMITTING 转换、每用户槽位 +1、全局并发门禁 +1。崩溃后任务**持久停留 SUBMITTING**。
2. **工作**:第二个事务内付费 Provider 轮询、终态写、槽位释放。领取的持久性使这里是崩溃恢复边界而非重试点。

崩溃语义:SUBMITTING 停留 → 租约过期 → `mark_expired` → `SUBMISSION_UNCERTAIN`(人工确认门,不静默重提)。`run_next_generation_task`/`run_next_character_generation_task` 新增预领取 `lease` 参数支持两段式。

回归测试:`test_crash_after_claim_leaves_durable_submitting`(原 `test_crash_inside_fenced_transaction_rolls_back_everything` 重写,断言 SUBMITTING 持久 + mark_expired → UNCERTAIN + 游标 0 + 下次领取不重发)+ 6 个新 worker 循环/并发 sweeper 测试。

### P1-2 入口释放

`mark_task_submission_uncertain` 的 UPDATE 后补 `release_user_queue_slot_for_task(conn, task_id=task_id)`(活 worker 路径;过期路径已由 `mark_expired` 释放)。注释说明两条路径的槽位释放分工。

### P1-3 迁移拓扑修复

- `030_user_fair_queue.py` → git mv → **`041_user_fair_queue.py`**,`down_revision = "040_fix_provider_settings_constraint"`,挂链尾
- `032_security_rate_limits.py` 恢复 `down_revision = "029_customer_sessions_and_idempotency"`(与发布版一致)+ 注释说明原改挂会在已 stamp 032–040 的库上静默跳过公平队列 schema

新链:`027 → 031 → 028 → 029 → 032 → … → 040 → 041`(等价于"040 发布态 + 041 新尾"——对既有 040 库,041 不在祖先路径上,**会执行**;对全新库,链覆盖全 schema)。

新增升级路径测试 `test_pg_upgrade_from_published_040_head_applies_fair_queue`:

- `upgrade "040_fix_provider_settings_constraint"`(模拟发布库)→ 断言无 `fair_queue_enabled` 列、无 `user_queue_cursors` 表
- `upgrade head` → 断言 `version_num == 041_user_fair_queue`、`user_queue_cursors` 表存在、`fair_queue_enabled` 列存在

### P1-4 按通道门控

`BusinessConnection.is_postgres` 属性(探针 backend 实例)。`ensure_user_queue_cursor`/`release_user_queue_slot_for_task`/`cleanup_idle_queue_cursors` 改用 `if not conn.is_postgres: return`,开关翻转瞬间数据一致(观察期游标也维护)。

### P1-5 对账不释放

`_store_and_finalize_archive` 释放改 `if reconcile_reservation is None: release_user_queue_slot_for_task(...)`——reconcile 路径的替代任务槽位由 reconcile 自身维护,再释放会清零。

回归:`_reconcile` helper 重构为与 API 同形(先插 `generation_task_operations` 行 `action='RECONCILE'`、传 `ReconcileReservation`),`test_reconcile_path_preserves_alternate_slot` 断言游标保持 1。

### P1-6 SKIP LOCKED

`_acquire_global_fifo_lease` 与 `acquire_character_generation_task` 的领取子查询加 `LIMIT 1 FOR UPDATE SKIP LOCKED`。SQLite 通道由 `db_portable.py` 翻译器剥离(`(?i)FOR UPDATE(?:\s+SKIP LOCKED)?`),行为不受影响。

回归:公平轮转测试 `test_rotation_skips_user_whose_task_is_locked`(holder 连接持锁未提交 → 轮转到下一用户;释放后可再领取)。

### P1-7 fail-loud

`_fair_queue_enabled` 异常处理窄化:仅缺列类错误(`does not exist`/`no such column`)返回 False(对应 SQLite 无表 / PG 未跑 041),其余异常 `logger.warning` + `raise`。

### P2-1 UPDATE…RETURNING

`mark_expired_active_leases_needing_attention` 重构:`archive_rows`/`uncertain_rows` 两个 `UPDATE … RETURNING (id, batch_id)`,各自循环写 `generation_task.lease_expired_archive_retry`/`generation_task.lease_expired_uncertain` 审计 + 槽位释放。并发 sweeper 败者 UPDATE 0 行 → 不重复审计、不 stale 释放。批次刷新改为 set 去重,置于 `with conn:` 事务外。

回归:并发 sweeper 测试断言败者不写审计、不释放二次。

## 三、验证矩阵(修复后全量)

| 批次 | 范围 | 结果 |
|---|---|---|
| crash recovery(worker 循环/两段式/UNCERTAIN/reconcile) | SQLite+PG | 13 ✓(+新测试) |
| customer queue fairness | PG | 11 ✓ |
| queue_load_10k(T27 重负载) | PG | 3 ✓ |
| chain_e2e(T34) | PG | 2 ✓ |
| postgres migrations(含新升级路径测试) | PG | 12 ✓ |
| generation/character/dialect | SQLite | 119 ✓ |
| db/activation/billing/settings/characters | SQLite | 111 ✓ |
| character_domain/devices/security/recharge | SQLite | 130 ✓ |
| 静态门禁 | ruff check + ruff format(本次 diff 内)+ mypy app/ | 全部通过 |

注:mypy strict 下 `tests/` 存在 972 个历史类型错误(40 文件,多为 psycopg `fetchone()[0]` 模式),非本次引入、非本项目门禁;`app/` 零错误。

## 三.5 CI 门禁 P0:test_db_pg 过时测试导致 pytest 卡死(修复)

**现象**:PR #61 的 Linux quality gate 在 pytest 全量 51%(test_db.py 完成后)卡死,连续两个 run(32805295363、32807178721)均 30 分钟 timeout。取消前 24 分钟无任何输出。

**根因**(本地 Windows 直接复现:`test_db.py + test_db_pg.py` 90 秒内复现,排除 locale/pipe 假说):
- `test_db_pg.py::test_worker_main_ready_check_in_pg_mode`(M4 时代,M0 review H1 语义)patch 了 `run_forever`/`run_worker_once`(SQLite 循环),期望 main() 在 PG 模式以非零码 SystemExit——当时 PG 循环未实现,不允许"空转 worker 被视为健康"
- M5 T25 实现了 PG 循环:main() 现在调用 `run_forever_pg` —— 测试 patch 落空,真函数进入无限循环
- 测试环境 storage settings 未配置 → 每轮 `get_media_storage` 抛 `STORAGE_SETTINGS_UNAVAILABLE` → ERROR 刷屏 + sleep(1.0) 自愈重试 → 永不返回 → pytest 卡死
- 本地 M5 验证时 PG 未运行 → `@pytestmark_pg` skipif 跳过该测试 → 未暴露;CI 有 PG service → 真实执行 → 卡死

**修复**:重写为 `test_worker_main_dispatches_to_pg_forever_loop` —— patch `run_forever_pg`,断言 main() 在 PG 模式调度到 T25 公平队列循环而非 SQLite 循环;保留 pool 生命周期断言。本地原卡死组合 47 passed 31s;M5 相关文件(worker/fair queue/10k/e2e)24+5 passed。

**评审教训**:功能落地(T25)改变入口行为时,依赖旧行为的 gate 测试必须同步迁移;仅本地验证 skipif 路径会漏掉 CI 专属路径。

## 三.6 CI 门禁 P1:T25 新表/新列未进 T07 导入契约(修复)

**现象**:CI 卡死修复后(本地全量)暴露 `test_sqlite_to_postgres.py` 5 个失败,`MigrationSafetyError: source/target table contract differs`。

**根因**:041 迁移在 PG 侧创建 `user_queue_cursors`(PG-only)并向共享表 `runtime_settings` 加 `fair_queue_enabled` 列;`scripts/reconcile_customer_billing.py` 的 `PG_ONLY_TABLES` 白名单和 `_validate_schema`/`_table_reconciliation` 的列集合比较未同步,把自有迁移的产物当成"目标侧非法差异" fail-closed。CI 卡死掩盖了它(该文件在字母序后段,CI 从未跑到)。

**修复**:`PG_ONLY_TABLES` 加入 `user_queue_cursors`;新增 `PG_ONLY_COLUMNS = {"runtime_settings": {"fair_queue_enabled"}}` 豁免共享表的 PG-only 列,两处列比较统一豁免。本地 test_sqlite_to_postgres 30 ✓。

**评审教训**:给既有表加 PG-only 列/新表时,必须同步 T07 导入契约(白名单 + 列豁免);这是 M5 评审未覆盖的脚本层契约。

## 四、follow-up 清单(不入代码,记录在案)

- **P2-2 空游标轮转**:全部游标 0(无活跃任务)时轮转退化为首位用户;因 PG 领取有 SKIP LOCKED + 无任务即无行,无放大效应。如未来引入"用户级负载均衡"需求再处理。
- **P2-3 SQLite stale worker**:SQLite 通道 worker 长驻不重读游标;桌面单进程 + 单 worker,非缺陷。若未来桌面多 worker 需加游标刷新周期。

## 五、PR 基线说明(需用户知悉)

- **main 已前移**:`30e90c6`(main 领先评审基线;M4 gate `9e41673` 为评审基线 HEAD~4)
- **M4 合并分支未并入 main**:`feat/customer-v3-m4-consolidation` 上的 M4 提交已合并,但对应 PR 的合并提交不在当前 main 上(用户侧操作)。本分支基于合并前状态,开 PR 时需以合并后的 main 为准或先 rebase。
- PR 提交:`dffb29a`(fix)+ `756f097`/`06ce034`/`2096345`/`0618701`(M5 四提交)。合并由用户决定。
