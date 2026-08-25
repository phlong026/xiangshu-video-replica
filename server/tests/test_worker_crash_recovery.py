"""T26 — worker crash recovery and provider-submission uncertainty (PG lane).

The fixture database is migrated with ``alembic upgrade head`` (the T25
precedent) so every query runs against the real revision-040 schema. The
tests drive the actual business functions on the PostgreSQL lane the way the
PG worker loop does: every logical operation inside its own fenced
``pg_transaction`` (T25).

Test matrix (T26 ADR §6 / BILL-03):
- crash inside the fenced transaction rolls the whole round back: the lease
  UPDATE, the cursor counter and the wallet RESERVE all disappear, the task
  is PENDING again and another worker can claim it — no double pay;
- an expired lease is taken over defensively with a worker-owned audit row
  (actor_user_id NULL) and the per-user slot released, for both the
  SUBMISSION_UNCERTAIN branch and the archive-retry branch;
- reconciliation on the PG lane settles exactly one terminal billing row
  (never a second), and a cancelled provider release is equally one-shot;
- reconciliation refuses to guess when the provider task id is unknown
  (SUBMISSION_REQUIRES_MANUAL_CONFIRMATION keeps the charge human-verified);
- BILL-03: dangling RESERVE rows of terminal tasks are detected.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t26-worker-reliability-tests-minimum-48-bytes-long-12",
)

import psycopg
import pytest
from fastapi import HTTPException

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation import (
    MetasoH3Provider,
    acquire_generation_task_lease,
    mark_expired_active_leases_needing_attention,
    reconcile_submission_uncertain_task,
)
from app.internal_billing import (
    find_dangling_billing_reservations,
    reserve_internal_billing,
)
from app.storage import FakeStorageAdapter

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

T26_DB_NAME = "t26_worker_reliability_test"

_FAIR_TABLES = (
    "user_queue_cursors, generation_task_operations, external_call_logs, "
    "assets, wallet_transactions, recharge_orders, internal_access_tokens, "
    "wallets, generation_tasks, generation_batches, audit_logs, "
    "runtime_settings, users"
)

_REQUEST_SNAPSHOT = '{"output_duration_seconds": 3, "resolution": "480p"}'
_PROMPT_SNAPSHOT = '{"prompt_text": "a cat", "first_frame_uri": "cos://bucket/ff.jpg"}'


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t26_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T26_DB_NAME}"


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fair_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip(SKIP_REASON)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T26_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T26_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t26_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t26_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T26_DB_NAME}" WITH (FORCE)')


def _seed(
    dsn: str,
    *,
    user_ids: list[str],
    tasks_per_user: int,
    task_status: str = "PENDING",
    wallet_credits: int = 1000,
) -> None:
    """TRUNCATE and re-seed users / batches / tasks / cursors / wallets."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_FAIR_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('proj-owner', 'proj_owner', 'Project Owner', 'user')"
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-1', 'Crash Recovery Project', 'proj-owner')"
        )
        pg.execute(
            "INSERT INTO versions (id, project_id, kind, version_number, payload_json) "
            "VALUES ('pv-1', 'proj-1', 'video', 1, '{}')"
        )
        for user_id in user_ids:
            pg.execute(
                "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'user')",
                (user_id, user_id, user_id),
            )
            pg.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, %s, 0)",
                (user_id, wallet_credits),
            )
            batch_id = f"batch-{user_id}"
            pg.execute(
                "INSERT INTO generation_batches ("
                " id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json, status"
                ") VALUES (%s, 'proj-1', %s, %s, %s, %s, 'QUEUED')",
                (batch_id, user_id, f"ik-{user_id}", f"rh-{user_id}", _REQUEST_SNAPSHOT),
            )
            for task_index in range(tasks_per_user):
                pg.execute(
                    "INSERT INTO generation_tasks ("
                    " id, batch_id, generation_mode, provider, model, "
                    " status, archive_status, quality_status, "
                    " prompt_version_id, prompt_snapshot_json, next_poll_at"
                    ") VALUES (%s, %s, 'I2V', 'metaso', 'h3', %s, "
                    "'PENDING', 'PENDING', 'pv-1', %s, NULL)",
                    (
                        f"task-{user_id}-{task_index}",
                        batch_id,
                        task_status,
                        _PROMPT_SNAPSHOT,
                    ),
                )
            pg.execute(
                "INSERT INTO user_queue_cursors "
                "(user_id, last_dispatched_at, running_tasks_count) "
                "VALUES (%s, now(), 0)",
                (user_id,),
            )


