#!/usr/bin/env bash
set -euo pipefail

: "${VIDEO_REPLICA_DATABASE_URL:?VIDEO_REPLICA_DATABASE_URL is required}"
: "${VIDEO_REPLICA_CUSTOMER_PRODUCTION:?VIDEO_REPLICA_CUSTOMER_PRODUCTION=true is required}"

case "${VIDEO_REPLICA_CUSTOMER_PRODUCTION,,}" in
    1|true|yes|on) ;;
    *)
        echo "customer migration requires VIDEO_REPLICA_CUSTOMER_PRODUCTION=true" >&2
        exit 64
        ;;
esac

# This is a host-local concurrency guard. The runbook designates exactly one
# migration host, so two deploy sessions on that host cannot race Alembic.
# Do not run this script concurrently from multiple hosts.
LOCK_FILE="${VIDEO_REPLICA_MIGRATION_LOCK_FILE:-/var/lock/video-replica-pg-migrate.lock}"
cd /opt/video-replica/app/server
# Alembic reads the DSN directly, so run the same customer-production database
# gate before it can touch any target. This rejects SQLite and libpq modes that
# permit plaintext fallback before the first migration statement executes.
.venv/bin/python -c \
    'from app.db_pg import resolve_database_config, validate_customer_production; validate_customer_production(resolve_database_config())'
exec flock -n "$LOCK_FILE" .venv/bin/alembic upgrade head
