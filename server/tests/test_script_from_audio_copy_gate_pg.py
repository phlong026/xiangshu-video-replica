"""爆款文案交付门禁：任务接口不下发正文，正文只按 claim 回执交付.

TEST-PG：复用 test_customer_pricing 的夹具链（module 级建库 → alembic head →
每用例清理）。设计 §5.4-1：转写完成后正文躺在 ``script_from_audio_tasks``
里，但两个任务读取接口对爆款任务一律脱敏——交付与计费的唯一入口是
``POST /api/viral/videos/copy/claim``（首取扣费、复看免费），否则读任务
就等于绕开「获取文案」费的免费旁路。
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

_COPY_SERVICE = "viral_copy"
_COPY_TEXT = "任务接口里必须被门禁住的一段共享文案"


@pytest.fixture()
def gate_client(client):
    """任务路由与「获取文案」路由都不在精简 app 的默认路由表里，显式挂上."""
    from app.script_from_audio_routes import router as script_router
    from app.viral_routes import router as viral_router

    client.app.include_router(viral_router)
    client.app.include_router(script_router)
    yield client
    client.app.dependency_overrides.clear()


def _seed_viral_copy_video(raw, uid: str, *, video_id: str) -> None:
    """一条可获取文案的视频：内容池有行 + 账号有钱 + 共享缓存里有结果."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES ('douyin', %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (video_id,),
    )
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.execute("DELETE FROM billing_tariffs WHERE service=%s", (_COPY_SERVICE,))
    raw.execute(
        "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,2)",
        (_COPY_SERVICE,),
    )
    raw.execute(
        "INSERT INTO viral_script_cache (platform, video_id, result_json) "
        "VALUES ('douyin', %s, %s) ON CONFLICT (platform, video_id) DO UPDATE "
        "SET result_json=excluded.result_json",
        (
            video_id,
            json.dumps(
                {
                    "text": _COPY_TEXT,
                    "duration_sec": 12.0,
                    "archive": {"platform": "douyin", "video_id": video_id, "sha256": "a" * 64},
                },
                ensure_ascii=False,
            ),
        ),
    )


def _seed_task(
    raw,
    uid: str,
    *,
    task_id: str,
    project_id: str,
    video_id: str | None,
) -> None:
    """直接落一条 SUCCEEDED 任务行：转写结果在任务里，正文是否下发由接口门禁决定."""
    raw.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '文案门禁项目')",
        (project_id, uid),
    )
    request_payload: dict[str, object] = {"source_asset_id": "asset-1"}
    if video_id is not None:
        request_payload["viral_source"] = ["douyin", video_id]
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
            json.dumps(request_payload),
            json.dumps(
                {"text": _COPY_TEXT, "duration_sec": 12.5, "language": "zh"},
                ensure_ascii=False,
            ),
        ),
    )


def _wallet(raw, uid: str) -> tuple[int, int]:
    row = raw.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def test_enqueue_cache_hit_skips_asr_billing_and_gates_response(gate_client, route_state) -> None:
    """命中共享缓存的入队直接完成但分文不收（2026-10-08 拍板，翻转原 test_cw030
    「命中照收」口径）：没有真实转写发生就没有转写费——不预留、不结算、钱包不动；
    响应仍不带正文，正文与交付费由 claim 按文案生产者判定.
    """
    headers, uid = account(gate_client, "gate_enqueue")
    project_id = "proj-gate-enqueue"
    asset_id = "asset-gate-enqueue"
    video_id = "v-gate-enqueue"
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id=video_id)
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES('asr',true,2)"
        )
        raw.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, '文案门禁项目')",
            (project_id, uid),
        )
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, 'reference_video', %s, %s, "
            "1024, 'video/mp4', %s)",
            (asset_id, project_id, f"cos://bucket/{asset_id}.mp4", "a" * 64, uid),
        )
        raw.execute(
            "INSERT INTO viral_import_tasks (id, owner_user_id, project_id, source_asset_id, "
            "platform, video_id, purpose, idempotency_key, request_hash, request_json, status) "
            "VALUES ('imp-gate-enqueue', %s, %s, %s, 'douyin', %s, 'copy', 'idem-imp', "
            "'hash-imp', '{}', 'SUCCEEDED')",
            (uid, project_id, asset_id, video_id),
        )
        from app.content_store import retain_content_object
        from app.db_portable import BusinessConnection

        conn = BusinessConnection.postgres(raw)
        content, _ = retain_content_object(
            conn,
            sha256="a" * 64,
            size_bytes=1024,
            content_type="video/mp4",
            provider="cos",
            bucket="bucket",
            object_key=f"{asset_id}.mp4",
            scope="global",
            pinned=True,
        )
        raw.execute("UPDATE assets SET content_object_id=%s WHERE id=%s", (content.id, asset_id))
        raw.commit()

    created = gate_client.post(
        f"/api/projects/{project_id}/script-from-audio",
        headers=headers,
        json={"source_asset_id": asset_id, "idempotency_key": "idem-gate-enqueue"},
    )
    assert created.status_code == 202, created.text
    body = created.json()
    assert body["status"] == "SUCCEEDED"
    assert body["copy_claim_required"] is True
    assert body["viral_source"] == {"platform": "douyin", "video_id": video_id}
    assert body["result"]["text"] is None

    with psycopg.connect(route_state) as raw:
        # 没有转写发生：这条任务名下没有 asr 计费单，钱包分文未动。
        operation = raw.execute(
            "SELECT 1 FROM billing_operations WHERE service='asr' AND source_id=%s",
            (body["id"],),
        ).fetchone()
        assert operation is None
        assert _wallet(raw, uid) == (50, 0)


