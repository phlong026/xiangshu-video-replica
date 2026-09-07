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

import json
import uuid
from typing import Literal

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, http_error, write_with_idempotency
from app.db_pg import pg_transaction
from app.settings import DEFAULT_BILLING_SETTINGS, DEFAULT_RUNTIME_SETTINGS

router = APIRouter(prefix="/api/control", tags=["admin-runtime"])

RUNTIME_SETTINGS_SERVICE_UNAVAILABLE = "RUNTIME_SETTINGS_SERVICE_UNAVAILABLE"
RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE = (
    "Runtime settings writes require the PostgreSQL runtime."
)
ViralRefreshStatus = Literal["not_configured", "configured_only", "refreshing", "ok", "error"]


class QueueModeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fair_queue_enabled: bool


class QueueModeUpdateRequest(AdminWriteContract):
    """The production switch follows the shared admin write contract (PR #85
    review P2): idempotency key header, ``confirm: true`` and a non-blank
    operator reason, so audit rows name the operator's reason and ambiguous
    retries replay the snapshotted outcome instead of duplicating audits."""

    model_config = ConfigDict(extra="forbid")

    fair_queue_enabled: bool


class ViralPlatformStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    cached_videos: int
    last_fetched_at: str | None
    refresh_status: ViralRefreshStatus
    last_refresh_error: str | None


class ViralRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_enabled: bool
    import_enabled: bool
    pending_imports: int
    running_imports: int
    failed_imports: int
    pending_refreshes: int
    running_refreshes: int
    failed_refreshes: int
    source_configured: bool
    platforms: list[ViralPlatformStatus]


class ViralRuntimeUpdateRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    collection_enabled: bool
    import_enabled: bool


class ViralAvailabilityUpdateRequest(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")

    status: Literal["AVAILABLE", "HIDDEN", "UNAVAILABLE"]


class ViralAvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    video_id: str
    status: Literal["AVAILABLE", "HIDDEN", "UNAVAILABLE"]


def _viral_runtime_response(conn: psycopg.Connection) -> ViralRuntimeResponse:
    controls = conn.execute(
        "SELECT collection_enabled, import_enabled FROM viral_runtime_controls WHERE id = 1"
    ).fetchone()
    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT status, count(*) FROM viral_import_tasks GROUP BY status"
        ).fetchall()
    }
    refresh_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT status, count(*) FROM viral_refresh_tasks GROUP BY status"
        ).fetchall()
    }
    source_configured = (
        conn.execute("SELECT 1 FROM provider_settings WHERE provider = 'tikhub'").fetchone()
        is not None
    )
    platforms: list[ViralPlatformStatus] = []
    for platform in ("douyin", "wechat_channels"):
        cached = conn.execute(
            "SELECT count(*) FROM viral_videos WHERE platform = %s", (platform,)
        ).fetchone()
        fetched = conn.execute(
            "SELECT max(fetched_at) FROM viral_fetch_state WHERE platform = %s",
            (platform,),
        ).fetchone()
        refresh = conn.execute(
            """
            SELECT status, error_message_redacted FROM viral_refresh_tasks
            WHERE platform = %s ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (platform,),
        ).fetchone()
        last_fetched_at = (
            str(fetched[0]) if fetched is not None and fetched[0] is not None else None
        )
        refresh_status: ViralRefreshStatus
        if refresh is not None and str(refresh[0]) in {"PENDING", "RUNNING"}:
            refresh_status = "refreshing"
        elif refresh is not None and str(refresh[0]) == "FAILED":
            refresh_status = "error"
        elif last_fetched_at is not None:
            refresh_status = "ok"
        elif source_configured:
            refresh_status = "configured_only"
        else:
            refresh_status = "not_configured"
        platforms.append(
            ViralPlatformStatus(
                platform=platform,
                cached_videos=int(cached[0]) if cached is not None else 0,
                last_fetched_at=last_fetched_at,
                refresh_status=refresh_status,
                last_refresh_error=(
                    str(refresh[1]) if refresh is not None and refresh[1] is not None else None
                ),
            )
        )
    return ViralRuntimeResponse(
        collection_enabled=bool(controls[0]) if controls is not None else False,
        import_enabled=bool(controls[1]) if controls is not None else False,
        pending_imports=counts.get("PENDING", 0),
        running_imports=counts.get("RUNNING", 0),
        failed_imports=counts.get("FAILED", 0),
        pending_refreshes=refresh_counts.get("PENDING", 0),
        running_refreshes=refresh_counts.get("RUNNING", 0),
        failed_refreshes=refresh_counts.get("FAILED", 0),
        source_configured=source_configured,
        platforms=platforms,
    )


@router.get("/settings/viral", response_model=ViralRuntimeResponse)
def read_viral_runtime(_actor: AdminReader) -> ViralRuntimeResponse:
    with pg_transaction() as conn:
        return _viral_runtime_response(conn)


@router.patch("/settings/viral", response_model=ViralRuntimeResponse)
def update_viral_runtime(
    payload: ViralRuntimeUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        updated = conn.execute(
            """
            UPDATE viral_runtime_controls
            SET collection_enabled = %s, import_enabled = %s,
                updated_by_user_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (int(payload.collection_enabled), int(payload.import_enabled), actor.user_id),
        )
        if updated.rowcount != 1:
            conn.execute(
                """
                INSERT INTO viral_runtime_controls (
                    id, collection_enabled, import_enabled, updated_by_user_id
                ) VALUES (1, %s, %s, %s)
                """,
                (int(payload.collection_enabled), int(payload.import_enabled), actor.user_id),
            )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'viral_runtime.update', 'viral_runtime_controls', '1', %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "collection_enabled": payload.collection_enabled,
                        "import_enabled": payload.import_enabled,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return _viral_runtime_response(conn).model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )


@router.patch(
    "/viral/videos/{platform}/{video_id:path}/availability",
    response_model=ViralAvailabilityResponse,
)
def update_viral_video_availability(
    platform: Literal["douyin", "wechat_channels"],
    video_id: str,
    payload: ViralAvailabilityUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        video = conn.execute(
            "SELECT 1 FROM viral_videos WHERE platform = %s AND video_id = %s",
            (platform, video_id),
        ).fetchone()
        if video is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "Viral video was not found.")
        conn.execute(
            """
            INSERT INTO viral_video_visibility (
                platform, video_id, status, reason, updated_by_user_id, updated_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (platform, video_id) DO UPDATE SET
                status = excluded.status,
                reason = excluded.reason,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (platform, video_id, payload.status, payload.reason.strip(), actor.user_id),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'viral_video.availability_update', 'viral_video', %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps(
                    {
                        "platform": platform,
                        "video_id": video_id,
                        "status": payload.status,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        return ViralAvailabilityResponse(
            platform=platform, video_id=video_id, status=payload.status
        ).model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )


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
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Flip the fair-queue rollout switch as an audited, idempotent admin
    write behind the shared write contract (PR #85 review P2).

    Uses the runtime_settings row's queue-mode column directly (not the
    internal ``save_runtime_settings`` limits upsert): the switch-only write
    must not require restating unrelated runtime limits, and the limits
    themselves stay on the internal settings lane. When the settings row does
    not exist yet (a fresh database whose limits were never configured), the
    documented defaults seed it so the switch always lands on a real row.
    """

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
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
                json.dumps(
                    {
                        "fair_queue_enabled": payload.fair_queue_enabled,
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                        "setting": "queue_mode",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
        return QueueModeResponse(
            fair_queue_enabled=bool(row[0]) if row is not None else False
        ).model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE,
        unavailable_message=RUNTIME_SETTINGS_SERVICE_UNAVAILABLE_MESSAGE,
    )
