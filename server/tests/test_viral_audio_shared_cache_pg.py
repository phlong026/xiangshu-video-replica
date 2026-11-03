"""服务端归档音频的共享文案缓存：跨形态凭据（2026-11 改造 WP1）.

历史缺口：共享写入资格 ``_viral_source`` 只认 ``reference_video``，抖音文案导入
拿到音频形态（资产 ``reference_audio``）时全部退化为私有转写——同链接第二人
无法命中文案缓存、被重复收全额转写费。本文件固定放宽后的口径：

- 音频与视频是同一条视频的两个归档形态，正文属于视频本身、不绑定形态；
- 凭据（viral_script_cache 的 archive）带 ``kind``，交付侧按 kind 对应校验，
  音频正文不再被并存的视频归档误判过期（反之亦然）；
- 同一形态重归档（换源/换版本）仍按指纹+归档 URI 拒绝旧正文；
- 客户端上传音轨（无全局内容对象）依旧不能成为共享生产者（见 CW030 既有
  ``test_viral_source_gate_rejects_independent_audio_without_original_video``）。
"""

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-viral-audio-shared-cache-tests-minimum-48-bytes",
)

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation_worker import _pg_audio_connection, _run_audio_lease
from app.script_from_audio import (
    acquire_script_from_audio_task,
    cached_transcript,
    enqueue_script_from_audio_task,
)
from app.storage import FakeStorageAdapter

DB_NAME = "viral_audio_shared_cache_test"
DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module")
def shared_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DB_NAME)


@pytest.fixture()
def pg_state(shared_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, shared_dsn)
    yield shared_dsn
    close_pg_pool()


@contextmanager
def _pg(dsn: str):
    """autocommit 直连；所有 SQL 一律在调用点写字面量 + 参数绑定."""
    with psycopg.connect(dsn, autocommit=True) as raw:
        yield raw


def _seed_base(dsn: str, users: tuple[str, ...]) -> None:
    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO runtime_settings ("
            " id, max_generation_count_per_batch, max_concurrent_h3_tasks,"
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen,"
            " fair_queue_enabled"
            ") VALUES (1, 4, 100, 1000, 10000, 1000, true) "
            "ON CONFLICT (id) DO NOTHING"
        )
        for user in users:
            pg.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES (%s, %s, %s, 'customer') "
                "ON CONFLICT (id) DO UPDATE SET role='customer'",
                (user, user, user),
            )
            pg.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, 1000, 0) ON CONFLICT (user_id) DO UPDATE SET "
                "available_credits=1000, reserved_credits=0",
                (user,),
            )
        pg.execute("DELETE FROM billing_tariffs WHERE service IN ('asr','viral_copy')")
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES ('asr',true,2,1), ('viral_copy',true,2,1)"
        )


def _audio_fixture(dsn: str, monkeypatch: pytest.MonkeyPatch, users: tuple[str, ...]) -> Any:
    """ASR 资费 + 假供应商 + 存储适配器：记录每次真实转写调用."""
    from app import script_from_audio as pipeline
    from app.asr import TranscriptResult

    _seed_base(dsn, users)
    calls: list[str] = []

    class Provider:
        name = "fake-audio-shared-asr"

        def transcribe(self, url: str, *, duration_sec: float | None = None) -> TranscriptResult:
            calls.append(url)
            return TranscriptResult("音频形态写入的共享文案", 12, "zh")

    monkeypatch.setattr(pipeline, "_configured_asr", lambda conn: Provider())
    monkeypatch.setattr(pipeline, "resolve_media_binary", lambda name: name)
    monkeypatch.setattr(
        pipeline, "extract_audio", lambda binary, source, dest: dest.write_bytes(b"audio")
    )
    monkeypatch.setattr(pipeline, "probe_duration_seconds", lambda binary, path: 12)
    storage = FakeStorageAdapter(provider="fake", bucket="shared")
    storage.put_object("audio-source.m4a", b"audio", content_type="audio/mp4")
    storage.put_object("video-source.mp4", b"video", content_type="video/mp4")
    return storage, calls


