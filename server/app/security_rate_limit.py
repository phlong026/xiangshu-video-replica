"""T15 / ACT-08 — the shared security rate limiter and failure auditor.

Every API instance behind the load balancer draws from one PostgreSQL
budget: the fixed-window counter state lives in
``security_rate_limit_counters`` (revision 032) and is mutated by a single
atomic UPSERT, so concurrent requests — including requests landing on
different instances — serialize on the bucket's primary key and observe
the same remaining budget (ACT-08 red line: in-process rate limiting is
not a multi-API answer; Redis and message-queue frameworks are banned by
the repository's architecture red lines, PostgreSQL is the shared truth).

Dimensions (frozen vocabulary, CHECK-constrained in 032):

- ``activate:ip``    — client address redeeming activation codes;
- ``activate:code``  — keyed digest of the normalized activation code
  (never the plaintext: a hammering attacker must burn the code budget
  without the failure audit ever storing the code itself);
- ``login:ip`` / ``login:account`` — reserved for the T19 login lane so
  the same engine, tables and thresholds carry over unchanged.

Failure auditing: every code-side rejection is appended to
``security_auth_failures`` (append-only trigger) with the dimension, the
identifier (digest for the code dimension), the request id and the server
clock. ``failure_metrics`` aggregates a trailing window and
``failure_alert_active`` compares it against the operator threshold —
the hook the T37 / OPS-02 alerting pipeline consumes.

Anti-enumeration: ``apply_anti_enumeration_delay`` burns a constant
PBKDF2 cost on the unified rejection path so unknown, malformed,
expired, suspended, revoked and already-active codes share one latency
profile (the response body is already the single 400
``ACTIVATION_UNAVAILABLE`` from T13; this closes the timing channel).

Clock: windows and metrics are decided on the server clock — the caller
may inject ``now`` only from tests; production paths read
``SELECT now()`` from the same PostgreSQL the counter lives in.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

logger = logging.getLogger(__name__)

COUNTERS_TABLE = "security_rate_limit_counters"
FAILURES_TABLE = "security_auth_failures"

DIMENSION_ACTIVATE_IP = "activate:ip"
DIMENSION_ACTIVATE_CODE = "activate:code"
DIMENSION_LOGIN_IP = "login:ip"
DIMENSION_LOGIN_ACCOUNT = "login:account"
DIMENSIONS = (
    DIMENSION_ACTIVATE_IP,
    DIMENSION_ACTIVATE_CODE,
    DIMENSION_LOGIN_IP,
    DIMENSION_LOGIN_ACCOUNT,
)

RATE_LIMIT_ACTIVATE_IP_ENV = "VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP"
RATE_LIMIT_ACTIVATE_CODE_ENV = "VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE"
RATE_LIMIT_WINDOW_ENV = "VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS"
RATE_LIMIT_FAILURE_ALERT_ENV = "VIDEO_REPLICA_RATE_LIMIT_FAILURE_ALERT_THRESHOLD"

DEFAULT_ACTIVATE_IP_LIMIT = 10
DEFAULT_ACTIVATE_CODE_LIMIT = 5
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_FAILURE_ALERT_THRESHOLD = 20

# Constant anti-enumeration cost: ~120k PBKDF2-SHA256 iterations land in
# the tens of milliseconds on commodity hardware — enough to dominate the
# sub-millisecond timing differences between the rejection sub-paths,
# cheap enough not to matter for a legitimate typo-retry.
ANTI_ENUMERATION_PBKDF2_ITERATIONS = 120_000
_ANTI_ENUMERATION_SALT = b"video-replica:anti-enumeration"
_ANTI_ENUMERATION_PAD = b"video-replica:constant-cost"


@dataclass(frozen=True)
class RateLimitDecision:
    """The verdict for one consumption attempt on one bucket."""

    allowed: bool
    retry_after_seconds: int
    hit_count: int


# ---------------------------------------------------------------------------
# Configuration (env overrides with safe fallbacks)
# ---------------------------------------------------------------------------


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def activation_ip_limit() -> int:
    return _positive_int_env(RATE_LIMIT_ACTIVATE_IP_ENV, DEFAULT_ACTIVATE_IP_LIMIT)


def activation_code_limit() -> int:
    return _positive_int_env(RATE_LIMIT_ACTIVATE_CODE_ENV, DEFAULT_ACTIVATE_CODE_LIMIT)


def rate_limit_window_seconds() -> int:
    return _positive_int_env(RATE_LIMIT_WINDOW_ENV, DEFAULT_WINDOW_SECONDS)


def failure_alert_threshold() -> int:
    return _positive_int_env(RATE_LIMIT_FAILURE_ALERT_ENV, DEFAULT_FAILURE_ALERT_THRESHOLD)


# ---------------------------------------------------------------------------
# Bucket identity
# ---------------------------------------------------------------------------


def bucket_key(dimension: str, identifier: str) -> str:
    """The fixed-window bucket identity two API instances must agree on."""
    return f"{dimension}|{identifier}"


# ---------------------------------------------------------------------------
# Shared fixed-window consumption
# ---------------------------------------------------------------------------


def _server_now(conn: psycopg.Connection) -> datetime:
    row = conn.execute("SELECT now()").fetchone()
    if row is None or not isinstance(row[0], datetime):
        raise RuntimeError("the PostgreSQL server clock is unavailable")
    now = row[0]
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now


def consume_rate_limit(
    conn: psycopg.Connection,
    *,
    dimension: str,
    identifier: str,
    limit: int,
    window_seconds: int,
    now: datetime | None = None,
) -> RateLimitDecision:
    """Atomically spend one hit of ``dimension``/``identifier``'s budget.

    One UPSERT decides everything: a fresh bucket starts at 1, a bucket
    whose window has lapsed restarts at 1, a live bucket increments. Two
    API instances racing on the same bucket serialize on the primary key,
    so the shared budget can never be exceeded by landing on different
    processes (the ACT-08 red line).
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown rate-limit dimension {dimension!r}")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")

    current = now if now is not None else _server_now(conn)
    current = current.astimezone(UTC).replace(microsecond=0)
    window_start = current.isoformat()
    # A stored window_start at or before this cutoff has lapsed.
    cutoff = (current - timedelta(seconds=window_seconds)).isoformat()
    updated_at = current.isoformat()

    row = conn.execute(
        f"INSERT INTO {COUNTERS_TABLE} (bucket_key, window_start, hit_count, updated_at) "
        "VALUES (%s, %s, 1, %s) "
        "ON CONFLICT (bucket_key) DO UPDATE SET "
        f"  hit_count = CASE WHEN {COUNTERS_TABLE}.window_start <= %s THEN 1 "
        f"    ELSE {COUNTERS_TABLE}.hit_count + 1 END, "
        f"  window_start = CASE WHEN {COUNTERS_TABLE}.window_start <= %s "
        "    THEN EXCLUDED.window_start "
        f"    ELSE {COUNTERS_TABLE}.window_start END, "
        "  updated_at = EXCLUDED.updated_at "
        "RETURNING hit_count, window_start",
        (
            bucket_key(dimension, identifier),
            window_start,
            updated_at,
            cutoff,
            cutoff,
        ),
    ).fetchone()
    assert row is not None  # RETURNING always yields exactly one row here
    hit_count = int(row[0])
    effective_window_start = datetime.fromisoformat(str(row[1]))
    if effective_window_start.tzinfo is None:
        effective_window_start = effective_window_start.replace(tzinfo=UTC)

    allowed = hit_count <= limit
    retry_after_seconds = 0
    if not allowed:
        window_end = effective_window_start + timedelta(seconds=window_seconds)
        remaining = (window_end - current).total_seconds()
        retry_after_seconds = max(1, math.ceil(remaining))
    return RateLimitDecision(
        allowed=allowed,
        retry_after_seconds=retry_after_seconds,
        hit_count=hit_count,
    )


