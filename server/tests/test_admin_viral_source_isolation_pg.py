"""客户个人链接输入与后台爆款运营内容隔离的 HTTP / PostgreSQL 回归。"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F401, F811

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_admin_session_routes import (
    _admin_session,
    _stub_link_import_infra,
    admin_app,
    client,
    route_state,
    sessions_dsn,
)

from app.viral_link import ResolvedViralLink

pytestmark = pytest.mark.pg

VIDEOS = "/api/control/viral/videos"
BATCH = f"{VIDEOS}/curation:batch"


@pytest.fixture()
def source_state(route_state: str) -> str:
    # 本矩阵逐条声明来源，不依赖通用管理夹具中的默认运营视频。
    with psycopg.connect(route_state) as raw:
        raw.execute("DELETE FROM viral_content_sources")
        raw.execute("DELETE FROM viral_videos")
    return route_state


def _seed_video(
    raw: psycopg.Connection,
    video_id: str,
    *sources: str,
    category: str = "",
    ready: bool = False,
    cover: bool = True,
) -> None:
    raw.execute(
        "INSERT INTO viral_videos(platform,video_id,title,category,cover_url,cover_key) "
        "VALUES('douyin',%s,%s,%s,'https://cdn.example/cover.jpg',%s)",
        (
            video_id,
            f"视频-{video_id}",
            category,
            f"viral/cover/douyin/{video_id}" if cover else None,
        ),
    )
    for source in sources:
        raw.execute(
            "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,keyword) "
            "VALUES(%s,%s,'douyin',%s,'')",
            (f"source:{video_id}:{source}", source, video_id),
        )
    if ready:
        raw.execute(
            "INSERT INTO viral_media_preparations"
            "(id,platform,video_id,media_kind,status,storage_uri) "
            "VALUES(%s,'douyin',%s,'video','SUCCEEDED','fake://test/video.mp4')",
            (f"media:{video_id}", video_id),
        )


def _batch(client: TestClient, headers: dict[str, str], action: str, *ids: str):
    return client.post(
        BATCH,
        headers={**headers, "Idempotency-Key": f"source-test:{action}:{':'.join(ids)}"},
        json={
            "action": action,
            "items": [{"platform": "douyin", "video_id": video_id} for video_id in ids],
            "reason": "验证后台素材来源隔离",
            "confirm": True,
        },
    )


def test_customer_inputs_do_not_enter_admin_lists_counts_categories_or_funnel(
    client: TestClient, source_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "admin-link", "admin_link_import", category="平台分类", ready=True)
        _seed_video(raw, "admin-upload", "admin_upload")
        _seed_video(raw, "both", "admin_link_import", "customer_import")
        _seed_video(raw, "customer-only", "customer_import", category="个人独有分类", ready=True)
        _seed_video(raw, "backfill-only", "backfill", category="历史独有分类", ready=True)
        _seed_video(raw, "without-source", category="无来源分类", ready=True)

    page = client.get(VIDEOS, headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 3
    assert {row["video_id"] for row in page.json()["items"]} == {
        "admin-link",
        "admin-upload",
        "both",
    }
    assert not {"个人独有分类", "历史独有分类", "无来源分类"}.intersection(
        page.json()["categories"]
    )
    searched = client.get(VIDEOS, headers=headers, params={"query": "customer-only"})
    assert searched.status_code == 200 and searched.json()["total"] == 0
    paged = client.get(VIDEOS, headers=headers, params={"limit": 1, "offset": 1})
    assert paged.json()["total"] == 3 and len(paged.json()["items"]) == 1

    overview = client.get("/api/control/viral/overview", headers=headers)
    assert overview.status_code == 200, overview.text
    assert overview.json()["content_total"] == 3
    assert overview.json()["archive_ready"] == 1
    content = client.get("/api/control/viral/content-overview", headers=headers)
    assert content.status_code == 200, content.text
    assert content.json()["funnel"][0]["count"] == 3
    assert {item["sourceKind"] for item in content.json()["sourcesByKind"]} == {
        "admin_link_import",
        "admin_upload",
    }


@pytest.mark.parametrize(
    "operation",
    ["details", "preview", "cover", "archive", "single-feature", "prepare", "push", "feature"],
)
def test_personal_video_ids_cannot_use_admin_media_or_curation_endpoints(
    client: TestClient,
    source_state: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(
            raw,
            "personal",
            "customer_import",
            category="链接导入",
            ready=True,
            cover=operation not in {"feature", "single-feature"},
        )

    def unexpected_storage(*args, **kwargs):
        pytest.fail("个人素材应在任何后台存储外呼之前被拒绝")

    monkeypatch.setattr("app.media_routes.get_media_storage", unexpected_storage)
    if operation in {"details", "preview"}:
        response = client.get(f"{VIDEOS}/douyin/personal/{operation}", headers=headers)
    elif operation == "cover":
        response = client.get("/api/control/viral/covers/douyin/personal", headers=headers)
    elif operation in {"archive", "single-feature"}:
        response = client.request(
            "POST" if operation == "archive" else "PATCH",
            f"{VIDEOS}/douyin/personal/{'archive' if operation == 'archive' else 'curation'}",
            headers={**headers, "Idempotency-Key": f"source-test:{operation}"},
            json={"action": "feature", "reason": "验证个人素材隔离", "confirm": True}
            if operation == "single-feature"
            else {"reason": "验证个人素材隔离", "confirm": True},
        )
    else:
        response = _batch(client, headers, operation, "personal")
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"
    with psycopg.connect(source_state) as raw:
        assert raw.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0] == 0
        row = raw.execute(
            "SELECT collection_published,pushed_at,homepage_featured FROM viral_videos "
            "WHERE video_id='personal'"
        ).fetchone()
        assert tuple(row) == (0, None, 0)
        assert raw.execute("SELECT count(*) FROM viral_videos").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["push", "feature"])
def test_verified_admin_source_with_old_link_category_can_be_published(
    client: TestClient, source_state: str, action: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "misclassified", "admin_link_import", category="链接导入", ready=True)
    response = _batch(client, headers, action, "misclassified")
    assert response.status_code == 200, response.text
    with psycopg.connect(source_state) as raw:
        row = raw.execute(
            "SELECT collection_published,homepage_featured FROM viral_videos "
            "WHERE video_id='misclassified'"
        ).fetchone()
        assert row[0 if action == "push" else 1] == 1


@pytest.mark.parametrize(
    "source_kind", ["admin_link_import", "admin_upload", "admin_search", "collection"]
)
def test_each_real_operator_source_remains_in_admin_content(
    client: TestClient, source_state: str, source_kind: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "managed", source_kind)
    response = client.get(VIDEOS, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["video_id"] == "managed"


@pytest.mark.parametrize("audit_action", ["viral_video.push", "viral_video.curation", None])
def test_legacy_backfill_and_publish_flags_do_not_adopt_personal_content(
    client: TestClient, source_state: str, audit_action: str | None
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "legacy", "backfill", "customer_import", ready=True)
        raw.execute(
            "UPDATE viral_videos SET collection_published=1,pushed_at=now(),homepage_featured=1 "
            "WHERE video_id='legacy'"
        )
        if audit_action:
            raw.execute(
                "INSERT INTO audit_logs"
                "(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
                "VALUES('legacy-audit','admin_u',%s,'viral_video','douyin:legacy','{}')",
                (audit_action,),
            )
    assert client.get(VIDEOS, headers=headers).json()["total"] == 0
    response = _batch(client, headers, "push", "legacy")
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("action", ["viral_video.admin_link_import", "viral_video.admin_upload"])
def test_exact_historical_admin_import_audit_recovers_old_managed_material(
    client: TestClient, source_state: str, action: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "historic-admin", "backfill", category="链接导入", ready=True)
        raw.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES('historic-admin-audit','admin_u',%s,'viral_video','douyin:historic-admin','{}')",
            (action,),
        )
    assert client.get(VIDEOS, headers=headers).json()["total"] == 1
    response = _batch(client, headers, "push", "historic-admin")
    assert response.status_code == 200, response.text


def test_mixed_batch_is_rejected_without_publishing_the_managed_first_item(
    client: TestClient, source_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "managed", "admin_link_import", ready=True)
        _seed_video(raw, "personal", "customer_import", ready=True)
    response = _batch(client, headers, "push", "managed", "personal")
    assert response.status_code == 404, response.text
    with psycopg.connect(source_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_videos "
                "WHERE collection_published=1 OR pushed_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert raw.execute("SELECT count(*) FROM viral_refresh_tasks").fetchone()[0] == 0


def test_customer_reimport_does_not_count_as_new_operator_collection(
    client: TestClient, source_state: str
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "old-managed", "admin_link_import", "customer_import")
        raw.execute(
            "UPDATE viral_content_sources SET collected_at=now()-interval '30 days' "
            "WHERE source_kind='admin_link_import'"
        )
    # 同一个视频仍属于运营库存，但客户今天使用它，不等于运营今天再次采集。
    assert client.get(VIDEOS, headers=headers).json()["total"] == 1
    response = client.get("/api/control/viral/content-overview", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["funnel"][0]["count"] == 0
    assert response.json()["sourcesByKind"] == []


def test_explicit_admin_link_import_adopts_previously_personal_video(
    client: TestClient, source_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _admin_session(client)
    with psycopg.connect(source_state) as raw:
        _seed_video(raw, "same-link", "customer_import", category="链接导入")
    assert client.get(VIDEOS, headers=headers).json()["total"] == 0

    class Resolver:
        def resolve(self, url: str, *, purpose: str) -> ResolvedViralLink:
            return ResolvedViralLink(
                platform="douyin",
                video_id="same-link",
                title="管理员明确采集",
                author="作者",
                cover_url=None,
                video_url="https://cdn.example/video.mp4",
                audio_url=None,
                duration_ms=12_000,
                source_description="",
            )

    _stub_link_import_infra(monkeypatch)
    monkeypatch.setattr(
        "app.viral_link.douyidou_link_client_from_settings", lambda conn: Resolver()
    )
    response = client.post(
        "/api/control/viral/link-import",
        headers={**headers, "Idempotency-Key": "explicit-admin-acquire"},
        json={
            "url": "https://v.douyin.com/source-isolation",
            "reason": "平台主动采集",
            "confirm": True,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["archiveQueued"] is True
    page = client.get(VIDEOS, headers=headers).json()
    assert page["total"] == 1 and page["items"][0]["video_id"] == "same-link"
    with psycopg.connect(source_state) as raw:
        kinds = {
            row[0]
            for row in raw.execute(
                "SELECT source_kind FROM viral_content_sources WHERE video_id='same-link'"
            ).fetchall()
        }
        assert kinds == {"customer_import", "admin_link_import"}
        assert raw.execute("SELECT count(*) FROM viral_videos").fetchone()[0] == 1
        config = json.loads(
            raw.execute("SELECT collection_config_json FROM viral_refresh_tasks").fetchone()[0]
        )
        assert config["publish_after"]["actor_user_id"] == "admin_u"