def _cache_result(dsn: str, video_id: str) -> dict[str, Any]:
    with _pg(dsn) as pg:
        row = pg.execute(
            "SELECT result_json FROM viral_script_cache WHERE video_id=%s", (video_id,)
        ).fetchone()
    assert row is not None
    return cast(dict[str, Any], json.loads(str(row[0])))


def _asr_operation_count(dsn: str, user: str) -> int:
    """按用户过滤：模块库跨用例复用，全局计数会吃进前面用例的计费单."""
    with _pg(dsn) as pg:
        row = pg.execute(
            "SELECT count(*) FROM billing_operations WHERE service='asr' AND user_id=%s",
            (user,),
        ).fetchone()
    return int(row[0])


def _seed_shared_import(
    dsn: str,
    user: str,
    *,
    video_id: str,
    kind: str,
    uri: str,
    sha: str,
    content_type: str,
    object_key: str,
) -> None:
    """用户侧的服务端导入形态：占位项目 + 共享归档资产 + SUCCEEDED 导入行."""
    from app.content_store import retain_content_object

    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES (%s, 'shared-copy', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (f"proj-{user}-{video_id}", user),
        )
    with psycopg.connect(dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        obj, _ = retain_content_object(
            conn,
            sha256=sha,
            size_bytes=5,
            content_type=content_type,
            provider="fake",
            bucket="shared",
            object_key=object_key,
            scope="global",
            pinned=True,
        )
    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, created_by_user_id, metadata_json, content_object_id) "
            "VALUES (%s, %s, %s, %s, %s, 5, %s, %s, %s, %s)",
            (
                f"asset-{user}-{video_id}-{kind}",
                f"proj-{user}-{video_id}",
                kind,
                uri,
                sha,
                content_type,
                user,
                json.dumps({"duration_seconds": 12, "platform": "douyin", "video_id": video_id}),
                obj.id,
            ),
        )
        pg.execute(
            "INSERT INTO viral_import_tasks (id, owner_user_id, project_id, source_asset_id,"
            " platform, video_id, purpose, idempotency_key, request_hash, request_json, status) "
            "VALUES (%s, %s, %s, %s, 'douyin', %s, 'copy', %s, 'hash', '{}', 'SUCCEEDED')",
            (
                f"import-{user}-{video_id}-{kind}",
                user,
                f"proj-{user}-{video_id}",
                f"asset-{user}-{video_id}-{kind}",
                video_id,
                f"ik-{user}-{video_id}-{kind}",
            ),
        )


def _seed_audio_import(dsn: str, user: str, *, video_id: str) -> None:
    _seed_shared_import(
        dsn,
        user,
        video_id=video_id,
        kind="reference_audio",
        uri="fake://shared/audio-source.m4a",
        sha=hashlib.sha256(b"audio").hexdigest(),
        content_type="audio/mp4",
        object_key="audio-source.m4a",
    )


def _seed_video_import(dsn: str, user: str, *, video_id: str) -> None:
    _seed_shared_import(
        dsn,
        user,
        video_id=video_id,
        kind="reference_video",
        uri="fake://shared/video-source.mp4",
        sha=hashlib.sha256(b"video").hexdigest(),
        content_type="video/mp4",
        object_key="video-source.mp4",
    )


def _seed_preparation(dsn: str, *, video_id: str, media_kind: str, uri: str) -> None:
    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO viral_media_preparations (id, platform, video_id, media_kind, status,"
            " storage_uri) VALUES (%s, 'douyin', %s, %s, 'SUCCEEDED', %s) "
            "ON CONFLICT (platform, video_id, media_kind) DO UPDATE SET "
            "status='SUCCEEDED', storage_uri=excluded.storage_uri",
            (f"prep-{video_id}-{media_kind}", video_id, media_kind, uri),
        )


def _seed_viral_video(dsn: str, video_id: str) -> None:
    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
            "VALUES ('douyin', %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
            "DO UPDATE SET deleted_at=NULL",
            (video_id,),
        )