def _acquire(dsn: str, worker_id: str) -> dict[str, object] | None:
    """Acquire a lease the way the PG worker loop does: one fenced transaction."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            return dict(lease)
        return None


def _cursor_count(dsn: str, user_id: str) -> int:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        row = conn.execute(
            "SELECT running_tasks_count FROM user_queue_cursors WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row is not None else -1


def _billing_rows(dsn: str, task_id: str) -> list[tuple[str, int]]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT type, billing_round FROM wallet_transactions "
            "WHERE task_id = %s "
            "ORDER BY billing_round, CASE WHEN type = 'RESERVE' THEN 0 ELSE 1 END, type",
            (task_id,),
        ).fetchall()
        return [(str(row["type"]), int(row["billing_round"])) for row in rows]


def _audit_actions(dsn: str) -> list[tuple[str, str | None, str]]:
    """(action, actor_user_id, entity_id) audit rows, oldest first."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            "SELECT action, actor_user_id, entity_id FROM audit_logs ORDER BY created_at, id"
        ).fetchall()
        return [(str(row["action"]), row["actor_user_id"], str(row["entity_id"])) for row in rows]


def _seed_reserved(
    dsn: str, *, task_id: str, available_credits: int, reserved_credits: int
) -> None:
    """Post-crash state: a committed RESERVE with the wallet moved (T26)."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "UPDATE wallets SET available_credits = %s, reserved_credits = %s WHERE user_id = 'u1'",
            (available_credits, reserved_credits),
        )
        pg.execute(
            "INSERT INTO wallet_transactions ("
            " id, user_id, type, available_delta, reserved_delta, "
            " task_id, billing_round, idempotency_key"
            ") VALUES (%s, 'u1', 'RESERVE', -1, 1, %s, 1, %s)",
            (f"wt-{task_id}-r1", task_id, f"reserve:{task_id}:1"),
        )


@pytest.fixture()
def fair_state(fair_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, fair_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield fair_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Crash inside the fenced transaction
# ---------------------------------------------------------------------------


def test_crash_inside_fenced_transaction_rolls_back_everything(fair_state: str) -> None:
    """A worker dying between the lease and the paid call must leave no trace:
    task back to PENDING, cursor 0, no wallet RESERVE, re-claimable."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    with pytest.raises(RuntimeError, match="simulated crash"):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            lease = acquire_generation_task_lease(conn, worker_id="w1")
            assert lease is not None
            assert str(lease["id"]) == "task-u1-0"
            reserve_internal_billing(conn, user_id="u1", task_id=str(lease["id"]), billing_round=1)
            raise RuntimeError("simulated crash")

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, locked_by, locked_until FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    assert task["status"] == "PENDING"
    assert task["locked_by"] is None
    assert task["locked_until"] is None
    assert int(wallet["available_credits"]) == 1000
    assert int(wallet["reserved_credits"]) == 0
    assert _billing_rows(fair_state, "task-u1-0") == []
    assert int(audit_count) == 0  # even the defensive-takeover scan wrote nothing
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None  # the same task is claimable again, no double pay
    assert str(lease2["id"]) == "task-u1-0"


# ---------------------------------------------------------------------------
# Expired lease takeover (audited, slot released)
# ---------------------------------------------------------------------------


