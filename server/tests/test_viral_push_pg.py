"""爆款视频「推送到前端」（人工策展发布）TEST-PG 矩阵.

覆盖四条不变量：
- 端点：curation:batch ``action=push``——就绪条目当场上架（collection_published=1
  + pushed_at + 审计），未就绪条目排队 ``archive_batch`` 任务并携带 publish_after。
- worker：publish_after 意图在素材就绪后自动上架；守卫未过（下架）不上架。
- 可见口径：pushed_at 豁免「发布时间 7 天窗口」——老视频人工上架后客户可见。
- 任务门禁：定时采集开关关闭只停关键词采集，人工任务照常认领。

复用 T34 夹具链（模块级建库 → alembic head → 每用例 TRUNCATE 隔离），
管理端走真实会话 cookie + CSRF（T12 模式），不入队即不发外呼。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip  # noqa: F401
from test_admin_session_routes import (  # noqa: F401
    _admin_session,
    admin_app,
    client,
    route_state,
    sessions_dsn,
)

from app.db_portable import BusinessConnection
from app.storage import FakeStorageAdapter
from app.viral_push import (
    mark_viral_push_published,
    viral_meta_snapshot_key,
    write_viral_metadata_snapshot,
)
from app.viral_refresh import ViralRefreshLease, acquire_viral_refresh_task
from app.viral_tikhub import ViralVideo

PUSH_PATH = "/api/control/viral/videos/curation:batch"
VIDEO_ID = "admin-video/opaque=id"


def _future_lock() -> str:
    return (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_ready_media(raw: psycopg.Connection, *, video_id: str = VIDEO_ID) -> None:
    raw.execute(
        "INSERT INTO viral_media_preparations"
        "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
        "(%s,'douyin',%s,'video','SUCCEEDED','fake://test/video.mp4')",
        (f"push-media-{video_id}", video_id),
    )


def _reset_video(raw: psycopg.Connection) -> None:
    raw.execute(
        "UPDATE viral_videos SET collection_published=0, pushed_at=NULL, "
        "cover_url='https://cdn.example.com/cover.jpg', cover_key='viral/cover/douyin/ready' "
        "WHERE video_id=%s",
        (VIDEO_ID,),
    )


def _push(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    response = client.post(
        PUSH_PATH,
        headers={**headers, "Idempotency-Key": "push-test-key"},
        json={
            "action": "push",
            "reason": "推送测试",
            "confirm": True,
            "items": [{"platform": "douyin", "video_id": VIDEO_ID}],
        },
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_pushed_at_column_exists_at_head(route_state: str) -> None:
    require_pg_or_explicit_skip()
    with psycopg.connect(route_state) as raw:
        columns = {
            row[0]
            for row in raw.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='viral_videos'"
            ).fetchall()
        }
    assert "pushed_at" in columns


def test_push_ready_video_publishes_immediately(client: TestClient, route_state: str) -> None:
    require_pg_or_explicit_skip()
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        _seed_ready_media(raw)

    result = _push(client, headers)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["published"] is True and item["queued_for_preparation"] is False
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT collection_published, pushed_at IS NOT NULL FROM viral_videos "
            "WHERE video_id=%s",
            (VIDEO_ID,),
        ).fetchone()
        assert (row[0], row[1]) == (1, True)
        audit = raw.execute(
            "SELECT count(*) FROM audit_logs WHERE action='viral_video.push' AND entity_id=%s",
            (f"douyin:{VIDEO_ID}",),
        ).fetchone()[0]
        assert audit == 1
        # 幂等：素材就绪时重复推送不产生新任务，也不重复计时。
        again = _push(client, headers)
        assert again["items"][0]["published"] is True
        tasks = raw.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0]
        assert tasks == 0


def test_push_unready_video_enqueues_publish_after_and_worker_publishes(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_pg_or_explicit_skip()
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        raw.execute("UPDATE viral_videos SET cover_key=NULL WHERE video_id=%s", (VIDEO_ID,))

    result = _push(client, headers)

    item = result["items"][0]
    assert item["published"] is False and item["queued_for_preparation"] is True
    task_id = item["task_id"]
    assert task_id
    with psycopg.connect(route_state) as raw:
        config = raw.execute(
            "SELECT collection_config_json FROM viral_refresh_tasks WHERE id=%s", (task_id,)
        ).fetchone()[0]
        assert json.loads(config)["publish_after"]["actor_user_id"] == "admin_u"
        published = raw.execute(
            "SELECT collection_published FROM viral_videos WHERE video_id=%s", (VIDEO_ID,)
        ).fetchone()[0]
        assert published == 0

    # worker 消费：转存本身打桩（外呼不在本用例范围），但转存成功的两个落库
    # 效果（媒体记录 SUCCEEDED + 封面回填）必须模拟，publish_after 守卫靠它放行。
    from app import viral_collection

    def fake_archive(lease: object, storage: object, video_id: str) -> None:
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "DELETE FROM viral_media_preparations WHERE platform='douyin' AND video_id=%s",
                (video_id,),
            )
            raw.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
                "(%s,'douyin',%s,'video','SUCCEEDED','fake://test/video.mp4')",
                (f"push-media-{video_id}", video_id),
            )
            raw.execute(
                "UPDATE viral_videos SET cover_key='viral/cover/douyin/ready' WHERE video_id=%s",
                (video_id,),
            )

    monkeypatch.setattr(viral_collection, "_run_single_archive", fake_archive)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET status='RUNNING', attempt=1, "
            "locked_by='test-worker', locked_until=%s, started_at=%s WHERE id=%s",
            (
                _future_lock(),
                datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                task_id,
            ),
        )
    lease = ViralRefreshLease(
        id=str(task_id), worker_id="test-worker", platform="douyin", sort="latest", attempt=1
    )
    viral_collection.run_viral_collection(lease, FakeStorageAdapter(provider="fake", bucket="t"))
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT collection_published, pushed_at IS NOT NULL FROM viral_videos "
            "WHERE video_id=%s",
            (VIDEO_ID,),
        ).fetchone()
        assert (row[0], row[1]) == (1, True)


def test_single_archive_publish_after_auto_publishes_and_tops_homepage(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """链接采集的 ``single_archive`` + publish_after：转存完成即自动上架并置顶首页.

    管理端「爆款视频获取」粘贴链接后走的就是这条链路：任务配置带
    ``publish_after`` 意图，worker 转存成功后调用 auto_publish_viral_video
    ——列表上架（pushed_at）+ 首页推荐位置顶（homepage_featured/rank），
    审计落在真实操作人头上。
    """
    require_pg_or_explicit_skip()
    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        raw.execute("UPDATE viral_videos SET cover_key=NULL WHERE video_id=%s", (VIDEO_ID,))
        raw.execute("DELETE FROM viral_refresh_tasks")
        raw.execute(
            "INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json) "
            "VALUES('task-single-archive','douyin','latest',%s)",
            (
                json.dumps(
                    {
                        "kind": "single_archive",
                        "video_id": VIDEO_ID,
                        "publish_after": {
                            "actor_user_id": "admin_u",
                            "reason": "采集后自动上架",
                            "request_id": "link-import-req-1",
                        },
                    }
                ),
            ),
        )
        raw.execute(
            "UPDATE viral_refresh_tasks SET status='RUNNING', attempt=1, "
            "locked_by='test-worker', locked_until=%s, started_at=%s "
            "WHERE id='task-single-archive'",
            (
                _future_lock(),
                datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    from app import viral_collection

    def fake_archive(lease: object, storage: object, video_id: str) -> None:
        # 转存成功的两个落库效果（publish_after 守卫靠它放行）。
        with psycopg.connect(route_state) as raw:
            raw.execute(
                "DELETE FROM viral_media_preparations WHERE platform='douyin' AND video_id=%s",
                (video_id,),
            )
            raw.execute(
                "INSERT INTO viral_media_preparations"
                "(id,platform,video_id,media_kind,status,storage_uri) VALUES"
                "(%s,'douyin',%s,'video','SUCCEEDED','fake://test/video.mp4')",
                (f"push-media-{video_id}", video_id),
            )
            raw.execute(
                "UPDATE viral_videos SET cover_key='viral/cover/douyin/ready' WHERE video_id=%s",
                (video_id,),
            )

    monkeypatch.setattr(viral_collection, "_run_single_archive", fake_archive)
    lease = ViralRefreshLease(
        id="task-single-archive",
        worker_id="test-worker",
        platform="douyin",
        sort="latest",
        attempt=1,
    )
    viral_collection.run_viral_collection(lease, FakeStorageAdapter(provider="fake", bucket="t"))
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT collection_published, pushed_at IS NOT NULL, homepage_featured, "
            "homepage_rank FROM viral_videos WHERE video_id=%s",
            (VIDEO_ID,),
        ).fetchone()
        assert (row[0], row[1]) == (1, True)
        assert row[2] == 1 and row[3] is not None
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='viral_video.push' AND entity_id=%s",
                (f"douyin:{VIDEO_ID}",),
            ).fetchone()[0]
            == 1
        )


def test_auto_publish_homepage_capacity_evicts_oldest(route_state: str) -> None:
    """首页容量（viral_runtime_controls.homepage_capacity）：置顶新条目时自动
    撤出最旧的推荐位（条目仍在客户列表，只是不再占首页）."""
    require_pg_or_explicit_skip()
    from app.viral_push import auto_publish_viral_video
    from app.viral_store import upsert_viral_videos

    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        _seed_ready_media(raw)
        raw.execute("UPDATE viral_runtime_controls SET homepage_capacity=1 WHERE id=1")
        conn = BusinessConnection.postgres(raw)
        upsert_viral_videos(
            conn,
            [
                ViralVideo(
                    platform="douyin",
                    video_id="capacity-old",
                    category="",
                    title="更早的推荐视频",
                    author="旧作者",
                    author_avatar=None,
                    verified=False,
                    cover_url=None,
                    duration_ms=9000,
                    likes=None,
                    comments=None,
                    shares=None,
                    collects=None,
                    published_at=None,
                    published_display=None,
                    like_display=None,
                    native={},
                )
            ],
            commit=False,
        )
        raw.execute(
            "UPDATE viral_videos SET homepage_featured=1, homepage_rank=5, pushed_at=now() "
            "WHERE video_id='capacity-old'"
        )
        published = auto_publish_viral_video(
            BusinessConnection.postgres(raw),
            platform="douyin",
            video_id=VIDEO_ID,
            actor_user_id="admin_u",
        )
        assert published is True
    with psycopg.connect(route_state) as raw:
        new_row = raw.execute(
            "SELECT homepage_featured, homepage_rank FROM viral_videos WHERE video_id=%s",
            (VIDEO_ID,),
        ).fetchone()
        assert new_row[0] == 1 and new_row[1] is not None and new_row[1] < 5
        old_row = raw.execute(
            "SELECT homepage_featured, homepage_rank FROM viral_videos WHERE video_id='capacity-old'"
        ).fetchone()
        assert (old_row[0], old_row[1]) == (0, None)


def test_pushed_video_visible_beyond_publish_window(route_state: str) -> None:
    require_pg_or_explicit_skip()
    from app.viral_content_state import customer_visible_sql
    from app.viral_store import list_viral_video_page

    old_published_at = int(datetime.now(UTC).timestamp()) - 30 * 86400
    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        raw.execute(
            "UPDATE viral_videos SET collection_published=1, published_at=%s, pushed_at=NULL "
            "WHERE video_id=%s",
            (old_published_at, VIDEO_ID),
        )
        _seed_ready_media(raw)
        outside_window = raw.execute(
            f"SELECT {customer_visible_sql()} FROM viral_videos v WHERE v.video_id=%s",
            (VIDEO_ID,),
        ).fetchone()[0]
        assert outside_window is False

        raw.execute(
            "UPDATE viral_videos SET pushed_at=%s WHERE video_id=%s",
            (datetime.now(UTC).isoformat(), VIDEO_ID),
        )
        visible = raw.execute(
            f"SELECT {customer_visible_sql()} FROM viral_videos v WHERE v.video_id=%s",
            (VIDEO_ID,),
        ).fetchone()[0]
        assert visible is True

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        page = list_viral_video_page(conn, platform="douyin", sort="hot", limit=10)
    assert [video.video_id for video in page.items] == [VIDEO_ID]


def test_acquire_gate_runs_manual_tasks_while_collection_paused(route_state: str) -> None:
    require_pg_or_explicit_skip()
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE viral_runtime_controls SET collection_enabled=0 WHERE id=1")
        raw.execute(
            "INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json) "
            "VALUES('push-task-manual','douyin','latest',%s)",
            (json.dumps({"kind": "single_archive", "video_id": VIDEO_ID}),),
        )

        lease = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="test-worker"
        )
        assert lease is not None and lease.id == "push-task-manual"

        # 人工任务被消费后，队列里只剩关键词采集任务：开关关闭时不得认领。
        raw.execute("UPDATE viral_refresh_tasks SET status='SUCCEEDED' WHERE id='push-task-manual'")
        raw.execute(
            "INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json) "
            "VALUES('push-task-keyword','douyin','hot',%s)",
            (json.dumps({"keywords": [{"keyword": "乡墅", "platform": "douyin"}]}),),
        )
        assert (
            acquire_viral_refresh_task(BusinessConnection.postgres(raw), worker_id="test-worker")
            is None
        )


def test_mark_viral_push_published_refuses_hidden_video(route_state: str) -> None:
    require_pg_or_explicit_skip()
    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        _seed_ready_media(raw)
        raw.execute(
            "INSERT INTO viral_video_visibility(platform,video_id,status,reason) "
            "VALUES('douyin',%s,'HIDDEN','测试下架')",
            (VIDEO_ID,),
        )
        applied = mark_viral_push_published(
            BusinessConnection.postgres(raw), platform="douyin", video_id=VIDEO_ID
        )
        assert applied is False
        row = raw.execute(
            "SELECT collection_published, pushed_at FROM viral_videos WHERE video_id=%s",
            (VIDEO_ID,),
        ).fetchone()
        assert (row[0], row[1]) == (0, None)


def test_push_rejects_link_imported_video(client: TestClient, route_state: str) -> None:
    """链接导入素材不得推送到前端：与「上首页」同一理由——那是单个客户的自有
    素材，推送会让它绕过采集质量门槛出现在所有客户的爆款列表里。"""
    require_pg_or_explicit_skip()
    headers = _admin_session(client)
    with psycopg.connect(route_state) as raw:
        _reset_video(raw)
        _seed_ready_media(raw)
        raw.execute(
            "UPDATE viral_videos SET category='链接导入' WHERE video_id=%s",
            (VIDEO_ID,),
        )

    response = client.post(
        PUSH_PATH,
        headers={**headers, "Idempotency-Key": "push-link-import"},
        json={
            "action": "push",
            "reason": "推送测试",
            "confirm": True,
            "items": [{"platform": "douyin", "video_id": VIDEO_ID}],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "VIRAL_LINK_IMPORT_NOT_CURATABLE"
    with psycopg.connect(route_state) as raw:
        row = raw.execute(
            "SELECT collection_published, pushed_at FROM viral_videos WHERE video_id=%s",
            (VIDEO_ID,),
        ).fetchone()
        assert (row[0], row[1]) == (0, None)
        assert raw.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0] == 0


def test_metadata_snapshot_writes_json_sidecar() -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="test")
    video = ViralVideo(
        platform="douyin",
        video_id="vid/1",
        category="乡墅",
        title="标题",
        author="作者",
        author_avatar=None,
        verified=True,
        cover_url=None,
        duration_ms=9000,
        likes=10,
        comments=2,
        shares=3,
        collects=4,
        published_at=1788700000,
        published_display=None,
        like_display=None,
        tags=["乡墅"],
    )
    key = write_viral_metadata_snapshot(
        storage,
        video,
        pushed_at="2026-10-06T01:00:00+00:00",
    )
    assert key == viral_meta_snapshot_key("douyin", "vid/1")
    snapshot = json.loads(storage.get_object(key))
    assert snapshot["videoId"] == "vid/1"
    assert snapshot["statistics"] == {"likes": 10, "comments": 2, "shares": 3, "collects": 4}
    # 共享文案缓存退役后快照不再携带 copy 字段（文案只落数据库，见 viral_push.py）。
    assert "copy" not in snapshot
    assert snapshot["pushedAt"] == "2026-10-06T01:00:00+00:00"
