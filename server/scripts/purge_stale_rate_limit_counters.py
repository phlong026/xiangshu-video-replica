"""M2 review M3 / ACT-08 — sweep stale rate-limit counter rows.

Usage (server/ directory):

    uv run python -m scripts.purge_stale_rate_limit_counters \
        --database-url postgresql://USER:PASSWORD@HOST:5432/DBNAME [--dry-run]

The DSN defaults to the ``VIDEO_REPLICA_DATABASE_URL`` environment variable.
Counter rows on fully lapsed windows can never be consulted again
(``consume_rate_limit`` resets the window on the first hit after the
cutoff), so deleting them keeps the table bounded without touching any
enforcement. The append-only failure table (the actual audit trail) is
never written here. Output carries counts only. Intended to run from the
``video-replica-maintenance`` systemd timer (deploy/systemd/), daily.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

import psycopg

from app.security_rate_limit import count_stale_counters, purge_stale_counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge stale rate-limit counter rows (M2 review M3)."
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
            eligible = count_stale_counters(conn, now=now)
            print(f"stale rate-limit counter rows eligible for purge: {eligible}")
            return 0
        with conn.transaction():
            purged = purge_stale_counters(conn, now=now)
    print(f"purged {purged} stale rate-limit counter row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
