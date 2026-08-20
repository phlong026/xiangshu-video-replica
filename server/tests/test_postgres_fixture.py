"""
T03 - PostgreSQL Fixture Tests
Verifies: PG container is running, accessible, and supports concurrent connections
"""
import pytest
import asyncpg
import os


@pytest.fixture(scope="session")
def pg_dsn():
    """Get PostgreSQL DSN for tests"""
    return os.environ.get(
        "TEST_POSTGRESQL_URL",
        "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


def test_database_creation(pg_dsn):
    """Test that PG database exists and is accessible"""
    async def run_test():
        conn = await asyncpg.connect(pg_dsn)
        try:
            result = await conn.fetchval("SELECT current_database();")
            assert result == "customer_v3_test", f"Expected customer_v3_test but got {result}"
        finally:
            await conn.close()
    
    import asyncio
    asyncio.run(run_test())


def test_user_identity(pg_dsn):
    """Test PostgreSQL user identity"""
    async def run_test():
        conn = await asyncpg.connect(pg_dsn)
        try:
            result = await conn.fetchval("SELECT current_user;")
            assert result == "testuser", f"Expected testuser but got {result}"
        finally:
            await conn.close()
    
    import asyncio
    asyncio.run(run_test())


def test_postgres_version(pg_dsn):
    """Test PostgreSQL version is 16.x"""
    async def run_test():
        conn = await asyncpg.connect(pg_dsn)
        try:
            result = await conn.fetchval("SHOW server_version;")
            assert result.startswith("16."), f"Expected PostgreSQL 16.x but got {result}"
        finally:
            await conn.close()
    
    import asyncio
    asyncio.run(run_test())


def test_independent_connections(pg_dsn):
    """Test multiple independent connections can coexist"""
    async def run_test():
        conn1 = await asyncpg.connect(pg_dsn)
        conn2 = await asyncpg.connect(pg_dsn)
        
        try:
            val1 = await conn1.fetchval("SELECT 1;")
            val2 = await conn2.fetchval("SELECT 2;")
            
            assert val1 == 1, "Connection 1 failed"
            assert val2 == 2, "Connection 2 failed"
        finally:
            await conn1.close()
            await conn2.close()
    
    import asyncio
    asyncio.run(run_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