# ---------------------------------------------------------------------------
# Failure auditing (append-only events, metrics, alert threshold)
# ---------------------------------------------------------------------------


def record_auth_failure(
    conn: psycopg.Connection,
    *,
    dimension: str,
    identifier: str,
    request_id: str | None,
    now: datetime | None = None,
) -> None:
    """Append one code-side rejection event for metrics and alerting.

    The identifier for the code dimension is the keyed digest computed by
    the caller — the plaintext activation code never reaches this table.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown failure dimension {dimension!r}")
    current = now if now is not None else _server_now(conn)
    current = current.astimezone(UTC).replace(microsecond=0)
    conn.execute(
        f"INSERT INTO {FAILURES_TABLE} (id, dimension, identifier, request_id, occurred_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), dimension, identifier, request_id, current.isoformat()),
    )


def failure_metrics(
    conn: psycopg.Connection,
    *,
    dimension: str,
    window_seconds: int,
    now: datetime | None = None,
) -> int:
    """Count the dimension's failure events inside the trailing window."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown failure dimension {dimension!r}")
    current = now if now is not None else _server_now(conn)
    current = current.astimezone(UTC).replace(microsecond=0)
    since = (current - timedelta(seconds=window_seconds)).isoformat()
    row = conn.execute(
        f"SELECT count(*) FROM {FAILURES_TABLE} WHERE dimension = %s AND occurred_at > %s",
        (dimension, since),
    ).fetchone()
    assert row is not None
    return int(row[0])


def failure_alert_active(
    conn: psycopg.Connection,
    *,
    dimension: str,
    window_seconds: int,
    threshold: int,
    now: datetime | None = None,
) -> bool:
    """Whether the trailing-window failures crossed the operator threshold."""
    return (
        failure_metrics(conn, dimension=dimension, window_seconds=window_seconds, now=now)
        >= threshold
    )


# ---------------------------------------------------------------------------
# Anti-enumeration constant cost
# ---------------------------------------------------------------------------


def apply_anti_enumeration_delay() -> None:
    """Burn a constant cost so rejection sub-paths share one latency profile.

    T13 already unifies every code-side rejection into the single 400
    ``ACTIVATION_UNAVAILABLE``; the remaining side channel is timing (an
    unknown code skips the row lock an expired one takes). A fixed PBKDF2
    digest dominates those sub-millisecond differences. Legitimate typo
    retries pay it too — tens of milliseconds, once per rejected attempt.
    """
    hashlib.pbkdf2_hmac(
        "sha256",
        _ANTI_ENUMERATION_PAD,
        _ANTI_ENUMERATION_SALT,
        ANTI_ENUMERATION_PBKDF2_ITERATIONS,
    )