def test_expired_lease_audited_and_slot_released(fair_state: str) -> None:
    """A worker that vanished mid-call: SUBMISSION_UNCERTAIN, worker-owned
    audit row, per-user slot freed for the user's next task."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=2, wallet_credits=1000)
    lease = _acquire(fair_state, "w1")
    assert lease is not None
    assert _cursor_count(fair_state, "u1") == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE generation_tasks SET locked_until = now() - interval '5 minutes' WHERE id = %s",
            (str(lease["id"]),),
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, error_code, locked_by, locked_until "
            "FROM generation_tasks WHERE id = %s",
            (str(lease["id"]),),
        ).fetchone()
    assert task["status"] == "SUBMISSION_UNCERTAIN"
    assert task["error_code"] == "LEASE_EXPIRED_NEEDS_ATTENTION"
    assert task["locked_by"] is None
    assert task["locked_until"] is None
    assert _audit_actions(fair_state) == [
        ("generation_task.lease_expired_uncertain", None, str(lease["id"]))
    ]
    assert _cursor_count(fair_state, "u1") == 0
    lease2 = _acquire(fair_state, "w2")
    assert lease2 is not None  # the user's next task is schedulable
    assert str(lease2["id"]) == "task-u1-1"


def test_expired_archive_retry_lease_audited_and_reset(fair_state: str) -> None:
    """Archive retries never start a paid call: an expired retry lease just
    resets to SUCCEEDED/ARCHIVE_FAILED for another attempt, audited."""
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks "
            "SET status = 'ARCHIVING', archive_status = 'ARCHIVE_FAILED', "
            "    provider_result_url = 'cos://bucket/result.mp4', "
            "    locked_by = 'w1', locked_until = now() - interval '5 minutes' "
            "WHERE id = 'task-u1-0'"
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        mark_expired_active_leases_needing_attention(conn)

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, archive_status, next_poll_at, locked_by "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
    assert task["status"] == "SUCCEEDED"
    assert task["archive_status"] == "ARCHIVE_FAILED"
    assert task["next_poll_at"] is not None  # retry backoff scheduled
    assert task["locked_by"] is None
    assert _audit_actions(fair_state) == [
        ("generation_task.lease_expired_archive_retry", None, "task-u1-0")
    ]
    assert _cursor_count(fair_state, "u1") == 0  # GREATEST guard: no-op is safe


# ---------------------------------------------------------------------------
# Reconciliation on the PG lane (terminal billing exactly once)
# ---------------------------------------------------------------------------


class _ReconcileSucceededProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {
            "id": provider_task_id,
            "status": "succeeded",
            "content": {"url": "https://example.com/results/ok.mp4"},
        }

    def download_result(self, url: str) -> bytes:
        return b"reconciled-mp4-bytes"


class _ReconcileCancelledProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {"id": provider_task_id, "status": "cancelled"}


def _reconcile(fair_state: str, task_id: str, provider: MetasoH3Provider) -> object:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return reconcile_submission_uncertain_task(
            conn,
            task_id=task_id,
            batch_id="batch-u1",
            project_id="proj-1",
            created_by_user_id="u1",
            storage_factory=lambda: FakeStorageAdapter(provider="cos", bucket="generation-results"),
            provider=provider,
        )


def test_reconcile_success_settles_exactly_once(
    fair_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciling a succeeded provider result archives it and settles the
    reserved credit exactly once; a repeated reconcile never settles twice."""
    # The SSRF guard is covered by its own tests; here the fake provider's
    # example.com result URL must not trigger a real DNS/address check. Patch
    # only the module-level guard, never the socket module (psycopg relies on
    # the real one for the PG pool).
    monkeypatch.setattr("app.generation._require_public_https_host", lambda _host: None)
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE generation_tasks SET provider_task_id = 'pt-1' WHERE id = 'task-u1-0'")
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)

    result = _reconcile(fair_state, "task-u1-0", _ReconcileSucceededProvider(api_key="test-key"))
    assert result is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute(
            "SELECT status, archive_status, result_asset_id "
            "FROM generation_tasks WHERE id = 'task-u1-0'"
        ).fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert task["status"] == "SUCCEEDED"
    assert task["archive_status"] == "ARCHIVED"
    assert task["result_asset_id"] is not None
    assert int(wallet["available_credits"]) == 999
    assert int(wallet["reserved_credits"]) == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]

    # A re-marked uncertain task reconciled again must not settle twice.
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE generation_tasks SET status = 'SUBMISSION_UNCERTAIN', "
            "error_code = 'SUBMISSION_UNCERTAIN', result_asset_id = NULL "
            "WHERE id = 'task-u1-0'"
        )
    _reconcile(fair_state, "task-u1-0", _ReconcileSucceededProvider(api_key="test-key"))
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("SETTLE", 1)]


