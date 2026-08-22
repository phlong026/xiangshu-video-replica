"""T10 / ACT-01 — activation code catalog schema (revision 027).

Fail-first PostgreSQL tests for the frozen file
``server/migrations/versions/027_activation_code_catalog.py``: batches,
codes, deliveries, exports and the one-shot activation fact table with the
invariants from dev doc §5 / §11.2 / §11.3 and ACT-01's exit gate (one code
per user, status machine, unique bindings, traceable records) — proven by
PostgreSQL constraints, never by application code. No-Go red line: the
database must never store activation-code plaintext (exact column sets are
asserted so no plaintext column can slip in).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

BATCH_TABLE = "activation_code_batches"
CODES_TABLE = "activation_codes"
DELIVERIES_TABLE = "activation_code_deliveries"
EXPORTS_TABLE = "activation_code_exports"
ACTIVATIONS_TABLE = "activation_code_activations"
EVENTS_TABLE = "activation_code_events"

_HEAD_REVISION = "028_admin_write_idempotency"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _pg_available(_pg_dsn()),
    reason=SKIP_REASON,
)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


@pytest.fixture()
def catalog_dsn() -> str:
    """Fresh database at head with one admin and one customer user."""
    from alembic import command

    db_name = "t10_activation_catalog"
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    command.upgrade(_alembic_config(dsn.replace("postgresql://", "postgresql+psycopg://")), "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('u-admin', 'u-admin', 'Admin', 'admin')"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('u-cust', 'u-cust', 'Customer', 'employee')"
        )
    try:
        yield dsn
    finally:
        _drop_database(db_name)


def _insert_batch(conn: psycopg.Connection, seq: int = 1, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": f"batch-{seq}",
        "name": f"Batch {seq}",
        "face_value_fen": 1500,
        "unit_price_fen_snapshot": 1500,
        "credits_snapshot": 100,
        "quantity": 10,
        "activation_expires_at": "2099-01-01T00:00:00+00:00",
        "status": "OPEN",
        "created_by_user_id": "u-admin",
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    conn.execute(
        f"INSERT INTO {BATCH_TABLE} ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _insert_code(conn: psycopg.Connection, seq: int = 1, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": f"code-{seq}",
        "batch_id": "batch-1",
        "code_digest": f"digest-{seq}",
        "digest_key_version": 1,
        "masked_code": f"XS04-****-****-{seq:04d}",
        "status": "ISSUED",
        "issued_at": "2026-08-22T00:00:00+00:00",
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    conn.execute(
        f"INSERT INTO {CODES_TABLE} ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def _insert_activation(conn: psycopg.Connection, seq: int = 1, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": f"act-{seq}",
        "code_id": "code-1",
        "user_id": "u-cust",
        "recharge_order_id": f"order-{seq}",
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    conn.execute(
        f"INSERT INTO {ACTIVATIONS_TABLE} ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


# ---------------------------------------------------------------------------
# Shape: the five catalog tables with the frozen column sets
# ---------------------------------------------------------------------------


def test_catalog_tables_and_columns(catalog_dsn: str) -> None:
    """ACT-01 red line: exact column sets — no plaintext code column, digest
    and key version mandatory, every operation traceable to a real actor."""
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _HEAD_REVISION

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        for table in (
            BATCH_TABLE,
            CODES_TABLE,
            DELIVERIES_TABLE,
            EXPORTS_TABLE,
            ACTIVATIONS_TABLE,
            EVENTS_TABLE,
        ):
            assert table in tables, f"missing table {table} after upgrade head"

        def columns(table: str) -> set[str]:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (table,),
                ).fetchall()
            }

        assert columns(BATCH_TABLE) == {
            "id",
            "name",
            "face_value_fen",
            "unit_price_fen_snapshot",
            "credits_snapshot",
            "quantity",
            "activation_expires_at",
            "status",
            "created_by_user_id",
            "created_at",
        }
        assert columns(CODES_TABLE) == {
            "id",
            "batch_id",
            "code_digest",
            "digest_key_version",
            "masked_code",
            "status",
            "bound_user_id",
            "issued_at",
            "activated_at",
            "suspended_at",
            "revoked_at",
            "expired_at",
        }
        assert columns(DELIVERIES_TABLE) == {
            "id",
            "code_id",
            "channel",
            "external_order_ref",
            "recipient_ref",
            "delivered_by_user_id",
            "delivered_at",
            "note",
        }
        assert columns(EXPORTS_TABLE) == {
            "id",
            "batch_id",
            "ciphertext",
            "ciphertext_sha256",
            "key_version",
            "requested_by_user_id",
            "created_at",
            "expires_at",
            "downloaded_at",
            "downloaded_by_user_id",
        }
        assert columns(ACTIVATIONS_TABLE) == {
            "id",
            "code_id",
            "user_id",
            "first_device_id",
            "recharge_order_id",
            "activated_at",
        }
        assert columns(EVENTS_TABLE) == {
            "id",
            "code_id",
            "event",
            "actor_user_id",
            "reason",
            "request_id",
            "created_at",
        }


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def test_batch_shapes_enforced(catalog_dsn: str) -> None:
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)  # legal baseline

        def rejected(seq: int, **overrides: object) -> None:
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_batch(conn, seq, **overrides)

        rejected(2, status="DRAFT")  # unknown batch status
        rejected(3, face_value_fen=0)  # face value must be positive
        rejected(4, unit_price_fen_snapshot=-1)  # price snapshot is unsigned
        rejected(5, credits_snapshot=0)  # an activation code must carry credits
        rejected(6, quantity=0)  # a batch generates at least one code
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_batch(conn, 7, created_by_user_id="u-ghost")
        with pytest.raises(psycopg.errors.CheckViolation):
            # activation window must outlive the batch creation
            _insert_batch(conn, 8, activation_expires_at="2020-01-01T00:00:00+00:00")
        with pytest.raises(psycopg.errors.CheckViolation):
            # same-day midnight is lexically larger than the space-separated
            # server default ('2026-08-22T00:00:00+00:00' > '2026-08-22 10:18:02+00'
            # by byte order) yet still a real timestamp in the past — the
            # CHECK must compare timestamps, not text (PR-review P2)
            conn.execute(
                f"INSERT INTO {BATCH_TABLE} "
                "(id, name, face_value_fen, unit_price_fen_snapshot, "
                " credits_snapshot, quantity, activation_expires_at, "
                " status, created_by_user_id) "
                "VALUES ('batch-9', 'Same-day', 1500, 1500, 100, 10, "
                " CURRENT_DATE::text || 'T00:00:00+00:00', 'OPEN', 'u-admin')"
            )


# ---------------------------------------------------------------------------
# Codes: digest uniqueness, status machine, one binding
# ---------------------------------------------------------------------------


def test_code_digest_globally_unique(catalog_dsn: str) -> None:
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_batch(conn, 2)
        _insert_code(conn, 1, batch_id="batch-1")
        # The same digest from a *different* batch is still a collision: a
        # CSPRNG never repeats, so cross-batch duplicates must be rejected.
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_code(conn, 2, batch_id="batch-2", code_digest="digest-1")


def test_code_status_machine_enforced(catalog_dsn: str) -> None:
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_code(conn, 1)  # ISSUED: unbound, untouched timestamps

        # ISSUED must not carry a binding (§12.1 binds on activation only).
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 2, bound_user_id="u-cust")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 3, activated_at="2026-08-22T01:00:00+00:00")

        # ACTIVE requires the binding and the activation timestamp.
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 4, status="ACTIVE")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 5, status="ACTIVE", activated_at="2026-08-22T01:00:00+00:00")
        _insert_code(
            conn,
            6,
            status="ACTIVE",
            bound_user_id="u-cust",
            activated_at="2026-08-22T01:00:00+00:00",
        )

        # SUSPENDED requires suspended_at; REVOKED requires revoked_at.
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 7, status="SUSPENDED")
        _insert_code(conn, 8, status="SUSPENDED", suspended_at="2026-08-22T02:00:00+00:00")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 9, status="REVOKED")
        _insert_code(conn, 10, status="REVOKED", revoked_at="2026-08-22T02:00:00+00:00")

        # A binding and an activation timestamp always appear together —
        # side states never break the pair (PR-review P3 defense-in-depth).
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(
                conn,
                16,
                status="SUSPENDED",
                suspended_at="2026-08-22T02:00:00+00:00",
                bound_user_id="u-cust",
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(
                conn,
                17,
                status="REVOKED",
                revoked_at="2026-08-22T02:00:00+00:00",
                activated_at="2026-08-22T01:00:00+00:00",
            )

        # Unknown statuses (including pre-V3 draft names) are rejected.
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 11, status="UNASSIGNED")
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 12, status="ASSIGNED")

        # Digest hygiene: versioned and non-blank.
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 13, digest_key_version=0)
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_code(conn, 14, masked_code="   ")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_code(conn, 15, batch_id="batch-ghost")

        # Acceptance spec §2.1: the full six-state machine — GENERATED is the
        # pre-delivery state (no issued_at yet), EXPIRED is the unactivated
        # end state past the batch activation window (PR-review P1).
        _insert_code(conn, 18, status="GENERATED", issued_at=None)
        with pytest.raises(psycopg.errors.CheckViolation):
            # GENERATED has not been delivered, so issued_at must stay NULL
            _insert_code(conn, 19, status="GENERATED")
        _insert_code(
            conn,
            20,
            status="EXPIRED",
            expired_at="2026-08-23T00:00:00+00:00",
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            # expiry never follows activation — a bound code cannot expire
            _insert_code(
                conn,
                21,
                status="EXPIRED",
                expired_at="2026-08-23T00:00:00+00:00",
                bound_user_id="u-cust",
                activated_at="2026-08-22T01:00:00+00:00",
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            # EXPIRED needs its own proof timestamp
            _insert_code(conn, 22, status="EXPIRED")


def test_one_active_binding_per_user(catalog_dsn: str) -> None:
    """ACT-01 exit gate: one user owns at most one currently-valid code."""
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_code(
            conn,
            1,
            status="ACTIVE",
            bound_user_id="u-cust",
            activated_at="2026-08-22T01:00:00+00:00",
        )
        # A second ACTIVE (or SUSPENDED) code bound to the same user collides
        # on the partial unique index over current bindings.
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_code(
                conn,
                2,
                status="ACTIVE",
                bound_user_id="u-cust",
                activated_at="2026-08-22T02:00:00+00:00",
            )
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_code(
                conn,
                3,
                status="SUSPENDED",
                bound_user_id="u-cust",
                activated_at="2026-08-22T02:00:00+00:00",
                suspended_at="2026-08-22T03:00:00+00:00",
            )
        # ISSUED rows never bind, so they do not occupy the user slot.
        _insert_code(conn, 4)


# ---------------------------------------------------------------------------
# Deliveries and exports: traceable, never plaintext
# ---------------------------------------------------------------------------


def test_delivery_traceability(catalog_dsn: str) -> None:
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_code(conn, 1)
        conn.execute(
            f"INSERT INTO {DELIVERIES_TABLE} "
            "(id, code_id, channel, external_order_ref, recipient_ref, "
            " delivered_by_user_id, note) "
            "VALUES ('dlv-1', 'code-1', 'ecommerce', 'EXT-1001', 'buyer-ref', "
            " 'u-admin', 'initial handout')"
        )
        # Re-delivery records are allowed (channel corrections), so no unique
        # constraint on code_id — but the actor must exist.
        conn.execute(
            f"INSERT INTO {DELIVERIES_TABLE} "
            "(id, code_id, channel, delivered_by_user_id) "
            "VALUES ('dlv-2', 'code-1', 'manual', 'u-admin')"
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                f"INSERT INTO {DELIVERIES_TABLE} "
                "(id, code_id, channel, delivered_by_user_id) "
                "VALUES ('dlv-3', 'code-1', 'manual', 'u-ghost')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                f"INSERT INTO {DELIVERIES_TABLE} "
                "(id, code_id, channel, delivered_by_user_id) "
                "VALUES ('dlv-4', 'code-1', '   ', 'u-admin')"
            )


def test_export_ciphertext_only_with_expiry(catalog_dsn: str) -> None:
    """ACT-03 groundwork: exports carry AEAD ciphertext + SHA-256 digest with a
    short-lived expiry and a one-time download audit trail — never plaintext."""
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        conn.execute(
            f"INSERT INTO {EXPORTS_TABLE} "
            "(id, batch_id, ciphertext, ciphertext_sha256, key_version, "
            " requested_by_user_id, expires_at) "
            "VALUES ('exp-1', 'batch-1', 'aead-ciphertext', "
            "'sha256-digest', 1, 'u-admin', '2099-01-01T00:00:00+00:00')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                f"INSERT INTO {EXPORTS_TABLE} "
                "(id, batch_id, ciphertext, ciphertext_sha256, key_version, "
                " requested_by_user_id, expires_at) "
                "VALUES ('exp-2', 'batch-1', 'aead-ciphertext', "
                "'sha256-digest', 0, 'u-admin', '2099-01-01T00:00:00+00:00')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            # short-lived: an export already expired at creation is refused
            conn.execute(
                f"INSERT INTO {EXPORTS_TABLE} "
                "(id, batch_id, ciphertext, ciphertext_sha256, key_version, "
                " requested_by_user_id, expires_at) "
                "VALUES ('exp-3', 'batch-1', 'aead-ciphertext', "
                "'sha256-digest', 1, 'u-admin', '2020-01-01T00:00:00+00:00')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            # same-day midnight would pass a lexical comparison against the
            # space-separated created_at default but is a real past timestamp
            # — short-lived exports are exactly the same-day case the CHECK
            # must actually catch (PR-review P2)
            conn.execute(
                f"INSERT INTO {EXPORTS_TABLE} "
                "(id, batch_id, ciphertext, ciphertext_sha256, key_version, "
                " requested_by_user_id, expires_at) "
                "VALUES ('exp-5', 'batch-1', 'aead-ciphertext', "
                "'sha256-digest', 1, 'u-admin', "
                " CURRENT_DATE::text || 'T00:00:00+00:00')"
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                f"INSERT INTO {EXPORTS_TABLE} "
                "(id, batch_id, ciphertext, ciphertext_sha256, key_version, "
                " requested_by_user_id, expires_at) "
                "VALUES ('exp-4', 'batch-ghost', 'aead-ciphertext', "
                "'sha256-digest', 1, 'u-admin', '2099-01-01T00:00:00+00:00')"
            )


def test_activation_code_events_append_only(catalog_dsn: str) -> None:
    """ACT-01 work package: the catalog carries an append-only event table —
    suspension, revocation, delivery and activation leave an immutable
    actor/reason/request record that PostgreSQL itself refuses to rewrite
    (PR-review P1)."""
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_code(conn, 1)
        conn.execute(
            f"INSERT INTO {EVENTS_TABLE} "
            "(id, code_id, event, actor_user_id, reason, request_id) "
            "VALUES ('evt-1', 'code-1', 'GENERATED', 'u-admin', "
            " 'batch generated', 'req-1')"
        )
        conn.execute(
            f"INSERT INTO {EVENTS_TABLE} (id, code_id, event) "
            "VALUES ('evt-2', 'code-1', 'ACTIVATED')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                f"INSERT INTO {EVENTS_TABLE} (id, code_id, event) "
                "VALUES ('evt-3', 'code-1', 'UNKNOWN')"
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                f"INSERT INTO {EVENTS_TABLE} (id, code_id, event, actor_user_id) "
                "VALUES ('evt-4', 'code-1', 'REVOKED', 'u-ghost')"
            )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(f"UPDATE {EVENTS_TABLE} SET reason = 'tampered' WHERE id = 'evt-1'")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(f"DELETE FROM {EVENTS_TABLE} WHERE id = 'evt-1'")
        count = conn.execute(f"SELECT COUNT(*) FROM {EVENTS_TABLE}").fetchone()[0]
        assert count == 2  # both rows survived the refused rewrites


# ---------------------------------------------------------------------------
# One-shot activation facts
# ---------------------------------------------------------------------------


def _insert_paid_order(conn: psycopg.Connection, order_id: str) -> None:
    # T08 shape: activation_code orders land PAID at the customer scope with
    # credits * charged price == amount (1500 fen * 1 credit).
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, pricing_scope, status, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, "
        " credits, paid_at) "
        "VALUES (%s, 'u-cust', %s, 'activation_code', 'CUSTOMER_STANDARD', "
        " 'PAID', 1500, 1500, 10000, 1000, 1500, 1, "
        " '2026-08-22T01:00:00+00:00')",
        (order_id, f"MOCK-{order_id}"),
    )


def test_activation_fact_one_shot_uniqueness(catalog_dsn: str) -> None:
    """§11.3: code_id UNIQUE, user_id UNIQUE and the first-charge order is
    unique — activation is a one-time fact per code and per user."""
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_code(
            conn,
            1,
            status="ACTIVE",
            bound_user_id="u-cust",
            activated_at="2026-08-22T01:00:00+00:00",
        )
        _insert_paid_order(conn, "order-1")
        _insert_activation(conn, 1, first_device_id="device-slot-1")

        _insert_paid_order(conn, "order-2")
        # The same code cannot activate twice.
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_activation(conn, 2, user_id="u-admin", recharge_order_id="order-2")
        # The same user cannot activate a second code.
        _insert_code(
            conn,
            3,
            status="ACTIVE",
            bound_user_id="u-admin",
            activated_at="2026-08-22T02:00:00+00:00",
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_activation(conn, 3, code_id="code-3", recharge_order_id="order-2")
        # The first-charge order cannot back a second activation fact.
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_activation(
                conn, 4, code_id="code-3", user_id="u-admin", recharge_order_id="order-1"
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            # Only the first-charge order is missing: code-3 and u-admin were
            # never inserted into the fact table (the attempts above were
            # rejected), so the FK on recharge_order_id is the lone violator.
            _insert_activation(
                conn, 5, code_id="code-3", user_id="u-admin", recharge_order_id="order-ghost"
            )


# ---------------------------------------------------------------------------
# Downgrade symmetry
# ---------------------------------------------------------------------------


def test_downgrade_drops_catalog_and_blocks_when_activated(catalog_dsn: str) -> None:
    from alembic import command

    sqlalchemy_dsn = catalog_dsn.replace("postgresql://", "postgresql+psycopg://")

    # With an activation fact present the downgrade must refuse loudly
    # (audit chain: activation -> PAID order -> CHARGE ledger).
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        _insert_batch(conn, 1)
        _insert_code(
            conn,
            1,
            status="ACTIVE",
            bound_user_id="u-cust",
            activated_at="2026-08-22T01:00:00+00:00",
        )
        _insert_paid_order(conn, "order-1")
        _insert_activation(conn, 1)
    with pytest.raises(RuntimeError, match="cannot downgrade 027"):
        # Two steps: 028->027 (empty idempotency ledger, symmetric) then
        # 027->026, which the guard refuses.
        command.downgrade(_alembic_config(sqlalchemy_dsn), "-2")

    # Remove the fact (test data only) and the downgrade is symmetric.
    with psycopg.connect(catalog_dsn, autocommit=True) as conn:
        conn.execute(f"DELETE FROM {ACTIVATIONS_TABLE}")
    command.downgrade(_alembic_config(sqlalchemy_dsn), "-2")
    with psycopg.connect(catalog_dsn) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "026_customer_security_and_billing"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        for table in (
            BATCH_TABLE,
            CODES_TABLE,
            DELIVERIES_TABLE,
            EXPORTS_TABLE,
            ACTIVATIONS_TABLE,
        ):
            assert table not in tables, f"{table} must be dropped by downgrade"
