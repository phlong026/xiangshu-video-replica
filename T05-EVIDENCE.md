# T05 — PostgreSQL DSN, Connection Pool, Transaction Context (DB-03)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T05 / DB-03 |
| **Owner** | DB/Backend (Agent) |
| **Reviewer** | chatgpt-codex-connector (PR review) |
| **Branch / Base SHA** | `feat/customer-v3-t05-db-pg` / base `8130321` |
| **Date** | 2026-08-21 |
| **Evidence Level** | `LOCALLY_VERIFIED` (PG fixture present) |

## Deliverables

### 1. `server/app/db_pg.py` (new, 230 lines)

| Primitive | Purpose | Exit-gate mapping |
| --- | --- | --- |
| `resolve_database_config()` | `VIDEO_REPLICA_DATABASE_URL` → PG mode (`postgresql://`/`postgres://`), SQLite URL/path → legacy mode; unsupported scheme → `ValueError` | DSN entry point |
| `validate_customer_production()` | `VIDEO_REPLICA_CUSTOMER_PRODUCTION=true` + SQLite/missing URL → `RuntimeError` | **"客户生产 SQLite 启动失败"** |
| `get_pg_pool()` / `close_pg_pool()` | psycopg3 `ConnectionPool` (min/max via `VIDEO_REPLICA_PG_POOL_MIN/MAX`, default 1/8), lazy, thread-safe | Connection pool |
| `pg_transaction(isolation=None)` | pooled transaction context; `isolation="SERIALIZABLE"` = BEGIN IMMEDIATE replacement | Transaction context |
| `pg_server_now()` | `SELECT now()` — server-side time only (SES-01) | PG 服务端时间 |
| `check_pg_ready()` | pool warm-up + `SELECT now()` + `SELECT 1` round-trip | API/Worker readiness |

### 2. `server/app/bootstrap.py` (API entry)

Mode resolution happens **before any SQLite file is touched**. PG mode: `check_pg_ready()` + log; SQLite mode: unchanged legacy path. Customer production + SQLite fails closed at startup.

### 3. `server/app/generation_worker.py` (Worker entry)

Same mode-resolution guard. PG mode proves readiness then intentionally stops (fair-queue loop lands with T24/T25 — Lane C); SQLite loop untouched.

### 4. Dependencies (`server/pyproject.toml` + `uv.lock`)

- Added `psycopg[binary,pool]>=3.2` (sync driver — matches the fully synchronous codebase; asyncpg from T03 stays for the fixture-only tests)
- **No ORM, no Redis, no queue framework** (frozen constraint from `docs/客户版代码开发清单-V3.md` §8.1)

## Test Evidence (`server/tests/test_db_pg.py`, 15 tests)

```
tests/test_db_pg.py ............................. 15 passed

DSN resolution (4):
  test_resolve_pg_url_selects_postgres_mode      PASSED
  test_resolve_postgres_scheme_alias             PASSED
  test_resolve_sqlite_fallback_keeps_internal_mode PASSED
  test_resolve_rejects_unsupported_scheme        PASSED

Fail-closed matrix (3):
  test_production_requires_database_url          PASSED  ← 生产+无URL → RuntimeError
  test_production_rejects_sqlite                 PASSED  ← 生产+SQLite → RuntimeError
  test_non_production_allows_sqlite              PASSED  ← 内部模式不受影响

Pool / transactions / time (6):
  test_check_pg_ready_uses_pool_and_server_time  PASSED
  test_pg_transaction_commits                    PASSED
  test_pg_transaction_rolls_back_on_error        PASSED
  test_pg_transaction_serializable_write_conflict PASSED ← BEGIN IMMEDIATE 替代语义：双池连接并发写同 key，迟到提交者 40001
  test_pg_server_now_is_monotonic_and_not_client_clock PASSED
  test_pool_returns_connections_and_is_observable PASSED

API/Worker integration (2):
  test_api_bootstrap_completes_in_pg_mode        PASSED  ← bootstrap main() PG 路径完成
  test_worker_main_ready_check_in_pg_mode        PASSED  ← 行为断言：PG 模式不进入 SQLite 任务循环
```

### Full regression

```
$ uv run python -m pytest --rootdir . tests -q
601 passed, 1 warning in 70.20s     # 582 pre-existing (zero regression) + 15 new + 4 param variants
```

### Static gates

```
ruff check .        → All checks passed!
ruff format --check → 117 files already formatted
mypy (strict) app   → Success: no issues found in 53 source files
```

## Key design decisions

1. **Sync driver (psycopg3), not asyncpg**: the entire codebase is synchronous (FastAPI sync routes + sqlite3). psycopg3 with `%s` placeholders keeps the ~180 qmark call sites convertible mechanically (T04 inventory).
2. **SERIALIZABLE + caller-side retry** replaces `BEGIN IMMEDIATE` write fencing — proven by the two-connection conflict test (SQLSTATE 40001).
3. **Additive module**: `db.py`/SQLite runtime untouched — business modules migrate lane by lane (§8.2 of the code list), so the 582-test regression stays green.
4. **PG mode readiness ≠ PG business logic**: bootstrap/Worker prove pool + server round-trip now; the fair-queue loop and route-by-route fencing are T24/T25/T21 scope.

## Section 14 Ledger Record

```text
任务/工作包：T05 / DB-03
Owner / Reviewer：DB/后端（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t05-db-pg / 基线 8130321
上游规格段落：docs/客户版任务清单-V3.md §2 T05 行、§12.1 DB-03；docs/客户版代码开发清单-V3.md §8.1
改动文件：server/app/db_pg.py（新增）、server/app/bootstrap.py、server/app/generation_worker.py、server/tests/test_db_pg.py（新增）、server/pyproject.toml、server/uv.lock、docs/客户版任务清单-V3.md（T05/DB-03 状态）、T05-EVIDENCE.md
失败测试或回归锁定：test_db_pg.py 先行（模块不存在红灯）→ 实现后 15 绿；601 全量回归锁定零回归
实现结果：DSN 解析 + psycopg3 连接池 + 事务上下文（SERIALIZABLE 替代 BEGIN IMMEDIATE，双连接 40001 实证）+ PG 服务端时间 + 客户生产 fail-closed；API bootstrap 与 Worker 入口均完成 PG 就绪检查
验证命令与通过数：pytest tests/test_db_pg.py → 15 passed；pytest 全量 → 601 passed；ruff/mypy 全绿
证据层级：LOCALLY_VERIFIED（本地 PG fixture，真实 PG 16 容器）
安全与可观测性：fail-closed 测试矩阵；连接池 min/max 可配；测试凭据仅限本地 fixture
迁移与回滚：db_pg.py 纯增量，内部 SQLite 路径未动；回滚 = revert 单提交
外部授权记录：无
未测试项：CI 内嵌 PG service（Linux gate 当前以 skip 运行 PG 测试，接入 PG service 后转真实断言）；业务模块的 PG 数据访问（后续车道）
Lore 提交 SHA：见 PR squash 合并 SHA
```
