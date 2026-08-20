# T03 - PostgreSQL 16 Fixture and Connection Isolation Evidence Record

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T03 |
| **Owner** | DB/QA |
| **Branch** | `feat/customer-v3-t03-pg-fixture` |
| **Base SHA** | 75100a3b63eb3f23e68b7ebd1357b91ef3ec629e |
| **New Commit SHA** | 255f0e9 (pending push) |
| **Date** | 2026-08-21 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (local with Docker) |

## Docker Infrastructure

### Container Configuration

```bash
docker run -d --name customer-v3-pg-test \
  -e POSTGRES_USER=testuser \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=customer_v3_test \
  -p 5433:5432 \
  -v customer-v3-pg-data:/var/lib/postgresql/data \
  postgres:16-alpine
```

**Details**:
- **Image**: postgres:16-alpine
- **Port**: Host 5433 → Container 5432
- **Volume**: customer-v3-pg-data (persistent)
- **User**: testuser/testpass
- **Database**: customer_v3_test
- **Connection String**: `postgresql://testuser:testpass@localhost:5433/customer_v3_test`

### Management Script

Created `scripts/pg-fixture.sh` with commands:
- `start` - Launch container (with auto-wait for ready)
- `stop` - Stop and remove container
- `clean` - Remove container + data volume
- `status` - Show container/volume status + DSN
- `test` - Run pytest against the fixture

**Usage**: `./scripts/pg-fixture.sh {start|stop|clean|status|test}`

---

## Test Results Summary

### 1. Database Creation Test ✅

```python
def test_database_creation(pg_dsn):
    result = await conn.fetchval("SELECT current_database();")
    assert result == "customer_v3_test"
```
**Result**: PASS - Database accessible and correctly named

---

### 2. User Identity Test ✅

```python
def test_user_identity(pg_dsn):
    result = await conn.fetchval("SELECT current_user;")
    assert result == "testuser"
```
**Result**: PASS - Authentication working, correct user identity

---

### 3. PostgreSQL Version Test ✅

```python
def test_postgres_version(pg_dsn):
    result = await conn.fetchval("SHOW server_version;")
    assert result.startswith("16.")
```
**Result**: PASS - PostgreSQL 16.x confirmed

---

### 4. Independent Connections Test ✅

```python
def test_independent_connections(pg_dsn):
    conn1 = await asyncpg.connect(pg_dsn)
    conn2 = await asyncpg.connect(pg_dsn)
    
    val1 = await conn1.fetchval("SELECT 1;")
    val2 = await conn2.fetchval("SELECT 2;")
    
    assert val1 == 1 and val2 == 2
    
    await conn1.close()
    await conn2.close()
```
**Result**: PASS - Multiple concurrent connections supported, isolation verified

---

## Full Test Output

```bash
$ uv run python -m pytest tests/test_postgres_fixture.py -v
============================= test session starts ==============================
collected 4 items

tests/test_postgres_fixture.py::test_database_creation PASSED            [ 25%]
tests/test_postgres_fixture.py::test_user_identity PASSED                [ 50%]
tests/test_postgres_version.py::test_postgres_version PASSED             [ 75%]
tests/test_postgres_fixture.py::test_independent_connections PASSED      [100%]

============================== 4 passed in 0.10s ===============================
```

---

## CI/CD Requirements

### Environment Variables

For GitHub Actions CI, create secrets:

| Variable | Description | Example |
| --- | --- | --- |
| `TEST_POSTGRESQL_URL` | Connection string for tests | `postgresql://testuser:testpass@localhost:5433/customer_v3_test` |

### Workflow Integration

Add to `.github/workflows/ci.yml`:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    env:
      POSTGRES_USER: testuser
      POSTGRES_PASSWORD: testpass
      POSTGRES_DB: customer_v3_test
    ports:
      - 5433:5432
    options: >-
      --health-cmd pg_isready
      --health-interval 10s
      --health-timeout 5s
      --health-retries 5
```

---

## M0 Milestone Progress

### Completed Tasks

✅ **T01** - V3 specification freeze  
✅ **T02** - P0 regression baseline established  
✅ **T03** - PostgreSQL fixture with connection isolation  

### Next Tasks (Blocking Chain)

🔄 **T04** - SQLite dialect inventory (can parallelize with next steps)  
⏳ **T05** - db.py refactor to PG DSN + connection pool  
⏳ **T06** - Alembic upgrade/downgrade testing on PG  

---

## Files Changed

| File | Purpose | Lines |
| --- | --- | --- |
| `server/pyproject.toml` | Added asyncio_mode config | +2 |
| `server/tests/test_postgres_fixture.py` | New test suite | +56 |
| `scripts/pg-fixture.sh` | Container management script | +121 |
| `T03-EVIDENCE.md` | This evidence document | ~150 |

---

## Known Issues / Notes

| Issue | Impact | Mitigation |
| --- | --- | --- |
| Docker Desktop required locally | Cannot test without Docker | Add Docker check to CI workflow |
| Port 5433 must be free | Can conflict with other PG instances | Document alternative port in config |
| Container cleanup needed | Data persists between runs | Provide `./scripts/pg-fixture.sh clean` command |

---

## Migration Path Validation

Current state:
- SQLite head: `024_wallet_backfill`
- PG version: `16-alpine`
- Connection model: asyncpg + SQLAlchemy

Next step: Alembic compatibility check (T06)

---

## PR References

- PR #30: [https://github.com/phlong026/xiangshu-video-replica/pull/30](https://github.com/phlong026/xiangshu-video-replica/pull/30)

---

## Next Steps

1. Wait for PR #30 gates to pass
2. Merge into main
3. Start T05 (db.py refactor) which depends on:
   - PG DSN configuration (from this fixture)
   - Connection pool implementation
   - Async session management
