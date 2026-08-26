"""T37 / OPS-02 — single-owner, bounded PostgreSQL anomaly probe.

The two API processes expose process-local metrics only. Cluster-global
business invariants are checked here by one systemd timer. A PostgreSQL
transaction advisory lock makes the single-owner property fail closed even
if the timer is accidentally enabled on more than one host.

Output is transition-only JSON (``fired`` / ``resolved``). It contains fixed
alert names and counts, never row identifiers, DSNs, tokens, signed URLs, or
provider responses. An external journal/metrics collector is still required
before gray release; this script is the repository-side alert source.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import psycopg

from app.db_pg import DatabaseMode, resolve_database_config, validate_customer_production

DEFAULT_STATE_FILE = Path("/var/lib/video-replica/ops-alerts-state.json")
STATE_FILE_ENV = "VIDEO_REPLICA_OPS_ALERT_STATE_FILE"
ADVISORY_LOCK_KEY = 0x56525433374F5053  # "VRT37OPS", signed-int64 safe.


@dataclass(frozen=True)
class ProbeConfig:
    row_limit: int = 100
    session_window_seconds: int = 120
    fencing_window_seconds: int = 300
    security_window_seconds: int = 300
    starvation_seconds: int = 300
    statement_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        for name, value in (
            ("row_limit", self.row_limit),
            ("session_window_seconds", self.session_window_seconds),
            ("fencing_window_seconds", self.fencing_window_seconds),
            ("security_window_seconds", self.security_window_seconds),
            ("starvation_seconds", self.starvation_seconds),
            ("statement_timeout_ms", self.statement_timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class AlertObservation:
    name: str
    active: bool
    observed_count: int

    def __init__(self, name: str, active: bool, observed_count: int) -> None:
        if not name or observed_count < 0:
            raise ValueError("alert observation is invalid")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "observed_count", observed_count)


@dataclass(frozen=True)
class AlertTransition:
    name: str
    transition: Literal["fired", "resolved"]
    active: bool
    observed_count: int


# Each query returns a single count over a capped anomaly subquery. The
# application role needs no pg_monitor / pg_stat_* privilege. Candidate
# wallets are capped before their correlated checks. Queue cursors use an
# index-backed existence check before the cap so idle cursors cannot hide a
# truly starved user.
ALERT_QUERIES: dict[str, str] = {
    "double_online": """
        SELECT count(*) FROM (
            SELECT user_id, session_epoch
            FROM customer_session_events
            WHERE event = 'HEARTBEAT' AND created_at >= %s
            GROUP BY user_id, session_epoch
            HAVING count(DISTINCT device_id) > 1
            LIMIT %s
        ) AS anomalies
    """,
    "duplicate_charge": """
        SELECT count(*) FROM (
            SELECT recharge_order_id
            FROM wallet_transactions
            WHERE type = 'CHARGE' AND recharge_order_id IS NOT NULL
            GROUP BY recharge_order_id
            HAVING count(*) > 1
            LIMIT %s
        ) AS anomalies
    """,
    "duplicate_provider_trade_no": """
        SELECT count(*) FROM (
            SELECT provider_trade_no
            FROM recharge_orders
            WHERE provider_trade_no IS NOT NULL
            GROUP BY provider_trade_no
            HAVING count(*) > 1
            LIMIT %s
        ) AS anomalies
    """,
    "paid_without_charge": """
        SELECT count(*) FROM (
            SELECT orders.id
            FROM recharge_orders AS orders
            LEFT JOIN wallet_transactions AS charge
              ON charge.recharge_order_id = orders.id AND charge.type = 'CHARGE'
            WHERE orders.status = 'PAID' AND charge.id IS NULL
            ORDER BY orders.paid_at DESC NULLS LAST, orders.id
            LIMIT %s
        ) AS anomalies
    """,
    "charge_without_paid_source": """
        SELECT count(*) FROM (
            SELECT charge.id
            FROM wallet_transactions AS charge
            JOIN recharge_orders AS orders ON orders.id = charge.recharge_order_id
            WHERE charge.type = 'CHARGE'
              AND (
                orders.status <> 'PAID'
                OR orders.paid_at IS NULL
                OR orders.user_id <> charge.user_id
                OR orders.credits <> charge.available_delta
                OR charge.reserved_delta <> 0
              )
            ORDER BY charge.created_at, charge.id
            LIMIT %s
        ) AS anomalies
    """,
    "wallet_balance_mismatch": """
        WITH candidate_wallets AS MATERIALIZED (
            SELECT user_id, available_credits, reserved_credits
            FROM wallets
            ORDER BY updated_at DESC, user_id
            LIMIT %s
        )
        SELECT count(*) FROM (
            SELECT wallet.user_id
            FROM candidate_wallets AS wallet
            LEFT JOIN LATERAL (
                SELECT
                    COALESCE(sum(tx.available_delta), 0) AS available_total,
                    COALESCE(sum(tx.reserved_delta), 0) AS reserved_total
                FROM wallet_transactions AS tx
                WHERE tx.user_id = wallet.user_id
            ) AS ledger ON true
            WHERE wallet.available_credits <> ledger.available_total
               OR wallet.reserved_credits <> ledger.reserved_total
            LIMIT %s
        ) AS anomalies
    """,
    "fencing_rejected": """
        SELECT count(*) FROM (
            SELECT id
            FROM security_auth_failures
            WHERE dimension = 'session:fencing' AND occurred_at >= %s
            ORDER BY occurred_at DESC, id
            LIMIT %s
        ) AS anomalies
    """,
    "cross_user_access_denied": """
        SELECT count(*) FROM (
            SELECT id
            FROM audit_logs
            WHERE action IN ('security.project_denied', 'security.asset_denied')
              AND created_at >= %s
            ORDER BY created_at DESC, id
            LIMIT %s
        ) AS anomalies
    """,
    "queue_starvation": """
        WITH candidate_cursors AS MATERIALIZED (
            SELECT cursor.user_id
            FROM user_queue_cursors AS cursor
            WHERE cursor.running_tasks_count = 0
              AND EXISTS (
                SELECT 1
                FROM generation_batches AS batch
                JOIN generation_tasks AS task ON task.batch_id = batch.id
                WHERE batch.created_by_user_id = cursor.user_id
                  AND task.status IN ('PENDING', 'QUEUED')
                  AND task.created_at < %s
                LIMIT 1
            )
            ORDER BY cursor.last_dispatched_at, cursor.user_id
            LIMIT %s
        )
        SELECT count(*) FROM (
            SELECT user_id
            FROM candidate_cursors
            LIMIT %s
        ) AS anomalies
    """,
    "dangling_reserve": """
        SELECT count(*) FROM (
            SELECT reserve.id
            FROM wallet_transactions AS reserve
            JOIN generation_tasks AS task ON task.id = reserve.task_id
            WHERE reserve.type = 'RESERVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM wallet_transactions AS terminal
                  WHERE terminal.task_id = reserve.task_id
                    AND terminal.billing_round = reserve.billing_round
                    AND terminal.type IN ('SETTLE', 'RELEASE')
                  LIMIT 1
              )
              AND (
                  (task.status = 'SUCCEEDED' AND task.archive_status = 'ARCHIVED')
                  OR task.status IN ('FAILED', 'CANCELLED')
              )
            ORDER BY reserve.created_at, reserve.id
            LIMIT %s
        ) AS anomalies
    """,
}


def _server_now(conn: psycopg.Connection[object]) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL server clock is unavailable")
    value = row[0]
    current = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _count_query(
    conn: psycopg.Connection[object],
    name: str,
    query: str,
    parameters: tuple[object, ...],
) -> int:
    del name  # fixed call-site key is retained for tests/diagnostics only.
    row = conn.execute(query, parameters).fetchone()
    if row is None:
        raise RuntimeError("anomaly query returned no count")
    count = int(row[0])
    if count < 0:
        raise RuntimeError("anomaly query returned an invalid count")
    return count


def _cutoff(current: datetime, seconds: int) -> str:
    # Legacy timestamp columns are TEXT populated by PostgreSQL's
    # CURRENT_TIMESTAMP. ``str(datetime)`` keeps the same space separator so
    # the composite text indexes remain usable without a non-immutable cast.
    return str(current - timedelta(seconds=seconds))


def _security_cutoff(current: datetime, seconds: int) -> str:
    # security_auth_failures facts are written with datetime.isoformat(), so
    # TEXT comparisons must retain the same ``T`` separator.
    return (current - timedelta(seconds=seconds)).isoformat()


def collect_observations(
    conn: psycopg.Connection[object],
    config: ProbeConfig | None = None,
) -> tuple[AlertObservation, ...]:
    config = config or ProbeConfig()
    current = _server_now(conn)
    parameters: dict[str, tuple[object, ...]] = {
        "double_online": (_cutoff(current, config.session_window_seconds), config.row_limit),
        "duplicate_charge": (config.row_limit,),
        "duplicate_provider_trade_no": (config.row_limit,),
        "paid_without_charge": (config.row_limit,),
        "charge_without_paid_source": (config.row_limit,),
        "wallet_balance_mismatch": (config.row_limit, config.row_limit),
        "fencing_rejected": (
            _security_cutoff(current, config.fencing_window_seconds),
            config.row_limit,
        ),
        "cross_user_access_denied": (
            _cutoff(current, config.security_window_seconds),
            config.row_limit,
        ),
        "queue_starvation": (
            _cutoff(current, config.starvation_seconds),
            config.row_limit,
            config.row_limit,
        ),
        "dangling_reserve": (config.row_limit,),
    }
    observations: list[AlertObservation] = []
    for name, query in ALERT_QUERIES.items():
        count = _count_query(conn, name, query, parameters[name])
        observations.append(AlertObservation(name, count > 0, count))
    return tuple(observations)


def transition_alerts(
    previous: dict[str, bool],
    observations: tuple[AlertObservation, ...],
) -> tuple[tuple[AlertTransition, ...], dict[str, bool]]:
    transitions: list[AlertTransition] = []
    current: dict[str, bool] = {}
    for observation in observations:
        old_active = previous.get(observation.name, False)
        current[observation.name] = observation.active
        if observation.active and not old_active:
            transitions.append(
                AlertTransition(
                    observation.name,
                    "fired",
                    True,
                    observation.observed_count,
                )
            )
        elif old_active and not observation.active:
            transitions.append(AlertTransition(observation.name, "resolved", False, 0))
    return tuple(transitions), current


def load_state(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ops alert state file is invalid") from exc
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, bool) for key, value in payload.items()
    ):
        raise RuntimeError("ops alert state file is invalid")
    return dict(payload)


def save_state(path: Path, state: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def render_transition(transition: AlertTransition, *, checked_at: datetime) -> str:
    payload = {
        "active": transition.active,
        "alert": transition.name,
        "checked_at": checked_at.astimezone(UTC).isoformat(),
        "observed_count": transition.observed_count,
        "severity": "P1",
        "transition": transition.transition,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def run_probe(
    conn: psycopg.Connection[object],
    *,
    state_file: Path,
    config: ProbeConfig | None = None,
) -> tuple[str, ...]:
    config = config or ProbeConfig()
    lock_row = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()
    if lock_row is None or not bool(lock_row[0]):
        return ()
    conn.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{config.statement_timeout_ms}ms",),
    )
    previous = load_state(state_file)
    observations = collect_observations(conn, config)
    transitions, current = transition_alerts(previous, observations)
    checked_at = _server_now(conn)
    save_state(state_file, current)
    return tuple(render_transition(item, checked_at=checked_at) for item in transitions)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def config_from_env() -> ProbeConfig:
    return ProbeConfig(
        row_limit=_positive_int_env("VIDEO_REPLICA_OPS_ALERT_ROW_LIMIT", 100),
        session_window_seconds=_positive_int_env(
            "VIDEO_REPLICA_OPS_DOUBLE_ONLINE_WINDOW_SECONDS", 120
        ),
        fencing_window_seconds=_positive_int_env("VIDEO_REPLICA_OPS_FENCING_WINDOW_SECONDS", 300),
        security_window_seconds=_positive_int_env("VIDEO_REPLICA_OPS_SECURITY_WINDOW_SECONDS", 300),
        starvation_seconds=_positive_int_env("VIDEO_REPLICA_OPS_QUEUE_STARVATION_SECONDS", 300),
        statement_timeout_ms=_positive_int_env("VIDEO_REPLICA_OPS_QUERY_TIMEOUT_MS", 5_000),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the T37 cluster anomaly probe")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get(STATE_FILE_ENV, str(DEFAULT_STATE_FILE))),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database = resolve_database_config()
        validate_customer_production(database)
        if database.mode is not DatabaseMode.POSTGRESQL or database.dsn is None:
            raise RuntimeError("ops alert probe requires PostgreSQL")
        with psycopg.connect(database.dsn) as conn:
            lines = run_probe(conn, state_file=args.state_file, config=config_from_env())
        for line in lines:
            print(line)
        return 0
    except Exception as exc:
        # Driver messages may contain DSN fragments or business values. Emit
        # only the exception class and a fixed operation name.
        print(
            json.dumps(
                {
                    "event": "ops_alert_probe_failed",
                    "error_type": type(exc).__name__,
                    "severity": "P1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through systemd/CLI.
    raise SystemExit(main())
