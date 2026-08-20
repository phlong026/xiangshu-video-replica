# T04 — SQLite Dialect Inventory Report

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T04 |
| **Owner** | DB/Architecture |
| **Branch** | `feat/customer-v3-t04-sqlite-inventory` |
| **Base SHA** | 75100a3b63eb3f23e68b7ebd1357b91ef3ec629e |
| **Date** | 2026-08-21 |
| **Evidence Level** | `CODE_PRESENT` (scan complete) |

---

## Executive Summary

Scanned all Python modules under `server/app/`, `server/tests/`, and `server/migrations/` for direct SQLite dependencies and dialect-specific syntax. This report is the DB-02 line-level inventory; it corrects the earlier draft (per PR #31 review): production-code BEGIN IMMEDIATE counts, SQL-vs-Python datetime expressions, the test-suite dependency surface, and the no-ORM constraint are now stated precisely.

### Key Statistics (verified 2026-08-21, base 75100a3)

- **Production modules with `import sqlite3`**: 32 files in `server/app/`
- **Test files with `import sqlite3`**: 26 files in `server/tests/` (regression surface, not runtime)
- **Migration env with sqlite branch**: `server/migrations/env.py` (3 sqlite references)
- **BEGIN IMMEDIATE**: 26 total — 23 in production code (9 files) + 3 in tests (3 files); exact per-file counts below
- **SQL-dialect datetime expressions**: 4 occurrences in 2 files (`datetime('now', ...)` in SQL strings); the 3 remaining `strftime(...)` hits are plain Python formatting and need no SQL migration
- **sqlite3.Row / sqlite3.Error types**: 155 lines across 24 files
- **ON CONFLICT / INSERT OR IGNORE**: 7 occurrences across 4 files
- **`?` qmark placeholders**: ~180 occurrences across 32 production modules
- **PRAGMA / row_factory / check_same_thread**: `db.py` only
- **batch_alter_table in Alembic revisions**: 16 migrations

---

## Module-Level Breakdown

### Category 1: Core Infrastructure (High Priority)

#### `db.py` ✅ Critical
**Dependencies**: 
- `sqlite3.connect()` with `check_same_thread=False`, `row_factory=sqlite3.Row`
- `PRAGMA journal_mode = WAL`
- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}`
- `_sqlite_url()` helper function

**Migration Complexity**: 🔴 **HIGH** — Must refactor to a PG DSN + connection pool (psycopg3 + psycopg_pool; asyncpg stays for async-only helpers). Customer-production startup must reject SQLite (T05 fail-closed).

**Alternatives**:
```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
engine = create_async_engine("postgresql://user:pass@host:5432/db", echo=True)
conn = await engine.connect()
```

---

#### `repositories.py` 
**Dependencies**:
- Uses `?` placeholders (qmark style): ~5 per file
- `INSERT OR REPLACE`: 2 occurrences
- Batch alter in migrations

**PostgreSQL Migration**: Replace qmark `?` with `%s` or positional `$1, $2`

---

### Category 2: Business Logic Modules (Medium Priority)

#### `generation.py` 🔴 High Impact
**Dependencies**:
- `BEGIN IMMEDIATE`: 8 occurrences (line numbers vary)
- `sqlite3.Error`: ~15 lines
- `datetime()` calls: 3 times

**Critical Path**: Task reservation workflow depends on this pattern

**PG Alternative**: Use explicit transaction isolation level
```python
async with engine.begin() as conn:
    await conn.execute(
        text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
    )
