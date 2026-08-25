"""The fair-queue rollout switch on the production control plane (M4/M5
review M2 follow-up, PR #68 Codex P1).

The internal ``PATCH /api/admin/settings/runtime`` route authenticates with
the internal Bearer lane (``SettingsAdmin`` → ``AuthenticatedUser``), which
customer-production operators can never present: they authenticate through
per-operator admin sessions (the admin cookie + CSRF flow of
``admin_auth_routes``). Without this router, a normally authenticated
administrator would answer 401 on the switch and the audited enable path
would not exist in production — leaving ad-hoc SQL as the only option,
exactly what M2 set out to close.

Contract mirrors the other admin routes (T12 precedent): ``AdminReader``
reads, ``AdminWriter`` writes (admin role, CSRF header enforced by
``get_admin_actor`` on write methods), every write lands an audit_logs row
with the real actor, and the SQLite lane is not applicable — admin sessions
themselves require the PostgreSQL runtime and fail closed elsewhere.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.admin_auth_routes import AdminReader, AdminWriter
from app.db_pg import pg_transaction
from app.settings import DEFAULT_BILLING_SETTINGS, DEFAULT_RUNTIME_SETTINGS

router = APIRouter(prefix="/api/control", tags=["admin-runtime"])


class QueueModeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fair_queue_enabled: bool


class QueueModeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fair_queue_enabled: bool


@router.get("/settings/queue-mode", response_model=QueueModeResponse)
def read_queue_mode(_actor: AdminReader) -> QueueModeResponse:
    """The current queue mode for the admin UI (revised ADR §4)."""
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
    return QueueModeResponse(fair_queue_enabled=bool(row[0]) if row is not None else False)


@router.patch("/settings/queue-mode", response_model=QueueModeResponse)
def update_queue_mode(
    payload: QueueModeUpdateRequest,
    actor: AdminWriter,
) -> QueueModeResponse:
    """Flip the fair-queue rollout switch as an audited admin write.

    Uses the runtime_settings row's queue-mode column directly (not the
    internal ``save_runtime_settings`` limits upsert): the switch-only write
    must not require restating unrelated runtime limits, and the limits
    themselves stay on the internal settings lane. When the settings row does
    not exist yet (a fresh database whose limits were never configured), the
    documented defaults seed it so the switch always lands on a real row.
    """
    with pg_transaction() as conn:
        updated = conn.execute(
            """
            UPDATE runtime_settings
            SET fair_queue_enabled = %s,
                updated_by_user_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (payload.fair_queue_enabled, actor.user_id),
        )
        if updated.rowcount == 0:
            conn.execute(
                """
                INSERT INTO runtime_settings (
                    id, max_generation_count_per_batch, max_concurrent_h3_tasks,
                    internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen,
                    active_storage_provider, fair_queue_enabled, updated_by_user_id,
                    created_at, updated_at
                ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    DEFAULT_RUNTIME_SETTINGS["max_generation_count_per_batch"],
                    DEFAULT_RUNTIME_SETTINGS["max_concurrent_h3_tasks"],
                    DEFAULT_BILLING_SETTINGS["internal_base_unit_price_fen"],
                    DEFAULT_BILLING_SETTINGS["min_recharge_fen"],
                    DEFAULT_BILLING_SETTINGS["recharge_step_fen"],
                    DEFAULT_RUNTIME_SETTINGS["active_storage_provider"],
                    payload.fair_queue_enabled,
                    actor.user_id,
                ),
            )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'runtime_settings.update', 'runtime_settings', '1', %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                '{"fair_queue_enabled": %s, "setting": "queue_mode"}'
                % ("true" if payload.fair_queue_enabled else "false"),
            ),
        )
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
    return QueueModeResponse(fair_queue_enabled=bool(row[0]) if row is not None else False)