def test_viral_task_hides_text_until_copy_claimed(gate_client, route_state) -> None:
    """爆款任务：两个任务接口都不给正文，claim 是唯一交付口；GET 本身零计费."""
    headers, uid = account(gate_client, "gate_viral")
    task_id = "task-gate-viral"
    project_id = "proj-gate-viral"
    video_id = "v-gate-viral"
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id=video_id)
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id=video_id)
        raw.commit()

    body = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert body["status"] == "SUCCEEDED"
    assert body["copy_claim_required"] is True
    assert body["viral_source"] == {"platform": "douyin", "video_id": video_id}
    assert body["result"] is not None
    # 正文不下发，但时长/语言等非正文元数据照常回填，客户端等待态不受影响。
    assert body["result"]["text"] is None
    assert body["result"]["duration_sec"] == 12.5

    latest = gate_client.get(
        f"/api/projects/{project_id}/script-from-audio-tasks/latest", headers=headers
    ).json()
    assert latest["id"] == task_id
    assert latest["copy_claim_required"] is True
    assert latest["result"]["text"] is None

    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_content_usage_events WHERE video_id=%s AND kind='copy'",
                (video_id,),
            ).fetchone()[0]
            == 0
        )

    claim = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["text"] == _COPY_TEXT
    assert claim.json()["billing"]["charged"] == 2
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_content_usage_events WHERE video_id=%s AND kind='copy'",
                (video_id,),
            ).fetchone()[0]
            == 1
        )

    # 已购复看：任务接口依旧不下发正文（口径统一），claim 免费重放.
    replay = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert replay.json()["billing"]["deduped"] is True
    assert replay.json()["text"] == _COPY_TEXT
    again = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert again["result"]["text"] is None
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_content_usage_events WHERE video_id=%s AND kind='copy'",
                (video_id,),
            ).fetchone()[0]
            == 2
        )


def test_copy_claim_producer_free_and_others_pay_once(gate_client, route_state) -> None:
    """交付费只向「非生产者的首次提取」收（2026-10-08 拍板，翻转原「首提取者同样
    付费」口径）：生产者拿自己转写产出的文案免费；别人首提扣一笔，复看免费.
    """
    headers, uid = account(gate_client, "gate_producer")
    other_headers, other_uid = account(gate_client, "gate_producer_other")
    task_id = "task-gate-producer"
    project_id = "proj-gate-producer"
    video_id = "v-gate-producer"
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id=video_id)
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id=video_id)
        # 缓存绑定生产者：这条文案是 uid 的任务转写出来的。
        raw.execute(
            "UPDATE viral_script_cache SET producer_task_id=%s "
            "WHERE platform='douyin' AND video_id=%s",
            (task_id, video_id),
        )
        # 提取方账号同样充值，避免首提收费断言被 402 挡住。
        raw.execute(
            "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s",
            (other_uid,),
        )
        raw.commit()

    # 生产者本人：免费拿正文。
    own = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert own.status_code == 200, own.text
    assert own.json()["text"] == _COPY_TEXT
    assert own.json()["billing"]["charged"] == 0
    assert own.json()["billing"]["deduped"] is True
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert _wallet(raw, other_uid) == (50, 0)

    # 非生产者首提：扣一笔「获取文案」费。
    other = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=other_headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert other.status_code == 200, other.text
    assert other.json()["text"] == _COPY_TEXT
    assert other.json()["billing"]["charged"] == 2
    assert other.json()["billing"]["deduped"] is False

    # 非生产者复看：已购免费。
    replay = gate_client.post(
        "/api/viral/videos/copy/claim",
        headers=other_headers,
        json={"platform": "douyin", "videoId": video_id},
    )
    assert replay.json()["billing"]["charged"] == 0
    assert replay.json()["billing"]["deduped"] is True
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, other_uid) == (48, 0)


