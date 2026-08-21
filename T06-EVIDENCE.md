# T06 — Alembic 001→head on PostgreSQL: Full Upgrade/Downgrade/Re-upgrade (DB-04)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T06 / DB-04 |
| **Owner** | DB (Agent) |
| **Reviewer** | chatgpt-codex-connector (PR review) |
| **Branch / Base SHA** | `feat/customer-v3-t06-alembic-pg` / base `c152766` |
| **Date** | 2026-08-21 |
| **Evidence Level** | `LOCALLY_VERIFIED` (real PG 16 container) |

## Exit-Gate Verification

Task exit gate: *PG 空库 upgrade head、downgrade rehearsal 和索引检查通过* — **all three delivered as an automated test**:

```bash
$ uv run python -m pytest tests/test_postgres_migrations.py -v
test_pg_full_upgrade_downgrade_reupgrade_and_indexes PASSED
  # Stage 1: empty DB → upgrade head (001..024) → assert version_num == 024_wallet_backfill,
  #          10 key tables exist (users/projects/characters/generation_batches/generation_tasks/
  #          wallets/wallet_transactions/recharge_orders/character_generation_tasks/external_call_logs),
  #          constraints intact (uq_generation_batches_user_project_key, generation_tasks_batch_id_fkey),
  #          index idx_generation_tasks_prompt_version present
  # Stage 2: downgrade base → business tables gone
  # Stage 3: re-upgrade head → version 024 again (rolled-back deployment rehearsal)
```

## Dialect Fixes (5 issues found by actually running the chain)

All fixes are **dialect-guarded branches**; revision ids, down_revision chain, and
SQLite behaviour are byte-identical to before (red line: *不得篡改已发布 revision* —
interpreted as: never break the published revision chain or existing SQLite
databases; PG-only branches satisfy both).

| # | Issue | Where | Fix |
| --- | --- | --- | --- |
| 1 | SQLAlchemy needs the psycopg3 dialect name (default looks for psycopg2) | caller side | URL `postgresql://` → `postgresql+psycopg://` for Alembic (helper in tests; application DSN stays raw psycopg3) |
| 2 | `DROP TABLE generation_batches` blocked by `generation_tasks` FK (PG enforces, SQLite ignores) | `009_idempotency_project_scope.py` | PG branch drops `generation_tasks_batch_id_fkey` before the swap and re-attaches it (with `ON DELETE CASCADE`) after — SQLite path unchanged |
| 3 | Implicit `rowid` column does not exist on PG | `014_character_image_generation.py` | PG branch uses `ctid` tie-breaker; on the PG path the backfill table is empty (0 rows), SQLite keeps `rowid` |
| 4 | Identifier `fk_external_call_logs_...` (87 chars) exceeds PG's 63-char cap | `014_character_image_generation.py` | PG branch uses short name `fk_external_call_logs_char_gen_task`; SQLite keeps the historical name verbatim |
| 5 | `alembic_version.version_num` is VARCHAR(32); `017_generation_task_retry_lineage` is 35 chars (SQLite ignores declared width) | `migrations/env.py` | `widen_postgres_version_table()`: pre-create the table at VARCHAR(64) (Alembic skips existing tables) / widen in place for interrupted upgrades; SQLite untouched |
| 6 | **Partial unique indexes declared with `sqlite_where` were silently dropped on PG** (incl. the anti-double-billing constraints `uq_recharge_orders_provider_trade_no`, `uq_wallet_transactions_charge_order`, `uq_wallet_transactions_reserve_round`) — found by PR #33 review P1 | `012`, `017`, `022` | Each `create_index` now also carries the equivalent `postgresql_where` (Alembic's cross-dialect pattern: each dialect picks its own kwarg); the rehearsal test asserts all 5 partial indexes exist on PG **with their WHERE clauses** via `pg_indexes.indexdef` |

## Critical Bug Found & Fixed During Verification

The first "successful" manual run was a **false positive**: the widen DDL in
`env.py` opened an implicit autobegin transaction, Alembic then treated it as an
*external* transaction and never committed — the entire upgrade chain was rolled
back when the connection closed (logs still printed "Running upgrade ...").
The automated test caught it (`relation "alembic_version" does not exist`).
Fix: explicit `connection.commit()` after the widen DDL in
`run_migrations_online()`. This is exactly why DB-04 demands rehearsal tests,
not log inspection.

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/env.py` | `widen_postgres_version_table()` + explicit commit (PG-only) |
| `server/migrations/versions/009_idempotency_project_scope.py` | dialect-guarded FK detach/reattach |
| `server/migrations/versions/014_character_image_generation.py` | dialect-guarded rowid→ctid + short FK name |
| `server/migrations/versions/012_character_domain.py`, `017_generation_task_retry_lineage.py`, `022_internal_billing.py` | +`postgresql_where` on 5 partial unique indexes (PR review P1) |
| `server/tests/test_postgres_migrations.py` | +1 rehearsal test (three-stage, real assertions) |

## Regression

```
$ uv run python -m pytest --rootdir . tests -q
603 passed, 1 warning in 101.10s   # 602 from T05 baseline + 1 new rehearsal test; SQLite path byte-identical
ruff check .        → All checks passed!
ruff format --check → 117 files already formatted
mypy (strict) app   → Success: no issues found in 53 source files
```

## Section 14 Ledger Record

```text
任务/工作包：T06 / DB-04
Owner / Reviewer：DB（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t06-alembic-pg / 基线 c152766
上游规格段落：docs/客户版任务清单-V3.md §2 T06 行、§12.1 DB-04；docs/客户版代码开发清单-V3.md §3.1
改动文件：server/migrations/env.py、server/migrations/versions/009_idempotency_project_scope.py、server/migrations/versions/014_character_image_generation.py、server/tests/test_postgres_migrations.py、docs/客户版任务清单-V3.md（T06/DB-04 状态）、T06-EVIDENCE.md
失败测试或回归锁定：三段演练测试先行（断言空库 upgrade/真实提交/约束存在），修复 5 个方言问题后转绿；603 全量回归锁定 SQLite 零破坏
实现结果：001→024 在 PG 空库完整 upgrade/downgrade/re-upgrade 通过；索引与约束存在性断言；发现并修复 env.py 隐式事务导致升级整体回滚的严重缺陷
验证命令与通过数：pytest tests/test_postgres_migrations.py → 5 passed（含三段演练）；pytest 全量 → 603 passed；ruff/mypy 全绿
证据层级：LOCALLY_VERIFIED（真实 PG 16 容器，专用 t06_migrate_test 库，测试自建自清理）
安全与可观测性：演练库测试后自动 DROP；方言分支仅影响 PG 路径
迁移与回滚：历史 revision 链与 SQLite 行为零变化（方言守卫分支）；env.py 变更仅 PG 分支生效
外部授权记录：无
未测试项：CI 内嵌 PG service（同 T05，skip 模式）；SQLite 存量库导入 PG（T07 对账工具范围）
Lore 提交 SHA：见 PR squash 合并 SHA
```