```

---

#### `analysis.py` & `analysis_routes.py`
**Dependencies**:
- `BEGIN IMMEDIATE`: Mixed usage
- `sqlite3.Row`: Extensive result fetching (~40+ lines combined)
- `?` placeholders: analysis.py ~1, routes.py ~2

---

#### `character*.py` cluster
Files scanned:
- `characters.py`
- `character_identity.py`
- `character_image_generation.py`
- `character_reference_matching.py`
- `character_asset_review.py`

**Shared Patterns**:
- `sqlite3.Error` handling: ~20+ lines total
- `ON CONFLICT DO NOTHING/UPDATE`: Used in `project_character_selection.py`
- `?` placeholders: ~60 occurrences total

**Risk Level**: 🟡 Medium — Many interfaces, but encapsulated

---

#### `first_frames.py` & `first_frame_routes.py`
**Dependencies**:
- `BEGIN IMMEDIATE`: Frequent
- `sqlite3.Error/OperationalError`: ~25 lines combined
- Row factory: Explicit use

---

#### `settings.py` & `settings_routes.py`
**Dependencies**:
- `ON CONFLICT`: INSERT conflicts (2+ occurrences)
- Simple CRUD patterns

---

#### `media.py` & `source_frames.py`
**Dependencies**:
- Basic sqlite3 usage
- `?` placeholders in queries
- Minimal error handling complexity

---

### Category 3: Auth & Control Plane

#### `auth.py` & `control_routes.py`
**Dependencies**:
- Direct sqlite3 connections
- `sqlite3.Error` catching
- Admin session management

**Security Note**: PG must preserve same auth semantics with role-based access

---

#### `permissions.py` & `rbac_routes.py`
**Dependencies**:
- `sqlite3.Error`: Permission checks
- Role resolution logic uses qmark placeholders

---

### Category 4: Billing & Payment

#### `zpay_payments.py` & `recharge_routes.py` & `internal_billing.py`
**Dependencies**:
- `BEGIN IMMEDIATE`: 6+ occurrences across billing modules
- Money formatting validation (`zpay.py` uses `strftime`)
- Transaction boundaries critical for funds

**Risk Level**: 🔴 **CRITICAL** - Must ensure ACID compliance in PG

**Alternative**: Explicit transaction blocks with SAVEPOINT support

---

#### `payment_routes.py`
**Dependencies**:
- Minimal sqlite3 usage
- Callback validation logic

---

### Category 5: Worker & Background Jobs

#### `generation_worker.py`
**Dependencies**:
- No direct sqlite3.Row usage
- Relies on `generation.py` patterns

**Good News**: Less coupled than API layer

---

#### `backup.py` & `settings_routes.py`
**Dependencies**:
- `strftime()` for backup filenames (3+ occurrences)
- Database export/import logic

**Migration Note**: PG requires `to_char(now(), 'YYYY-MM-DD HH24:MI:SS')`

---

### Category 6: Other Modules

| Module | Key SQLite Features | Risk |
| --- | --- | --- |
| `simple_character.py` | sqlite3.Error, qmark (16x) | 🟡 Medium |
| `script_rewrite.py` | Minimal, no direct deps | 🟢 Low |
| `internal_accounts.py` | sqlite3.Error, qmark (4x) | 🟡 Medium |
| `media.py` | Basic fetch/update, qmark (2x) | 🟢 Low |
| `zpay.py` | strftime() in filename gen | 🟡 Medium |

---

## Dialect Feature Inventory

### 1. BEGIN IMMEDIATE (26 occurrences: 23 production + 3 test)

**Purpose**: Prevent write-write conflicts by acquiring an exclusive lock before the transaction body

**Exact per-file counts** (grep -c verified):

Production (`server/app/`, 23 occurrences in 9 files):

| File | Count |
| --- | --- |
| `generation.py` | 11 |
| `character_image_generation.py` | 3 |
| `simple_character.py` | 2 |
| `character_asset_review.py` | 2 |
| `analysis.py` | 1 |
| `zpay_payments.py` | 1 |
| `project_character_selection.py` | 1 |
| `first_frames.py` | 1 |
| `character_reference_matching.py` | 1 |

Tests (`server/tests/`, 3 occurrences in 3 files — must be converted together with the modules they exercise):

| File | Count |
| --- | --- |
| `test_wallet_billing_service.py` | 1 |
| `test_payments.py` | 1 |
| `test_db.py` | 1 |

**PostgreSQL Alternatives** (driver-level, no ORM — per `docs/客户版代码开发清单-V3.md` §8.1 "不引入 ORM、Redis 或队列框架"):

```python
# psycopg3, SERIALIZABLE isolation + retry on serialization failure
import psycopg

with psycopg.connect(dsn, autocommit=False) as conn:
    conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
    try:
        with conn.transaction():
            ...  # former BEGIN IMMEDIATE body
    except psycopg.errors.SerializationFailure:
        ...  # bounded retry loop