def _enqueue(dsn: str, user: str, video_id: str, key: str) -> Any:
    with psycopg.connect(dsn) as raw:
        return enqueue_script_from_audio_task(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(user, user, user, "customer"),
            project_id=f"proj-{user}-{video_id}",
            source_asset_id=f"asset-{user}-{video_id}-reference_audio",
            idempotency_key=key,
        )


def _enqueue_video_asset(dsn: str, user: str, video_id: str, key: str) -> Any:
    with psycopg.connect(dsn) as raw:
        return enqueue_script_from_audio_task(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(user, user, user, "customer"),
            project_id=f"proj-{user}-{video_id}",
            source_asset_id=f"asset-{user}-{video_id}-reference_video",
            idempotency_key=key,
        )


def _run_worker(dsn: str, storage: Any, worker: str = "audio-worker") -> None:
    """把队列跑空：上一用例可能遗留私有 PENDING 任务，只跑一个 lease 会错位."""
    while True:
        with pg_transaction() as raw:
            lease = acquire_script_from_audio_task(
                BusinessConnection.postgres(raw), worker_id=worker
            )
        if lease is None:
            return
        _run_audio_lease(lease, storage=storage, connection=_pg_audio_connection)


class _ClaimDatabase:
    """claim 直调所需的最小 BusinessDbDep 替身."""

    def __init__(self, dsn: str, user: str) -> None:
        self._dsn = dsn
        self._user = user

    @contextmanager
    def write(self):
        with psycopg.connect(self._dsn) as raw:
            yield (
                BusinessConnection.postgres(raw),
                CurrentUser(self._user, self._user, self._user, "customer"),
            )


def _claim(dsn: str, user: str, video_id: str, task_id: str | None = None) -> Any:
    from app.viral_routes import ViralCopyClaimRequest, claim_viral_video_copy

    request = ViralCopyClaimRequest(platform="douyin", videoId=video_id, taskId=task_id)
    return claim_viral_video_copy(request, cast(Any, _ClaimDatabase(dsn, user)))


def test_audio_transcription_populates_shared_cache_and_second_user_hits_free(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """音频形态生产 → 缓存带 kind=audio 凭据；第二人同链接命中免 ASR、claim 扣交付费."""
    storage, calls = _audio_fixture(pg_state, monkeypatch, ("producer-a1", "follower-b1"))
    video_id = "audio-shared-1"
    _seed_viral_video(pg_state, video_id)
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="audio", uri="fake://shared/audio-source.m4a"
    )
    _seed_audio_import(pg_state, "producer-a1", video_id=video_id)
    _seed_audio_import(pg_state, "follower-b1", video_id=video_id)

    producer = _enqueue(pg_state, "producer-a1", video_id, "first")
    assert producer["status"] == "PENDING"
    _run_worker(pg_state, storage)
    assert len(calls) == 1
    # 凭据带 kind=audio；生产者的导入资产形态被如实记录。
    assert _cache_result(pg_state, video_id)["archive"]["kind"] == "audio"

    # 第二人同链接：入队即命中缓存秒回，不建 ASR 计费单、不再真实转写。
    follower = _enqueue(pg_state, "follower-b1", video_id, "second")
    assert follower["status"] == "SUCCEEDED"
    assert json.loads(str(follower["result_json"]))["text"] == "音频形态写入的共享文案"
    assert len(calls) == 1
    assert _asr_operation_count(pg_state, "producer-a1") == 1
    assert _asr_operation_count(pg_state, "follower-b1") == 0

    # 交付计费：生产者免费；非生产者首提扣一次 viral_copy（单价 2），复看免费。
    producer_claim = _claim(pg_state, "producer-a1", video_id)
    assert producer_claim.text == "音频形态写入的共享文案"
    assert producer_claim.billing.charged == 0
    first_claim = _claim(pg_state, "follower-b1", video_id)
    assert first_claim.text == "音频形态写入的共享文案"
    assert first_claim.billing.charged == 2
    replay_claim = _claim(pg_state, "follower-b1", video_id)
    assert replay_claim.billing.charged == 0
    assert replay_claim.billing.deduped is True


