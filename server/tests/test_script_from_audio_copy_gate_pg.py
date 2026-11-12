"""爆款任务接口契约回归：正文随任务直出 + 首页时钟窗口（2026-11-11 极简化后）.

TEST-PG：复用 test_customer_pricing 的夹具链（module 级建库 → alembic head →
每用例清理）。新转写正文随任务返回；带旧归档标记的历史任务必须有
成功 ASR 或旧交付结算才能读取正文。claim 端点退役，不撤销已购权益。

- 首页展示下发服务端时钟与下一个窗口边界；
- 普通任务正文直出，旧任务按权益返回，两个 GET 均保持项目权限隔离。

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

    新生产爆款任务同样直出正文（历史共享任务仍校验权益），且响应里不再有
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


@pytest.mark.parametrize("paid", [True, False])
def test_legacy_task_http_entitlement_and_project_isolation(gate_client, route_state, paid):
    """真实登录后的两个读取口均保护旧成果，其他账号不得借已付费任务越权。"""
    from app.db_portable import BusinessConnection
    from app.usage_billing import accept_operation, finish_source

    headers, uid = account(gate_client, "legacy_http_owner")
    other_headers, _ = account(gate_client, "legacy_http_other")
    task_id, project_id = "legacy-http-task", "legacy-http-project"
    with psycopg.connect(route_state) as raw:
        _seed_task(raw, uid, task_id=task_id, project_id=project_id)
        raw.execute(
            "UPDATE script_from_audio_tasks SET request_json=%s WHERE id=%s",
            (
                json.dumps(
                    {
                        "viral_archive_sha256": "a" * 64,
                        "viral_source": ["douyin", "legacy-http-video"],
                    }
                ),
                task_id,
            ),
        )
        if paid:
            raw.execute("UPDATE wallets SET available_credits=100 WHERE user_id=%s", (uid,))
            raw.execute(
                "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
                "VALUES ('asr',true,2,1) ON CONFLICT (service) DO UPDATE SET enabled=true"
            )
            conn = BusinessConnection.postgres(raw)
            accept_operation(conn, user_id=uid, service="asr", source_id=task_id, units=1)
            finish_source(conn, task_id, units=1, succeeded=True, service="asr")
    for path in (
        f"/api/script-from-audio-tasks/{task_id}",
        f"/api/projects/{project_id}/script-from-audio-tasks/latest",
    ):
        response = gate_client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["text"] == (_COPY_TEXT if paid else None)
        denied = gate_client.get(path, headers=other_headers)
        # 项目权限以 404 隐藏资源存在性，不改成可枚举的 403。
        assert denied.status_code == 404, denied.text
        assert denied.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
        assert _COPY_TEXT not in denied.text
