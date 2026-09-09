from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from fastapi import HTTPException

from app.auth import CurrentUser
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.generation_worker import _run_sqlite_viral_refresh_step, run_worker_once
from app.storage import FakeStorageAdapter
from app.viral_refresh import (
    acquire_viral_refresh_task,
    complete_viral_refresh_task,
    enqueue_viral_refresh_task,
    fail_viral_refresh_task,
)


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "viral-refresh.db"
    initialize_database(path).close()
    connection = BusinessConnection.sqlite(connect_database(path))
    try:
        yield connection
    finally:
        connection.close()


def test_refresh_scope_is_deduplicated_and_requeued(conn: BusinessConnection) -> None:
    first = enqueue_viral_refresh_task(conn, platform="douyin", sort="hot")
    replay = enqueue_viral_refresh_task(conn, platform="douyin", sort="hot")

    assert replay["id"] == first["id"]
    lease = acquire_viral_refresh_task(conn, worker_id="worker-1")
    assert lease is not None
    assert lease.attempt == 1
    assert acquire_viral_refresh_task(conn, worker_id="worker-2") is None

    complete_viral_refresh_task(conn, lease=lease)
    requeued = enqueue_viral_refresh_task(conn, platform="douyin", sort="hot")
    assert requeued["id"] == first["id"]
    assert requeued["status"] == "PENDING"


def test_expired_refresh_lease_is_fenced_by_attempt(conn: BusinessConnection) -> None:
    enqueue_viral_refresh_task(conn, platform="wechat_channels", sort="latest")
    stale = acquire_viral_refresh_task(conn, worker_id="reused-worker")
    assert stale is not None
    expired = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE viral_refresh_tasks SET locked_until = %s WHERE id = %s",
        (expired, stale.id),
    )
    conn.commit()

    current = acquire_viral_refresh_task(conn, worker_id="reused-worker")
    assert current is not None
    assert current.id == stale.id
    assert current.attempt == stale.attempt + 1

    with pytest.raises(HTTPException) as raised:
        complete_viral_refresh_task(conn, lease=stale)
    assert raised.value.detail["code"] == "VIRAL_REFRESH_LEASE_LOST"

    fail_viral_refresh_task(conn, lease=current, cause=RuntimeError("secret upstream body"))
    row = conn.execute(
        "SELECT status, retryable, error_message_redacted FROM viral_refresh_tasks WHERE id = %s",
        (current.id,),
    ).fetchone()
    assert tuple(row) == ("FAILED", 1, "爆款视频刷新失败，请稍后重试。")


def test_sqlite_worker_executes_refresh_task(
    conn: BusinessConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.generation_worker as worker

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "get_viral_source_client", lambda _conn: object())
    monkeypatch.setattr(
        worker,
        "_collect_videos",
        lambda _conn, _client, *, platform, sort, **_kwargs: calls.append((platform, sort)),
    )
    task = enqueue_viral_refresh_task(conn, platform="douyin", sort="latest")

    assert (
        run_worker_once(
            conn,
            worker_id="refresh-worker",
            storage=FakeStorageAdapter(provider="fake", bucket="private"),
            max_tasks=1,
        )
        == 1
    )
    assert calls == [("douyin", "latest")]
    row = conn.execute(
        "SELECT status FROM viral_refresh_tasks WHERE id = %s", (task["id"],)
    ).fetchone()
    assert row["status"] == "SUCCEEDED"


def test_sqlite_refresh_failure_does_not_crash_after_lease_loss(
    conn: BusinessConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.generation_worker as worker

    enqueue_viral_refresh_task(conn, platform="douyin", sort="latest")
    monkeypatch.setattr(worker, "get_viral_source_client", lambda _conn: object())

    def fail_after_expiring_lease(*_args, **_kwargs) -> None:
        expired = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE viral_refresh_tasks SET locked_until = %s", (expired,))
        conn.commit()
        raise RuntimeError("upstream failed after lease expiry")

    monkeypatch.setattr(worker, "_collect_videos", fail_after_expiring_lease)

    assert _run_sqlite_viral_refresh_step(conn, worker_id="refresh-worker") is True
    row = conn.execute("SELECT status FROM viral_refresh_tasks").fetchone()
    assert row["status"] == "RUNNING"


@pytest.mark.pg
def test_postgres_refresh_scope_has_one_task_and_one_lease() -> None:
    base_dsn = os.environ.get(
        "TEST_POSTGRESQL_URL",
        "postgresql://testuser:testpass@localhost:5433/customer_v3_test",
    )
    admin_dsn = base_dsn.rsplit("/", 1)[0] + "/postgres"
    database_name = "viral_refresh_concurrency_test"
    dsn = base_dsn.rsplit("/", 1)[0] + f"/{database_name}"
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{database_name}"')
    except psycopg.Error:
        pytest.skip("PostgreSQL fixture is unavailable")

    from app.db import alembic_config

    try:
        config = alembic_config(":memory:")
        config.set_main_option(
            "sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://")
        )
        command.upgrade(config, "head")

        from app.viral_routes import list_viral_videos

        with psycopg.connect(dsn) as raw:
            response = list_viral_videos(
                BusinessConnection.postgres(raw),
                CurrentUser(
                    id="user-1",
                    username="user-1",
                    display_name="User One",
                    role="customer",
                ),
                None,
                platform="douyin",
                sort="hot",
            )
        assert response.items == []
        assert response.stale is True
        assert response.refreshing is True

        def enqueue_once() -> str:
            with psycopg.connect(dsn) as raw:
                row = enqueue_viral_refresh_task(
                    BusinessConnection.postgres(raw), platform="douyin", sort="hot"
                )
                return str(row["id"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _index: enqueue_once(), range(20)))
        assert len(set(ids)) == 1

        def acquire_once(index: int):
            with psycopg.connect(dsn) as raw:
                return acquire_viral_refresh_task(
                    BusinessConnection.postgres(raw), worker_id=f"worker-{index}"
                )

        with ThreadPoolExecutor(max_workers=4) as pool:
            leases = list(pool.map(acquire_once, range(4)))
        assert len([lease for lease in leases if lease is not None]) == 1
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
