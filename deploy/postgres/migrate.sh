#!/usr/bin/env bash
set -euo pipefail

: "${VIDEO_REPLICA_DATABASE_URL:?VIDEO_REPLICA_DATABASE_URL is required}"

# This is a host-local concurrency guard. The runbook designates exactly one
# migration host, so two deploy sessions on that host cannot race Alembic.
# Do not run this script concurrently from multiple hosts.
LOCK_FILE="${VIDEO_REPLICA_MIGRATION_LOCK_FILE:-/var/lock/video-replica-pg-migrate.lock}"
cd /opt/video-replica/app/server
exec flock -n "$LOCK_FILE" .venv/bin/alembic upgrade head