def test_homepage_http_returns_server_clock_and_next_window_boundary(gate_client, route_state):
    from datetime import datetime

    headers, uid = account(gate_client, "gate_home_clock")
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id="clock-video")
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
    """普通上传任务不属于爆款零售链路：正文照常随任务接口下发."""
    headers, uid = account(gate_client, "gate_plain")
    task_id = "task-gate-plain"
    project_id = "proj-gate-plain"
    with psycopg.connect(route_state) as raw:
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id=None)
        raw.commit()

    body = gate_client.get(f"/api/script-from-audio-tasks/{task_id}", headers=headers).json()
    assert body["status"] == "SUCCEEDED"
    assert body["copy_claim_required"] is False
    assert body["viral_source"] is None
    assert body["result"]["text"] == _COPY_TEXT

    latest = gate_client.get(
        f"/api/projects/{project_id}/script-from-audio-tasks/latest", headers=headers
    ).json()
    assert latest["result"]["text"] == _COPY_TEXT


def test_viral_task_text_is_never_leaked_by_latest_for_other_project(
    gate_client, route_state
) -> None:
    """latest 按项目隔离：别的项目查不到这条任务，也不会顺带漏出正文."""
    headers, uid = account(gate_client, "gate_latest")
    task_id = "task-gate-latest"
    project_id = "proj-gate-latest"
    other_project = "proj-gate-other"
    with psycopg.connect(route_state) as raw:
        _seed_task(raw, uid, task_id=task_id, project_id=project_id, video_id="v-gate-latest")
        raw.commit()

    assert (
        gate_client.get(
            f"/api/projects/{other_project}/script-from-audio-tasks/latest",
            headers=headers,
        ).status_code
        == 404
    )


@pytest.mark.parametrize("window", ["queued", "running", "completed"])
def test_deleted_video_retains_only_owners_accepted_task_delivery(gate_client, route_state, window):
    headers, uid = account(gate_client, "deleted_owner")
    other_headers, _ = account(gate_client, "deleted_other")
    with psycopg.connect(route_state) as raw:
        _seed_viral_copy_video(raw, uid, video_id="deleted-video")
        _seed_task(
            raw,
            uid,
            task_id="accepted-task",
            project_id="accepted-project",
            video_id="deleted-video",
        )
        # 分别重现受理、处理中、完成未领取时撤回公共内容；私人任务保留。
        if window != "completed":
            raw.execute(
                "UPDATE script_from_audio_tasks SET status=%s,result_json=NULL "
                "WHERE id='accepted-task'",
                ("PENDING" if window == "queued" else "RUNNING",),
            )
        raw.execute("UPDATE viral_videos SET deleted_at=now() WHERE video_id='deleted-video'")
    body = {"platform": "douyin", "videoId": "deleted-video", "taskId": "accepted-task"}
    if window != "completed":
        assert (
            gate_client.post("/api/viral/videos/copy/claim", headers=headers, json=body).status_code
            == 404
        )
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "UPDATE script_from_audio_tasks SET status='SUCCEEDED', result_json=%s "
                "WHERE id='accepted-task'",
                (json.dumps({"text": _COPY_TEXT, "duration_sec": 12.5}),),
            )
    result = gate_client.post("/api/viral/videos/copy/claim", headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["text"] == _COPY_TEXT
    assert result.json()["billing"]["charged"] == 0
    assert result.json()["billing"]["billable"] is False
    assert (
        gate_client.post(
            "/api/viral/videos/copy/claim", headers=other_headers, json=body
        ).status_code
        == 404
    )
    assert (
        gate_client.post(
            "/api/viral/videos/copy/claim",
            headers=headers,
            json={"platform": "douyin", "videoId": "deleted-video"},
        ).status_code
        == 404
    )
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE service='viral_copy'"
            ).fetchone()[0]
            == 0
        )