def test_video_credential_is_shared_with_audio_asset_importers(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨形态共享：视频资产生产的正文（kind=video 凭据）对音频资产导入者照常命中.

    对应真实场景：管理端转存视频并完成转写后，用户粘贴链接走音频导入——两种
    形态都是同一条视频的归档载体，命中的是同一条正文，不再退化为私有转写。
    """
    storage, calls = _audio_fixture(pg_state, monkeypatch, ("video-producer", "audio-follower"))
    video_id = "cross-kind-1"
    _seed_viral_video(pg_state, video_id)
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="video", uri="fake://shared/video-source.mp4"
    )
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="audio", uri="fake://shared/audio-source.m4a"
    )
    _seed_video_import(pg_state, "video-producer", video_id=video_id)
    _seed_audio_import(pg_state, "audio-follower", video_id=video_id)

    _enqueue_video_asset(pg_state, "video-producer", video_id, "video-first")
    _run_worker(pg_state, storage)
    assert _cache_result(pg_state, video_id)["archive"]["kind"] == "video"

    hit = _enqueue(pg_state, "audio-follower", video_id, "audio-second")
    assert hit["status"] == "SUCCEEDED"
    assert len(calls) == 1
    assert _asr_operation_count(pg_state, "video-producer") == 1
    assert _asr_operation_count(pg_state, "audio-follower") == 0


def test_same_kind_rearchive_still_rejects_stale_assets(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同形态重归档保护保持：音频归档换源后，指向旧对象的资产与正文都过期."""
    storage, _calls = _audio_fixture(pg_state, monkeypatch, ("owner-a2", "next-b2"))
    video_id = "rearchive-audio-1"
    _seed_viral_video(pg_state, video_id)
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="audio", uri="fake://shared/audio-source.m4a"
    )
    _seed_audio_import(pg_state, "owner-a2", video_id=video_id)
    _seed_audio_import(pg_state, "next-b2", video_id=video_id)
    _enqueue(pg_state, "owner-a2", video_id, "first")
    _run_worker(pg_state, storage)

    # 音频被重新归档（换源/换版本）：旧 URI 的凭据与资产都指向过期版本。
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="audio", uri="fake://shared/audio-v2.m4a"
    )
    with psycopg.connect(pg_state) as raw:
        conn = BusinessConnection.postgres(raw)
        assert cached_transcript(conn, platform="douyin", video_id=video_id) is None
    stale = _enqueue(pg_state, "next-b2", video_id, "after-rearchive")
    assert stale["status"] == "PENDING"
    assert "viral_source" not in json.loads(str(stale["request_json"]))
    # 交付侧同样不再命中：claim 未命中分文不扣。
    miss = _claim(pg_state, "next-b2", video_id)
    assert miss.text is None
    assert miss.billing.charged == 0


def test_video_rearchive_does_not_invalidate_audio_credential(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨形态不误伤：音频凭据只对 audio 归档负责，视频重归档不影响其交付."""
    storage, _calls = _audio_fixture(pg_state, monkeypatch, ("owner-a3", "reader-b3"))
    video_id = "audio-cred-video-rearchive"
    _seed_viral_video(pg_state, video_id)
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="audio", uri="fake://shared/audio-source.m4a"
    )
    _seed_audio_import(pg_state, "owner-a3", video_id=video_id)
    _seed_audio_import(pg_state, "reader-b3", video_id=video_id)
    _enqueue(pg_state, "owner-a3", video_id, "first")
    _run_worker(pg_state, storage)

    # 管理端随后把视频形态转存/重转存：视频归档出现且与音频凭据无关。
    _seed_preparation(
        pg_state, video_id=video_id, media_kind="video", uri="fake://shared/video-v3.mp4"
    )
    hit = _enqueue(pg_state, "reader-b3", video_id, "second")
    assert hit["status"] == "SUCCEEDED"
    delivered = _claim(pg_state, "reader-b3", video_id)
    assert delivered.text == "音频形态写入的共享文案"
    assert delivered.billing.charged == 2
