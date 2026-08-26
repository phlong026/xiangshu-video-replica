"""T37 / OPS-02 — single-owner cluster anomaly probe contracts."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _pg_available(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:
        return False


def test_cluster_probe_covers_the_frozen_alert_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import check_ops_alerts as alerts

    counts = {
        "double_online": 1,
        "duplicate_charge": 2,
        "duplicate_provider_trade_no": 3,
        "paid_without_charge": 4,
        "charge_without_paid_source": 5,
        "wallet_balance_mismatch": 6,
        "fencing_rejected": 7,
        "cross_user_access_denied": 8,
        "queue_starvation": 9,
        "dangling_reserve": 10,
    }
    observed_queries: list[tuple[str, tuple[object, ...]]] = []

    def fake_count(
        _conn: object,
        name: str,
        _query: str,
        parameters: tuple[object, ...],
    ) -> int:
        observed_queries.append((name, parameters))
        return counts[name]

    monkeypatch.setattr(alerts, "_count_query", fake_count)
    monkeypatch.setattr(
        alerts,
        "_server_now",
        lambda _conn: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    observations = alerts.collect_observations(object(), alerts.ProbeConfig(row_limit=100))

    assert {item.name: item.observed_count for item in observations} == counts
    assert {name for name, _parameters in observed_queries} == set(counts)
    assert all(item.active for item in observations)


def test_every_cluster_query_is_bounded_and_uses_only_application_tables() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    assert ALERT_QUERIES
    for name, query in ALERT_QUERIES.items():
        normalized = " ".join(query.lower().split())
        assert "limit %s" in normalized, name
        assert "pg_stat" not in normalized, name
        assert "pg_locks" not in normalized, name
        assert "information_schema" not in normalized, name


def test_double_online_groups_by_session_epoch_to_avoid_switch_false_positives() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["double_online"].lower().split())
    assert "group by user_id, session_epoch" in normalized


def test_cross_user_alert_uses_only_durable_denial_audit_facts() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["cross_user_access_denied"].lower().split())
    assert "from audit_logs" in normalized
    assert "security.project_denied" in normalized
    assert "security.asset_denied" in normalized
    assert "metadata_json" not in normalized
    assert "entity_id" not in normalized


def test_fencing_cutoff_matches_the_isoformat_security_fact_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import check_ops_alerts as alerts

    observed_parameters: dict[str, tuple[object, ...]] = {}

    def fake_count(
        _conn: object,
        name: str,
        _query: str,
        parameters: tuple[object, ...],
    ) -> int:
        observed_parameters[name] = parameters
        return 0

    monkeypatch.setattr(alerts, "_count_query", fake_count)
    monkeypatch.setattr(
        alerts,
        "_server_now",
        lambda _conn: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    alerts.collect_observations(object(), alerts.ProbeConfig(fencing_window_seconds=300))

    assert observed_parameters["fencing_rejected"][0] == "2026-08-26T11:55:00+00:00"
    assert observed_parameters["double_online"][0] == "2026-08-26 11:58:00+00:00"


def test_queue_starvation_filters_for_waiting_work_before_candidate_limit() -> None:
    from scripts.check_ops_alerts import ALERT_QUERIES

    normalized = " ".join(ALERT_QUERIES["queue_starvation"].lower().split())
    assert normalized.index("exists (") < normalized.index("limit %s")


def test_transition_engine_emits_only_fired_and_resolved_edges() -> None:
    from scripts.check_ops_alerts import AlertObservation, transition_alerts

    first = (
        AlertObservation("double_online", True, 1),
        AlertObservation("fencing_rejected", False, 0),
    )
    transitions, state = transition_alerts({}, first)
    assert [(item.name, item.transition) for item in transitions] == [("double_online", "fired")]
    assert state == {"double_online": True, "fencing_rejected": False}

    transitions, state = transition_alerts(state, first)
    assert transitions == ()

    recovered = (
        AlertObservation("double_online", False, 0),
        AlertObservation("fencing_rejected", True, 2),
    )
    transitions, state = transition_alerts(state, recovered)
    assert [(item.name, item.transition) for item in transitions] == [
        ("double_online", "resolved"),
        ("fencing_rejected", "fired"),
    ]
    assert state == {"double_online": False, "fencing_rejected": True}


def test_state_file_is_atomic_private_and_rejects_corruption(tmp_path: Path) -> None:
    from scripts.check_ops_alerts import load_state, save_state

    target = tmp_path / "ops-alerts.json"
    save_state(target, {"double_online": True, "wallet_balance_mismatch": False})

    assert load_state(target) == {
        "double_online": True,
        "wallet_balance_mismatch": False,
    }
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))

    target.write_text("{not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="state file is invalid"):
        load_state(target)


def test_transition_output_never_contains_subjects_or_connection_details() -> None:
    from scripts.check_ops_alerts import AlertObservation, render_transition, transition_alerts

    transitions, _state = transition_alerts(
        {}, (AlertObservation("wallet_balance_mismatch", True, 1),)
    )
    payload = json.loads(
        render_transition(
            transitions[0],
            checked_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        )
    )

    assert payload == {
        "active": True,
        "alert": "wallet_balance_mismatch",
        "checked_at": "2026-08-26T12:00:00+00:00",
        "observed_count": 1,
        "severity": "P1",
        "transition": "fired",
    }
    rendered = json.dumps(payload)
    for forbidden in ("postgresql://", "password", "user_id", "device_id", "session_id"):
        assert forbidden not in rendered


def test_advisory_lock_loss_skips_without_mutating_state(tmp_path: Path) -> None:
    from scripts import check_ops_alerts as alerts

    state_file = tmp_path / "ops-alerts.json"
    alerts.save_state(state_file, {"double_online": True})

    class _Cursor:
        def fetchone(self) -> tuple[bool]:
            return (False,)

    class _Connection:
        def execute(self, _query: str, _parameters: tuple[object, ...]) -> _Cursor:
            return _Cursor()

    assert alerts.run_probe(_Connection(), state_file=state_file) == ()
    assert alerts.load_state(state_file) == {"double_online": True}


def test_all_cluster_queries_execute_on_a_fresh_pg16_database(tmp_path: Path) -> None:
    if not _pg_available(_pg_dsn()):
        pytest.skip("PostgreSQL fixture not reachable")

    from alembic import command
    from alembic.config import Config

    from scripts import check_ops_alerts as alerts

    database_name = "t37_ops_alerts_test"
    admin_dsn = _pg_dsn().rsplit("/", 1)[0] + "/postgres"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{database_name}"
    server_dir = Path(__file__).resolve().parents[1]
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        dsn.replace("postgresql://", "postgresql+psycopg://"),
    )

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        command.upgrade(config, "head")
        with psycopg.connect(dsn) as conn:
            observations = alerts.collect_observations(conn)
            assert {item.name for item in observations} == set(alerts.ALERT_QUERIES)
            assert all(not item.active and item.observed_count == 0 for item in observations)
            conn.execute(
                "INSERT INTO security_auth_failures "
                "(id, dimension, identifier, request_id, occurred_at) "
                "VALUES ('old-fence-t37', 'session:fencing', 'old-digest', "
                "'old-request-t37', replace((clock_timestamp() - interval '1 hour')::text, "
                "' ', 'T'))"
            )
            conn.commit()
            expired = alerts.collect_observations(conn)
            assert (
                next(item for item in expired if item.name == "fencing_rejected").observed_count
                == 0
            )
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES "
                "('idle-cursor-t37', 'idle-cursor-t37', 'Idle cursor'), "
                "('starved-user-t37', 'starved-user-t37', 'Starved user')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) "
                "VALUES ('starved-project-t37', 'starved-user-t37', 'Starvation probe')"
            )
            conn.execute(
                "INSERT INTO user_queue_cursors "
                "(user_id, last_dispatched_at, running_tasks_count) VALUES "
                "('idle-cursor-t37', clock_timestamp() - interval '2 hours', 0), "
                "('starved-user-t37', clock_timestamp() - interval '1 hour', 0)"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, request_hash, "
                "request_snapshot_json, status) VALUES "
                "('starved-batch-t37', 'starved-project-t37', 'starved-user-t37', "
                "'starved-key-t37', 'starved-hash-t37', '{}', 'QUEUED')"
            )
            conn.execute(
                "INSERT INTO generation_tasks "
                "(id, batch_id, provider, model, status, created_at) VALUES "
                "('starved-task-t37', 'starved-batch-t37', 'fake_h3', 'MiniMax-H3', "
                "'PENDING', (clock_timestamp() - interval '1 hour')::text)"
            )
            conn.commit()
            capped = alerts.collect_observations(
                conn,
                alerts.ProbeConfig(row_limit=1, starvation_seconds=300),
            )
            assert (
                next(item for item in capped if item.name == "queue_starvation").observed_count == 1
            )
            conn.execute(
                "INSERT INTO security_auth_failures "
                "(id, dimension, identifier, request_id, occurred_at) "
                "VALUES ('fence-t37', 'session:fencing', 'digest', "
                "'request-t37', replace(clock_timestamp()::text, ' ', 'T'))"
            )
            conn.execute(
                "INSERT INTO audit_logs "
                "(id, actor_user_id, action, entity_type, entity_id, metadata_json, created_at) "
                "VALUES ('cross-user-t37', NULL, 'security.project_denied', "
                "'project', 'redacted-by-alert-query', '{}', clock_timestamp()::text)"
            )
            conn.commit()

        state_file = tmp_path / "ops-alerts.json"
        with psycopg.connect(dsn) as conn:
            emitted = alerts.run_probe(conn, state_file=state_file)
        payloads = [json.loads(line) for line in emitted]
        assert any(
            item["alert"] == "fencing_rejected" and item["transition"] == "fired"
            for item in payloads
        )
        assert any(
            item["alert"] == "cross_user_access_denied" and item["transition"] == "fired"
            for item in payloads
        )
        assert alerts.load_state(state_file)["fencing_rejected"] is True
        assert alerts.load_state(state_file)["cross_user_access_denied"] is True
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