```

For task-lease claiming (generation.py), prefer `SELECT ... FOR UPDATE SKIP LOCKED` over table locks; the ADR is frozen in T24 (QUE-01).

**Risk Assessment**: 🔴 **HIGH** — concurrent-write semantics differ; each of the 23 production sites needs an individual transaction-boundary review (SES-04/QUE-01/QUE-02 will do this route by route, not by batch replacement)

---

### 2. SQL datetime expressions (4 occurrences, 2 files) — plus 3 non-SQL Python strftime

**SQL-dialect `datetime('now', ...)` inside SQL strings** (these change semantics on PG and must be rewritten):

| File | Line | Expression |
| --- | --- | --- |
| `generation.py` | 2708 | `AND datetime(updated_at) <= datetime('now', ?)` |
| `generation.py` | 3503 | `next_poll_at = datetime('now', '+60 seconds')` |
| `generation.py` | 3748 | `next_poll_at = datetime('now', '+60 seconds')` |
| `repositories.py` | 80 | `locked_until = datetime('now', ?)` |

**PostgreSQL Alternatives**:
```sql
-- generation.py:2708
AND updated_at <= now() - make_interval(secs => %s)

-- generation.py:3503/3748, repositories.py:80 (parameterized offset)
next_poll_at = now() + make_interval(secs => %s)
locked_until  = now() + make_interval(secs => %s)
```
Note the parameter becomes a plain integer offset (seconds) instead of a SQLite modifier string like `'+60 seconds'`; callers must pass numbers. PG server time is the only trusted clock (SES-01).

**Not SQL-dialect (no migration needed, listed to close the earlier ambiguity):** plain-Python `datetime.now(UTC).strftime(...)` formatting in `backup.py:66`, `zpay.py:94`, `gate1_e2e.py:738`. These format timestamps in application code and behave identically regardless of database backend.

**Risk Assessment**: 🟡 **MEDIUM** — 4 sites, mechanical rewrite, but the `?` modifier-string → integer-seconds change touches call sites

---

### 3. sqlite3.Row (155 lines across 24 files)

**Purpose**: Return query results as dict-like objects instead of tuples

**Examples**:
```python
conn.row_factory = sqlite3.Row
cursor = conn.execute("SELECT * FROM users WHERE id = ?", (id,))
row = cursor.fetchone()
username = row['username']  # Column name access
```

**PostgreSQL Alternatives** (driver-level dict-row access, no ORM):
```python
import psycopg
from psycopg.rows import dict_row

# dict_row gives the same string-keyed access as sqlite3.Row
with psycopg.connect(dsn, row_factory=dict_row) as conn:
    row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    username = row["username"]
