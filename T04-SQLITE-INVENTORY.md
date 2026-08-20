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

Scanned all Python modules in `server/app/` for direct SQLite dependencies and dialect-specific syntax. Identified **32 modules** with direct `sqlite3` imports, plus Alembic migrations using SQLite-specific patterns. Documented each dialect feature used and provided PostgreSQL migration alternatives with risk levels.

### Key Statistics

- **Modules with `import sqlite3`**: 32 files
- **BEGIN IMMEDIATE usages**: 23 occurrences across 9 files
- **datetime() / strftime() functions**: 7 occurrences across 5 files  
- **sqlite3.Row/Error types**: 155 total lines, 24 files
- **ON CONFLICT / INSERT OR IGNORE**: 7 occurrences across 4 files
- **PRAGMA statements**: Multiple in db.py
- **row_factory / check_same_thread**: In db.py only
- **batch_alter_table (Alembic SQLite-specific)**: 16 migrations

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

**Migration Complexity**: 🔴 **HIGH** - Must refactor to asyncpg + SQLAlchemy connection pool

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

### 1. BEGIN IMMEDIATE (23 occurrences, 9 files)

**Purpose**: Prevent write-write conflicts by acquiring exclusive lock early

**Files**:
- `first_frames.py` (3x)
- `generation.py` (8x)
- `analysis.py` (2x)
- `project_character_selection.py` (1x)
- `character_image_generation.py` (2x)
- `zpay_payments.py` (2x)
- `character_asset_review.py` (1x)
- `simple_character.py` (1x)
- `character_reference_matching.py` (3x)

**PostgreSQL Alternatives**:
```python
# Option 1: Explicit isolation level
async with begin_transaction(isolation_level="SERIALIZABLE"):
    # perform writes
    
# Option 2: Pessimistic locking
await conn.execute(text("LOCK TABLE users IN EXCLUSIVE MODE"))
    
# Option 3: Optimistic concurrency via version column
```

**Risk Assessment**: 🔴 **HIGH** — Concurrent modifications could fail silently if not properly handled

---

### 2. datetime() / strftime() Functions (7 occurrences, 5 files)

**Purpose**: Generate timestamps for audit logs, backup filenames, etc.

**Files**:
- `backup.py` (backup filename format)
- `zpay.py` (transaction time format)
- `generation.py` (task timestamp)
- `gate1_e2e.py` (test fixtures)
- `repositories.py` (audit logging)

**Usage Examples**:
```python
datetime('now', 'localtime')  # Returns formatted string
strftime('%Y-%m-%d %H:%M:%S', 'now')
```

**PostgreSQL Alternatives**:
```sql
-- Standard ISO format
NOW()::TEXT  -- 2026-08-21 00:32:45

-- Custom formatting
TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')

-- Unix timestamp
EXTRACT(EPOCH FROM NOW())

-- Date-only
CURRENT_DATE
```

**Risk Assessment**: 🟡 **MEDIUM** — Easy replacement, no semantic change

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

**PostgreSQL Alternatives**:
```python
from sqlalchemy.orm import Session
session = Session(engine)
user = session.query(User).filter(User.id == id).first()
username = user.username  # Attribute access

# Or raw SQL
result = session.execute(text("SELECT * FROM users WHERE id = :id"), {"id": id})
username = result.fetchone()[0]  # Positional access
# Or use RowMapping
username = result.fetchone()._mapping['username']
```

**Risk Assessment**: 🟢 **LOW** — SQLAlchemy ORM makes this transparent

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

**PostgreSQL Alternatives**:
```python
# psycopg2 / asyncpg style (%s placeholder):
cur.execute("SELECT * FROM users WHERE id = %s AND status = %s", (user_id, status))

# SQLAlchemy style (:name placeholder):
stmt = text("SELECT * FROM users WHERE id = :user_id AND status = :status")
result = session.execute(stmt, {"user_id": user_id, "status": status})

# Positional parameters ($n):
cur.execute("SELECT * FROM users WHERE id = $1 AND status = $2", (user_id, status))
```

**Risk Assessment**: 🟡 **MEDIUM** — All need systematic replacement, but straightforward

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

Total count: **32 modules** identified with direct SQLite dependencies, matching the estimate in `docs/客户版代码开发清单-V3.md`. Key risks center on BEGIN IMMEDIATE patterns (23 usages), datetime functions (7 usages), and ON CONFLICT clauses (7 usages). 

All issues are **replaceable** but require systematic refactoring:
- Transaction semantics preserved via SERIALIZABLE isolation
- Timestamps via TO_CHAR/NOW()
- UPSERT via standard ON CONFLICT syntax (already supported)
- Row access via SQLAlchemy ORM or RowMapping

Estimated effort: 45–65 person-days (including tests + documentation)

---

## References

- See also: `docs/客户版代码开发清单-V3.md` §8 (PostgreSQL runtime migration scope)
- See also: `docs/客户版开发计划-V3.md` §12 (DB-02 line-level inventory)
- Related task: T05 (db.py refactor), T06 (Alembic on PG)