def test_reconcile_cancelled_releases_exactly_once(fair_state: str) -> None:
    """A provider-reported cancellation releases the reserved credit exactly
    once; the wallet is restored to its pre-reservation balance."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        pg.execute("UPDATE generation_tasks SET provider_task_id = 'pt-1' WHERE id = 'task-u1-0'")
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)

    result = _reconcile(fair_state, "task-u1-0", _ReconcileCancelledProvider(api_key="test-key"))
    assert result is not None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        task = conn.execute("SELECT status FROM generation_tasks WHERE id = 'task-u1-0'").fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert task["status"] == "FAILED"
    assert int(wallet["available_credits"]) == 1000
    assert int(wallet["reserved_credits"]) == 0
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("RELEASE", 1)]


def test_reconcile_refuses_without_provider_task_id(fair_state: str) -> None:
    """No provider task id: refuse to guess, keep the charge reserved and the
    wallet untouched until a human confirms (SUBMISSION_REQUIRES_MANUAL_*)."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=1,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    _seed_reserved(fair_state, task_id="task-u1-0", available_credits=999, reserved_credits=1)

    with pytest.raises(HTTPException) as exc_info:
        _reconcile(fair_state, "task-u1-0", _ReconcileCancelledProvider(api_key="test-key"))
    assert exc_info.value.status_code == 409
    detail = cast(dict[str, object], exc_info.value.detail)
    assert detail["code"] == "SUBMISSION_REQUIRES_MANUAL_CONFIRMATION"
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u1'"
        ).fetchone()
    assert int(wallet["available_credits"]) == 999
    assert int(wallet["reserved_credits"]) == 1


# ---------------------------------------------------------------------------
# BILL-03: dangling reservation detection
# ---------------------------------------------------------------------------


def test_find_dangling_billing_reservations_detects_bill_03(fair_state: str) -> None:
    """Only terminal tasks without a terminal billing row are flagged:
    archived success / failed-cancelled with a dangling RESERVE; uncertain
    tasks and already-settled tasks stay untouched."""
    _seed(
        fair_state,
        user_ids=["u1"],
        tasks_per_user=4,
        task_status="SUBMISSION_UNCERTAIN",
        wallet_credits=1000,
    )
    with psycopg.connect(fair_state, autocommit=True) as pg:
        # t1, t2: archived success; t3 stays SUBMISSION_UNCERTAIN; t4: failed.
        pg.execute(
            "UPDATE generation_tasks SET status = 'SUCCEEDED', archive_status = 'ARCHIVED' "
            "WHERE id IN ('task-u1-0', 'task-u1-1')"
        )
        pg.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task-u1-3'")
        for task_id in ("task-u1-0", "task-u1-1", "task-u1-2", "task-u1-3"):
            pg.execute(
                "INSERT INTO wallet_transactions ("
                " id, user_id, type, available_delta, reserved_delta, "
                " task_id, billing_round, idempotency_key"
                ") VALUES (%s, 'u1', 'RESERVE', -1, 1, %s, 1, %s)",
                (f"wt-{task_id}-r1", task_id, f"reserve:{task_id}:1"),
            )
        pg.execute(
            "INSERT INTO wallet_transactions ("
            " id, user_id, type, available_delta, reserved_delta, "
            " task_id, billing_round, idempotency_key"
            ") VALUES ('wt-task-u1-1-s1', 'u1', 'SETTLE', 0, -1, "
            "'task-u1-1', 1, 'settle:task-u1-1:1')"
        )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        dangling = find_dangling_billing_reservations(conn)
    assert {d.task_id for d in dangling} == {"task-u1-0", "task-u1-3"}
    assert {d.billing_round for d in dangling} == {1}
    assert {d.user_id for d in dangling} == {"u1"}