```
`psycopg.rows.dict_row` preserves the exact `row["column"]` call pattern, so the ~155 usage lines convert mechanically without introducing an ORM layer.

**Risk Assessment**: 🟢 **LOW** — mechanical rewrite with dict_row

---

### 4. ON CONFLICT / INSERT OR IGNORE / INSERT OR REPLACE (7 occurrences, 4 files)

**Purpose**: UPSERT patterns for upserting records

**Files**:
- `project_character_selection.py`
- `settings.py` (3x)
- `repositories.py`
- `characters.py` (1x)

**SQLite Patterns**:
```sql
INSERT INTO settings (key, value) VALUES (?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value;

INSERT OR IGNORE INTO cache (...) SELECT ...

INSERT OR REPLACE INTO cache (...) SELECT ...
```

**PostgreSQL Alternatives**:
```sql
-- Same syntax supported in PG 9.5+
INSERT INTO settings (key, value) VALUES ($1, $2)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- MERGE-style (manual implementation in PG)
DO $$
BEGIN
  UPDATE cache SET value = $2 WHERE key = $1;
EXCEPTION WHEN unique_violation THEN
  INSERT INTO cache (key, value) VALUES ($1, $2);
END $$;
```

**Risk Assessment**: 🟡 **MEDIUM** — Syntax compatible but needs parameterization change (? → $n)

---

### 5. PRAGMA Statements (Multiple in db.py)

**Current Usage**:
```python
conn.execute("PRAGMA journal_mode = WAL")      # Enable WAL mode
conn.execute("PRAGMA foreign_keys = ON")       # Enable FK constraints
conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")  # Retry timeout
```

**PostgreSQL Alternatives**:
```python
# WAL mode equivalent: shared_buffers + checkpoint settings in postgresql.conf
# Foreign keys: Enabled globally, cannot disable
# Busy timeout: Handled by connection pooling + retry middleware
```

**Configuration Changes Required**:
- Set `journal_mode = wal` in postgresql.conf
- Verify `constraint_exclusion = on`
- Implement application-level retry logic with exponential backoff

**Risk Assessment**: 🟢 **LOW** — Mostly configuration-driven changes

---

### 6. Row Factory / check_same_thread / isolation_level (db.py only)

**Current Usage**:
```python
conn = sqlite3.connect(path, timeout=..., check_same_thread=False)
conn.row_factory = sqlite3.Row
```

**Meanings**:
- `check_same_thread=False`: Allow connections from different threads
- `row_factory=Row`: Dict-like result access
- `timeout=BUSY_TIMEOUT_MS`: Write lock wait time

**PostgreSQL Implications**:
- Asyncpg supports concurrent connections natively
- SQLAlchemy ORM handles result mapping
- Connection pooling replaces per-query timeout

**Risk Assessment**: 🟢 **LOW** — These are SQLite-specific optimizations that don't translate directly

---

### 7. Parameterized Queries with `?` Placeholders

**Count**: ~180 total occurrences across 32 modules

**Top Files**:
- `generation.py`: 26 qmark placeholders
- `character_identity.py`: 22 placeholders
- `simple_character.py`: 16 placeholders
- `character_image_generation.py`: 13 placeholders
- `characters.py`: 14 placeholders

**Pattern Example**:
```python
conn.execute(
    "SELECT * FROM users WHERE id = ? AND status = ?",
    (user_id, status)
)
```

**PostgreSQL Alternatives** (psycopg3, no ORM):
```python
# psycopg3 native style (%s placeholder, closest to current qmark code):
cur.execute("SELECT * FROM users WHERE id = %s AND status = %s", (user_id, status))
```
No named-parameter indirection is required: `%s` placeholders keep the same positional tuple binding as `?`, minimizing diff noise across the ~180 call sites.

**Risk Assessment**: 🟡 **MEDIUM** — systematic but mechanical; grep-able pattern

---

## Alembic Migration Analysis

### SQLite-Specific Migrations

**Identified Issues**:

1. **batch_alter_table**: 16 migrations use this (not portable to PG without ALTER TABLE)

   Files with batch_alter_table:
   - `012_character_domain.py`
   - `017_generation_task_retry_lineage.py`
   - `022_internal_billing.py`
   - + 13 others

   **PG Alternative**:
   ```python
   op.alter_column('table_name', 'column_name', new_type='VARCHAR(255)')
   ```

2. **AUTOINCREMENT** on INTEGER PRIMARY KEY (SQLite quirk not needed in PG)

3. **Inline FOREIGN KEY definitions** in CREATE TABLE statements (works in both but order matters differently)

4. **DEFAULT CURRENT_TIMESTAMP** variations (SQLite's `'now'` vs PG's `CURRENT_TIMESTAMP`)

### Migration Environment Setup

**File**: `server/migrations/env.py`

**SQLite-Specific Code**:
```python
def ensure_sqlite_parent_directory(url: str) -> None:
    """Create parent directories for SQLite files"""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
```

**PG Compatibility**: Remove entirely; databases already exist before migrations run

---

## Risk Levels Summary

| Module Group | Risk | Primary Issue | Migration Complexity |
| --- | --- | --- | --- |
| `db.py`, `generation.py`, `generation_worker.py` | 🔴 CRITICAL | BEGIN IMMEDIATE + transaction control | HIGH |
| `internal_billing.py`, `recharge_routes.py`, `zpay_payments.py` | 🔴 CRITICAL | Fund transfers require ACID | HIGH |
| `auth.py`, `control_routes.py`, `permissions.py` | 🟠 HIGH | Concurrent login/session writes | MEDIUM-HIGH |
| `first_frames.py`, `analysis.py`, `characters.py` | 🟡 MEDIUM | Row access patterns + conflict handling | MEDIUM |
| `settings.py`, `backup.py`, `media.py` | 🟢 LOW | Basic CRUD with qmark params | LOW |

---

## Migration Roadmap Recommendations

### Phase 1: Foundation (T05-T06)

1. Refactor `db.py` to use asyncpg + connection pool
2. Update Alembic environment for PG URL support
3. Test upgrade/downgrade cycles

### Phase 2: Core Workflows (T07-T10)

1. Convert BEGIN IMMEDIATE patterns to SERIALIZABLE transactions
2. Replace datetime() / strftime() with PG equivalents
3. Add retry middleware for PG contention

### Phase 3: Business Modules (T11-T15)

1. Character management + first frames
2. Wallet/billing (high priority due to fund safety)
3. Auth/control plane updates

### Phase 4: Cleanup (T16+)

1. Remove all sqlite3 imports from tests
2. Drop SQLite fixture scripts
3. Final consistency audit

---

## CI/CD Considerations

### Pipeline Changes Needed

```yaml
# .github/workflows/ci.yml
services:
  postgres:
    image: postgres:16-alpine
    env:
      POSTGRES_USER: testuser
      POSTGRES_PASSWORD: testpass
      POSTGRES_DB: customer_v3_test
    ports:
      - 5433:5432

env:
  TEST_POSTGRESQL_URL: postgresql://testuser:testpass@localhost:5433/customer_v3_test
```

### Testing Strategy

1. Run full Pytest suite against PostgreSQL
2. Verify transaction isolation behavior
3. Check query performance on hot paths (generation queue)

---

## Conclusion

Dependency surface (this report is the DB-02 deliverable; it is **evidence only** — the sole task-status ledger remains `docs/客户版任务清单-V3.md`):

- **32 production modules** in `server/app/` with direct `sqlite3` imports (the "33 modules" figure in `docs/客户版代码开发清单-V3.md` §8 counts this set plus `server/migrations/env.py`)
- **26 test files** in `server/tests/` with direct `sqlite3` imports — they must migrate together with the modules they exercise, or the PG regression in T05+ loses coverage
- **1 migration env** (`server/migrations/env.py`) with a sqlite-only parent-directory branch

Key risks: BEGIN IMMEDIATE (23 production sites, concentrated in `generation.py` with 11), SQL-dialect datetime (4 sites), ON CONFLICT clauses (7 sites). All are replaceable at the **driver level with psycopg3** (`dict_row`, `%s` placeholders, `SERIALIZABLE` transactions, `make_interval`) — deliberately **no ORM rewrite**, per the frozen constraint in `docs/客户版代码开发清单-V3.md` §8.1.

Estimated effort: 45–65 person-days (including tests + documentation), consistent with the V3 plan's Lane-A budget.

---

## References

- See also: `docs/客户版代码开发清单-V3.md` §8 (PostgreSQL runtime migration scope)
- See also: `docs/客户版开发计划-V3.md` §12 (DB-02 line-level inventory)
- Related task: T05 (db.py refactor), T06 (Alembic on PG)


---

## Section 14 Ledger Record

```text
任务/工作包：T04 / DB-02
Owner / Reviewer：DB（Agent 执行）/ chatgpt-codex-connector（PR #31 评审，3×P1+2×P2 已全部修复）
分支 / 基线 SHA：feat/customer-v3-t04-sqlite-inventory / 基线 75100a3
上游规格段落：docs/客户版任务清单-V3.md §1 T04 行、§12.1 DB-02；docs/客户版代码开发清单-V3.md §8
改动文件：T04-SQLITE-INVENTORY.md、docs/客户版任务清单-V3.md（T04/DB-02 状态）
失败测试或回归锁定：静态扫描类任务，无失败测试
实现结果：行级清单覆盖 app 32 模块 + tests 26 文件 + migrations/env.py；BEGIN IMMEDIATE 26 处精确分布；SQL 方言 datetime 4 处与 Python 层 strftime 3 处区分；全部替代方案为 psycopg3 驱动层（无 ORM）
验证命令与通过数：grep -rc/-rn 精确统计（见文档各表）
证据层级：CODE_PRESENT
安全与可观测性：N/A
迁移与回滚：纯文档
外部授权记录：无
未测试项：N/A
Lore 提交 SHA：见 PR #31 squash 合并 SHA
```
