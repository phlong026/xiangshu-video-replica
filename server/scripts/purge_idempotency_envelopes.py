"""T14 / ACT-07 — purge expired customer idempotency envelopes.

Usage (server/ directory):

    uv run python -m scripts.purge_idempotency_envelopes \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME [--dry-run]

The DSN defaults to the ``VIDEO_REPLICA_DATABASE_URL`` environment variable.
Output carries counts only — no business values, credentials or envelope
contents are ever printed. Intended to run from the
``video-replica-maintenance`` systemd timer (deploy/systemd/), daily.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

import psycopg

from app.customer_idempotency import count_expired_envelopes, purge_expired_envelopes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge expired customer idempotency envelopes (T14/ACT-07)."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("VIDEO_REPLICA_DATABASE_URL", ""),
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the purge candidate count without changing anything",
    )
    args = parser.parse_args(argv)

    if not args.database_url.strip():
        print(
            "error: --database-url or VIDEO_REPLICA_DATABASE_URL is required",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(UTC)
    with psycopg.connect(args.database_url) as conn:
        if args.dry_run:
            eligible = count_expired_envelopes(conn, now=now)
            print(f"expired idempotency envelopes eligible for purge: {eligible}")
            return 0
        with conn.transaction():
            purged = purge_expired_envelopes(conn, now=now)
    print(f"purged {purged} expired idempotency envelope(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
