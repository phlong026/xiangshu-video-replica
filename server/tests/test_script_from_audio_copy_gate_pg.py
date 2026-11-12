"""爆款任务接口契约回归：正文随任务直出 + 首页时钟窗口（2026-11-11 极简化后）.

TEST-PG：复用 test_customer_pricing 的夹具链（module 级建库 → alembic head →
每用例清理）。2026-11-11 共享文案缓存退役后，「任务接口对爆款任务脱敏正文、
claim 是唯一交付口」的门禁口径整体作废：任务接口恒下发 result.text，响应不再
有 viral_source / copy_claim_required 字段，claim 端点只余 410 壳（见
test_viral_copy_simplify_pg.py）。本文件保留两类仍有效的契约：

- 首页展示下发服务端时钟与下一个窗口边界（数据时点一致性）；
- 普通任务的正文直出（上传转写不属于爆款零售链路的口径不变）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    registration_client,
    registration_dsn,
    route_state,
)

_COPY_TEXT = "任务接口里必须被门禁住的一段共享文案"


@pytest.fixture()
def gate_client(client):
    """任务路由与爆款列表路由都不在精简 app 的默认路由表里，显式挂上."""
    from app.script_from_audio_routes import router as script_router
    from app.viral_routes import router as viral_router

    client.app.include_router(viral_router)
    client.app.include_router(script_router)
    yield client
    client.app.dependency_overrides.clear()


def _seed_viral_video(raw, uid: str, *, video_id: str) -> None:
    """内容池里的一条视频 + 账号有钱：只服务首页时钟窗口用例."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES ('douyin', %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (video_id,),
    )
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )


def _seed_task(
    raw,
    uid: str,
    *,
    task_id: str,
    project_id: str,
) -> None:
    """直接落一条 SUCCEEDED 任务行：正文躺在任务里，接口是否下发由契约决定."""
    raw.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '文案门禁项目')",
        (project_id, uid),
    )
    raw.execute(
        "INSERT INTO script_from_audio_tasks ("
        "id, project_id, source_asset_id, created_by_user_id, idempotency_key, "
        "request_hash, request_json, status, result_json, completed_at, "
        "created_at, updated_at) VALUES ("
        "%s, %s, 'asset-1', %s, 'idem-1', 'hash-1', %s, 'SUCCEEDED', %s, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            task_id,
            project_id,
            uid,
            json.dumps({"source_asset_id": "asset-1"}),
            json.dumps(
                {"text": _COPY_TEXT, "duration_sec": 12.5, "language": "zh"},
                ensure_ascii=False,
            ),
        ),
    )


def test_homepage_http_returns_server_clock_and_next_window_boundary(gate_client, route_state):
    from datetime import datetime

    headers, uid = account(gate_client, "gate_home_clock")
    with psycopg.connect(route_state) as raw:
        _seed_viral_video(raw, uid, video_id="clock-video")
        raw.execute(
            "UPDATE viral_videos SET homepage_featured=1,"
            "homepage_ends_at=date_trunc('second',now()+interval '1 day')"
            "+interval '0.123450 seconds' "
            "WHERE video_id='clock-video'"
        )
        raw.execute(
            "INSERT INTO viral_media_preparations"
            " (id,platform,video_id,media_kind,status,storage_uri) "
            "VALUES('clock-media','douyin','clock-video','video','SUCCEEDED','fake://clock.mp4')"
        )
    reply = gate_client.get("/api/viral/videos?featured_only=true", headers=headers)
    assert reply.status_code == 200, reply.text
    result = reply.json()
    assert len(result["items"]) == 1
    # SQL JSON removes trailing fractional zeros; ISO timestamps permit either
    # precision. Compare timezone-aware instants, including the persisted value.
    item_end = datetime.fromisoformat(result["items"][0]["homepageEndsAt"])
    server_time = datetime.fromisoformat(result["serverTime"])
    boundary = datetime.fromisoformat(result["nextChangeAt"])
    assert "T" in result["nextChangeAt"] and "T" in result["serverTime"]
    assert item_end == boundary
    assert server_time.utcoffset() is not None and boundary.utcoffset() is not None
    with psycopg.connect(route_state) as raw:
        persisted_end, database_now = raw.execute(
            "SELECT homepage_ends_at,clock_timestamp() FROM viral_videos "
            "WHERE video_id='clock-video'"
        ).fetchone()
    assert boundary == persisted_end
    assert abs((server_time - database_now).total_seconds()) < 10
    assert server_time < boundary


def test_plain_task_still_returns_text(gate_client, route_state) -> None:
    """普通上传任务不属于爆款零售链路：正文照常随任务接口下发.

    2026-11-11 极简化后爆款任务同样直出正文，且响应里不再有
    viral_source / copy_claim_required 字段——本用例顺带固定住字段确实消失。
    """
    headers, uid = account(gate_client, "gate_plain")
    task_id = "task-gate-plain"
    project_id = "proj-gate-plain"
    with psycopg.connect(route_state) as raw:
        _seed_task(raw, uid, task_id=task_id, project_id=project_id)
        raw.commit()

    body = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert body["status"] == "SUCCEEDED"
    assert "viral_source" not in body
    assert "copy_claim_required" not in body
    assert body["result"]["text"] == _COPY_TEXT

    latest = gate_client.get(
        f"/api/projects/{project_id}/script-from-audio-tasks/latest", headers=headers
    ).json()
    assert "viral_source" not in latest
    assert "copy_claim_required" not in latest
    assert latest["result"]["text"] == _COPY_TEXT
