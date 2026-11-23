"""CW-043 Segment 4+ — 爆款导入 durable-queue 租约机制 TEST-PG 矩阵.

CW-043 审计实施：``CW043-PG-COVERAGE-MATRIX`` §5.1.1 #20 把旧
``test_viral_import.py`` 的 import 全矩阵标为 ⚠️部分（旧文件仅 SQLite，且多为
经 ``X-Dev-User-Id`` 的 TestClient 路由级重型集成）。CW-026 收敛后 PG lane 只认
客户会话 Bearer，路由级 dev-header 用例在 PG 上不可达；本模块改在真实 PG 上直接
验证 ``run_pg_worker_once`` 消费爆款导入队列所用的**持久队列租约机制**——这是
``test_cw030_worker_pg_matrix.py`` 唯一未覆盖的任务类（cw030 grep viral_import = 0）：

- 独占认领（``FOR UPDATE SKIP LOCKED`` + status 状态机：一个 PENDING 任务不会被
  两个 worker 重复认领）
- 过期租约回收 + attempt 递增（RUNNING 且 locked_until 过期 → 复位 PENDING 后被
  新 worker 重新认领，attempt 1→2，started_at 保留）
- 失败释放锁 + retryable 分级（普通异常默认可重试；终态 ViralImportError
  retryable=False 落 retryable=0）
- 陈旧租约失败是 no-op（attempt + locked_by 栅栏令牌：被回收后旧 worker 的迟到
  失败不得覆盖已恢复状态）
- 幂等键按属主唯一（``uq_viral_import_tasks_owner_idempotency`` —— enqueue 的
  ``ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING`` 重放去重所依赖的约束）

PG-lane 事实（CW-043 审计确认）：``viral_import_tasks`` 的 ``locked_until`` /
``updated_at`` / ``created_at`` 皆 ``sa.Text()``（迁移 078），认领/回收用 TEXT 词法
比较 ``locked_until <= now_text``，且 ``now_text`` 由 Python 侧
``datetime.now(UTC).strftime`` 生成——全程与会话 TimeZone 无关。故本模块用固定过去
文本 ``'2020-01-01 00:00:00'`` 制造确定性过期，不依赖 SQL ``now()``。

专属隔离库 ``cw043_viral_import_test`` 已登记 ``pg_test_kit.RECORDED_TEST_DATABASES``；
零 SQLite 替代、零缺库 skip（缺 PG 即硬失败，PG-05）；用例间 DELETE 复位隔离。
认领/失败经 ``pg_transaction`` 生产事务通道（与 ``run_pg_worker_once`` 同形）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

# 审计写入器在导入期即要求 HMAC key（enqueue 走 insert_audit）；先于 app 导入设置。
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw043-viral-import-matrix-tests-minimum-48-bytes-long",
)

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from psycopg.rows import dict_row

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.viral_import import (
    ViralImportError,
    ViralImportLease,
    acquire_viral_import_task,
    fail_viral_import_task,
)

CW043_VIRAL_IMPORT_TEST_DB = "cw043_viral_import_test"


@pytest.mark.parametrize("duration_ms", [0, 3_000, 31_000])
def test_replica_import_rejects_out_of_range_duration(monkeypatch, duration_ms):
    from app import viral_import
    from app.auth import CurrentUser

    monkeypatch.setattr(viral_import, "require_viral_import_enabled", lambda _conn: None)
    monkeypatch.setattr(viral_import, "require_not_auditor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        viral_import,
        "_require_import_video",
        lambda *_args, **_kwargs: SimpleNamespace(duration_ms=duration_ms),
    )
    actor = CurrentUser(id="u1", username="u1", display_name="用户", role="customer")
    request = viral_import.ViralImportRequest(
        platform="douyin", videoId="short-video", purpose="replica"
    )

    with pytest.raises(ViralImportError) as error:
        viral_import.enqueue_viral_import_task(
            object(), actor=actor, request=request, idempotency_key="short-video"
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "VIRAL_IMPORT_DURATION_OUT_OF_RANGE"


@pytest.mark.parametrize(
    "failure", [None, "upload", "tampered", "production", "superseded", "relocated"]
)
def test_cached_local_media_moves_to_cos_without_provider_call_or_losing_source(
    pg_state, tmp_path, monkeypatch, failure
):
    from fastapi import HTTPException

    from app.storage import FakeStorageAdapter, LocalStorageAdapter
    from app.viral_media_preparation import ViralMediaLeaseLost, ViralMediaPreparation

    # Each failure mode owns its row; a failed preceding case must not turn the
    # next setup into the real FAILED preparation cooldown.
    _exec(pg_state, "DELETE FROM viral_media_preparations WHERE video_id='legacy-migrate'")
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "0")
    old = LocalStorageAdapter(root=tmp_path, bucket="local-private")
    content = b"verified historical video"
    original, _ = ViralMediaPreparation(storage=old).fetch(
        platform="douyin",
        video_id="legacy-migrate",
        kind="video",
        prepare=lambda key, check: old.put_object(key, content, content_type="video/mp4"),
    )
    before = _rows(
        pg_state, "SELECT * FROM viral_media_preparations WHERE video_id='legacy-migrate'"
    )[0]
    cloud = FakeStorageAdapter(provider="cos", bucket="current")
    if failure == "upload":

        def fail_upload(*args, **kwargs):
            raise RuntimeError("cloud unavailable")

        monkeypatch.setattr(cloud, "put_file", fail_upload)
    elif failure == "tampered":
        _exec(
            pg_state,
            "UPDATE viral_media_preparations SET storage_uri='local://local-private/unrelated.mp4' "
            "WHERE video_id='legacy-migrate'",
        )
    elif failure == "production":
        monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "1")
    elif failure == "relocated":
        import shutil

        restored = tmp_path.parent / f"{tmp_path.name}-restored"
        shutil.copytree(tmp_path, restored)
        monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(restored))
    elif failure == "superseded":
        original_put = cloud.put_file

        def concurrent_update(*args, **kwargs):
            stored = original_put(*args, **kwargs)
            _exec(
                pg_state,
                "UPDATE viral_media_preparations SET status='RUNNING',"
                "attempt=attempt+1 WHERE video_id='legacy-migrate'",
            )
            return stored

        monkeypatch.setattr(cloud, "put_file", concurrent_update)
    coordinator = ViralMediaPreparation(storage=cloud)
    if failure in {"upload", "production", "superseded"}:
        with pytest.raises((RuntimeError, HTTPException, ViralMediaLeaseLost)):
            coordinator.cached(platform="douyin", video_id="legacy-migrate", kind="video")
    else:
        result = coordinator.cached(platform="douyin", video_id="legacy-migrate", kind="video")
        if failure == "tampered":
            assert result is None
        else:
            assert result is not None and result.uri.startswith("cos://current/")
            assert cloud.get_object(result.key) == content
            assert (
                coordinator.cached(platform="douyin", video_id="legacy-migrate", kind="video")
                == result
            )
    after = _rows(
        pg_state, "SELECT * FROM viral_media_preparations WHERE video_id='legacy-migrate'"
    )[0]
    if failure in {"upload", "production"}:
        assert after["storage_uri"] == before["storage_uri"]
        assert after["cache_scope"] == before["cache_scope"]
    elif failure == "superseded":
        assert after["status"] == "RUNNING"
        assert after["attempt"] == before["attempt"] + 1
        assert after["storage_uri"] == before["storage_uri"]
    assert old.get_object(original.key) == content


def test_shared_media_preparation_cross_connection_cache(pg_state: str, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    started, release = Event(), Event()
    calls = []

    def prepare(key, check):
        calls.append(key)
        started.set()
        assert release.wait(5)
        check()
        return storage.put_object(key, b"video", content_type="video/mp4")

    def fetch():
        return ViralMediaPreparation(storage=storage, poll_seconds=0.01).fetch(
            platform="wechat_channels", video_id="same-video", kind="video", prepare=prepare
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch)
        assert started.wait(5)
        second = pool.submit(fetch)
        release.set()
        a, b = first.result(), second.result()
    assert len(calls) == 1
    assert a[0].uri == b[0].uri
    assert sorted([a[1], b[1]]) == [False, True]
    row = _rows(pg_state, "SELECT * FROM viral_media_preparations")[0]
    assert row["status"] == "SUCCEEDED"
    assert row["storage_uri"] == a[0].uri


@pytest.mark.parametrize("stage", ["upload", "publish"])
def test_expired_shared_attempt_cannot_publish_or_delete_successor(pg_state, stage):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaLeaseLost, ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    old = ViralMediaPreparation(storage=storage)
    paused, release = Event(), Event()
    old_keys = []

    def prepare_old(key, check):
        old_keys.append(key)
        stored = storage.put_object(key, b"old-video", content_type="video/mp4")
        if stage == "upload":
            paused.set()
            assert release.wait(5)
        return stored

    publish = old._publish

    def pause_publish(lease, stored):
        paused.set()
        assert release.wait(5)
        publish(lease, stored)

    if stage == "publish":
        old._publish = pause_publish
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            old.fetch,
            platform="wechat_channels",
            video_id="lease-video",
            kind="video",
            prepare=prepare_old,
        )
        assert paused.wait(5)
        _exec(pg_state, "UPDATE viral_media_preparations SET locked_until='2020-01-01 00:00:00'")
        newer, hit = ViralMediaPreparation(storage=storage).fetch(
            platform="wechat_channels",
            video_id="lease-video",
            kind="video",
            prepare=lambda key, check: storage.put_object(
                key, b"new-video", content_type="video/mp4"
            ),
        )
        release.set()
        with pytest.raises(ViralMediaLeaseLost):
            future.result()
    assert not hit
    assert newer.key != old_keys[0]
    assert storage.get_object(newer.key) == b"new-video"
    row = _rows(pg_state, "SELECT * FROM viral_media_preparations")[0]
    assert row["status"] == "SUCCEEDED" and row["storage_uri"] == newer.uri
    assert row["attempt"] == 2


def test_preview_and_import_share_one_object_across_every_import(pg_state, monkeypatch):
    """DEDUP-CAS-20260916：导入不再为每个项目各写一份私有副本。

    旧断言固定的是「每次导入一份 attempt 私有副本」（`new.stored.key !=
    old.stored.key != other.stored.key`）。那正是本次要消除的重复：同一平台视频
    导入 N 个项目就存 N 份完整视频。新不变量更强——三个导入（同属主两次 +
    他属主一次）必须指向**同一个**对象，且下载只发生一次。
    """
    from test_viral_media import _video

    from app import viral_media
    from app.storage import FakeStorageAdapter
    from app.viral_import import (
        ViralImportWork,
        discard_viral_import_outcome,
        perform_viral_import_task,
    )

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    video = _video("douyin", _playback_version=1)
    calls = []

    def stream(self, url, *, extra_headers=None):
        calls.append(url)
        yield b"\x00\x00\x00\x18ftypisom" + b"video"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", stream)
    preview = viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    objects_before_imports = len(storage._objects)

    def work(owner, attempt):
        return ViralImportWork(
            lease=ViralImportLease(
                id=f"task-{owner}",
                worker_id=f"worker-{attempt}",
                owner_user_id=owner,
                project_id=f"project-{owner}",
                platform=video.platform,
                video_id=video.video_id,
                purpose="replica",
                attempt=attempt,
            ),
            video=video,
            storage=storage,
            prefer="video",
        )

    old = perform_viral_import_task(work("u1", 1))
    new = perform_viral_import_task(work("u1", 2))
    other = perform_viral_import_task(work("u2", 1))
    discard_viral_import_outcome(storage, outcome=old, actor_id="u1")
    assert len(calls) == 1
    # One shared object serves every import, including the other user's.
    assert new.stored.key == old.stored.key == other.stored.key
    assert len(storage._objects) == objects_before_imports
    assert storage.head_object(new.stored.key) and storage.head_object(other.stored.key)
    assert (
        _rows(pg_state, "SELECT storage_uri FROM viral_media_preparations")[0]["storage_uri"]
        == preview.storage_uri
    )
    # Discarding an outcome must not remove the shared bytes any more.
    assert storage.head_object(old.stored.key) is not None


def test_broken_copy_backend_cannot_fail_an_import(pg_state, monkeypatch):
    """DEDUP-CAS-20260916：导入不再调用 copy_object，故副本故障不再能拖垮导入。

    旧版本用 ``BrokenCopy.copy_object`` 抛错来证明「副本失败不会污染共享媒体」——
    那条路径如今根本不存在。保留同一个故障注入，断言的是更强的事实：整个导入
    过程一次 copy 都不发起，共享媒体依旧是缓存命中。
    """
    from test_viral_media import _video

    from app import viral_media
    from app.storage import FakeStorageAdapter
    from app.viral_import import ViralImportWork, perform_viral_import_task

    _exec(pg_state, "DELETE FROM viral_media_preparations")

    class BrokenCopy(FakeStorageAdapter):
        def copy_object(self, source_key, destination_key):
            raise RuntimeError("project storage copy failed")

    storage = BrokenCopy(provider="cos", bucket="shared")
    video = _video("douyin", _playback_version=1)
    calls = []

    def stream(self, url, *, extra_headers=None):
        calls.append(url)
        yield b"\x00\x00\x00\x18ftypisom"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", stream)
    work = ViralImportWork(
        lease=ViralImportLease(
            id="copy-fails",
            worker_id="worker",
            owner_user_id="u1",
            project_id="project",
            platform=video.platform,
            video_id=video.video_id,
            purpose="replica",
            attempt=1,
        ),
        video=video,
        storage=storage,
        prefer="video",
    )
    viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    outcome = perform_viral_import_task(work)
    assert outcome.stored.key
    cached = viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    assert cached.cache_hit and len(calls) == 1


def test_copy_import_prefers_audio_and_falls_back_to_archived_video(pg_state, monkeypatch):
    """文案导入首选音频归档；只归档过视频的同一视频回落到视频对象，不空转重试。

    「只归档过视频」对应三类真实来路：复刻解析（旧决策强制缓存整条视频）、每周
    采集、以及本次改动之前完成的文案解析。2026-09-30 拍板后文案解析只归档音频，
    两种形态必须都能被只读导入 Worker 消费。
    """
    from test_viral_media import _video

    from app import viral_media
    from app.storage import FakeStorageAdapter
    from app.viral_import import ViralImportWork, perform_viral_import_task
    from app.viral_media_preparation import ViralMediaBusy

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    video = _video("douyin", "copy-mixed-archive")

    def stream(self, url, *, extra_headers=None):
        yield b"\x00\x00\x00\x18ftypisom" + b"bytes"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", stream)

    def work():
        return ViralImportWork(
            lease=ViralImportLease(
                id="copy-task",
                worker_id="worker",
                owner_user_id="u1",
                project_id="project",
                platform=video.platform,
                video_id=video.video_id,
                purpose="copy",
                attempt=1,
            ),
            video=video,
            storage=storage,
            prefer="audio",
        )

    # 没有任何归档时保留真正的未就绪错误，不能为了回落静默抓取源站。
    with pytest.raises(ViralMediaBusy):
        perform_viral_import_task(work())
    # 场景一：此前只归档过视频（复刻解析/采集/旧流程）→ 首选音频报未就绪后回落。
    viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="video"
    )
    outcome = perform_viral_import_task(work())
    assert outcome.media_kind == "video"

    # 场景二：音频归档就位（新文案解析的常态）→ 首选直接命中，不再碰视频。
    viral_media.ViralMediaPipeline(client=None, storage=storage, shared=True).fetch(
        video, prefer="audio"
    )
    outcome = perform_viral_import_task(work())
    assert outcome.media_kind == "audio"


def test_shared_audio_and_video_prepare_independently(pg_state):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="independent-kinds")
    barrier = Barrier(2)
    calls = []

    def fetch(kind):
        def prepare(key, check):
            calls.append(kind)
            barrier.wait(timeout=10)
            check()
            return storage.put_object(key, kind.encode(), content_type=f"{kind}/mp4")

        return ViralMediaPreparation(storage=storage).fetch(
            platform="douyin", video_id="one-video-two-kinds", kind=kind, prepare=prepare
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        audio, video = list(pool.map(fetch, ["audio", "video"]))
    assert sorted(calls) == ["audio", "video"]
    assert audio[0].uri != video[0].uri
    assert not audio[1] and not video[1]
    for kind, result in [("audio", audio), ("video", video)]:
        hit = ViralMediaPreparation(storage=storage).cached(
            platform="douyin", video_id="one-video-two-kinds", kind=kind
        )
        assert hit == result[0]
        assert storage.get_object(hit.key) == kind.encode()


def test_changed_storage_namespace_and_missing_object_prepare_again(pg_state):
    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storages = [
        FakeStorageAdapter(provider="cos", bucket="shared", key_prefix=prefix)
        for prefix in ("first", "second")
    ]
    results = []
    for storage in storages:
        coordinator = ViralMediaPreparation(storage=storage)

        def prepare(key, check):
            return storage.put_object(key, b"video", content_type="video/mp4")

        first, hit = coordinator.fetch(
            platform="wechat_channels", video_id="scope", kind="video", prepare=prepare
        )
        assert not hit
        storage.delete_object(first.key)
        recovered, hit = coordinator.fetch(
            platform="wechat_channels", video_id="scope", kind="video", prepare=prepare
        )
        assert not hit and recovered.key != first.key
        results.append(recovered)
    assert results[0].uri != results[1].uri


def test_waiter_does_not_reclaim_when_running_snapshot_becomes_succeeded(pg_state, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.storage import FakeStorageAdapter
    from app.viral_media_preparation import ViralMediaPreparation

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    started, release = Event(), Event()
    owner, waiter = ViralMediaPreparation(storage=storage), ViralMediaPreparation(storage=storage)

    def prepare(key, check):
        started.set()
        assert release.wait(5)
        return storage.put_object(key, b"video", content_type="video/mp4")

    cached = waiter._cached

    def interleave(row, kind):
        if row["status"] == "RUNNING":
            release.set()
            future.result(timeout=5)
            return None
        return cached(row, kind)

    monkeypatch.setattr(waiter, "_cached", interleave)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            owner.fetch, platform="wechat_channels", video_id="race", kind="video", prepare=prepare
        )
        assert started.wait(5)
        result, hit = waiter.fetch(
            platform="wechat_channels",
            video_id="race",
            kind="video",
            prepare=lambda key, check: pytest.fail("completed media downloaded again"),
        )
    assert hit and result.uri == future.result()[0].uri
    assert _rows(pg_state, "SELECT attempt FROM viral_media_preparations")[0]["attempt"] == 1


@pytest.mark.parametrize("interval_days", [1, 7])
def test_schedule_is_unique_and_does_not_repeat_until_due(pg_state, interval_days):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from app.viral_collection import enqueue_due_viral_collections

    _exec(pg_state, "DELETE FROM viral_refresh_tasks")
    _exec(pg_state, "DELETE FROM viral_runtime_controls")
    keywords = [{"platform": "wechat_channels", "category": "预算", "keyword": "建房预算"}]
    _exec(
        pg_state,
        "INSERT INTO viral_runtime_controls"
        "(id,collection_enabled,import_enabled,keywords_json,collection_interval_days) "
        "VALUES(1,1,1,%s,%s)",
        (json.dumps(keywords), interval_days),
    )

    def tick():
        with pg_transaction() as raw:
            enqueue_due_viral_collections(BusinessConnection.postgres(raw))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(), range(2)))
    assert len(_rows(pg_state, "SELECT * FROM viral_refresh_tasks")) == 1
    assert _rows(
        pg_state,
        f"SELECT next_collection_at BETWEEN now()+interval '{interval_days * 24 - 1} hours' "
        f"AND now()+interval '{interval_days * 24 + 1} hours' AS future "
        "FROM viral_runtime_controls",
    )[0]["future"]
    _exec(pg_state, "UPDATE viral_refresh_tasks SET status='SUCCEEDED'")
    tick()
    assert _rows(pg_state, "SELECT status FROM viral_refresh_tasks")[0]["status"] == "SUCCEEDED"
    _exec(
        pg_state, "UPDATE viral_runtime_controls SET next_collection_at=now()-interval '1 minute'"
    )
    tick()
    assert _rows(pg_state, "SELECT status FROM viral_refresh_tasks")[0]["status"] == "PENDING"
    _exec(
        pg_state,
        "UPDATE viral_refresh_tasks SET status='FAILED',retryable=1,retry_count=2,"
        "updated_at='2020-01-01 00:00:00'",
    )
    tick()
    assert _rows(pg_state, "SELECT status FROM viral_refresh_tasks")[0]["status"] == "FAILED"
    _exec(pg_state, "DELETE FROM viral_runtime_controls")


def test_customer_cached_only_path_never_starts_preparation(pg_state, monkeypatch):
    from test_viral_media import _video

    from app.storage import FakeStorageAdapter
    from app.viral_media import UrlFetcher, ViralMediaPipeline
    from app.viral_media_preparation import ViralMediaBusy

    _exec(pg_state, "DELETE FROM viral_media_preparations")
    storage = FakeStorageAdapter(provider="cos", bucket="shared")
    monkeypatch.setattr(
        UrlFetcher, "iter_fetch", lambda *args: pytest.fail("customer click fetched source")
    )
    with pytest.raises(ViralMediaBusy):
        ViralMediaPipeline(client=None, storage=storage, shared=True, cached_only=True).fetch(
            _video("douyin"), prefer="video"
        )
    assert _rows(pg_state, "SELECT * FROM viral_media_preparations") == []


# 固定“已过期”时刻：acquire 用 TEXT 词法比较 locked_until <= now_text，
# 该值恒小于任何真实 now（2026+），确定性触发回收且与会话 TimeZone 无关。
_EXPIRED = "2020-01-01 00:00:00"
# 固定“仍持有”时刻：恒大于真实 now，代表未过期的活动租约。
_HELD = "2999-01-01 00:00:00"
_SEED_STAMP = "2026-09-06 03:00:00"


# ---------------------------------------------------------------------------
# 座子：专属库 + DATABASE_URL_ENV 指向它（pg_transaction 走生产 PG 通道）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def import_dsn() -> Iterator[str]:
    """专属爆款导入队列测试库：建库 → alembic head → 用完即删."""
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW043_VIRAL_IMPORT_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW043_VIRAL_IMPORT_TEST_DB)


@pytest.fixture()
def pg_state(import_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """DATABASE_URL_ENV 指向专属库：``pg_transaction`` 走生产 PG 事务通道."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, import_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield import_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# 原语播种 helper（worker-identical 行，无路由机制）
# ---------------------------------------------------------------------------

# 叶子优先 DELETE：viral_import_tasks 的 project_id/owner_user_id 是 FK。
_CLEANUP_ORDER = (
    "viral_import_tasks",
    "viral_link_resolution_receipts",
    "viral_media_preparations",
    "viral_video_visibility",
    "viral_content_sources",
    "viral_videos",
    "projects",
    "users",
)


def _exec(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(sql, params)


def _rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as pg:
        return [dict(row) for row in pg.execute(sql, params).fetchall()]


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(sql, params).fetchone()
        return None if row is None else row[0]


def _truncate(dsn: str) -> None:
    for table in _CLEANUP_ORDER:
        _exec(dsn, f"DELETE FROM {table}")


def _seed_operator_source(dsn: str, video_id: str) -> None:
    _exec(
        dsn,
        "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,keyword) "
        "VALUES('test-operator','admin_link_import','douyin',%s,'')",
        (video_id,),
    )


def _seed_base(dsn: str) -> None:
    _truncate(dsn)
    _exec(
        dsn,
        "INSERT INTO users (id, username, display_name, role) VALUES"
        " ('u1', 'u1', 'User One', 'employee'),"
        " ('u2', 'u2', 'User Two', 'employee')",
    )
    _exec(
        dsn,
        "INSERT INTO projects (id, name, owner_user_id) VALUES"
        " ('proj-1', 'CW043 Viral Import', 'u1')",
    )


@pytest.mark.parametrize("platform", ["douyin", "wechat_channels", "xiaohongshu"])
@pytest.mark.parametrize("legacy_pushed", [False, True])
@pytest.mark.parametrize("receipt_duration", [None, 0])
def test_private_link_receipt_preserves_owner_import_without_public_listing(
    pg_state, monkeypatch, platform, legacy_pushed, receipt_duration
):
    from contextlib import contextmanager

    from app import viral_routes
    from app.auth import CurrentUser
    from app.storage import FakeStorageAdapter
    from app.viral_import import (
        ViralImportRequest,
        complete_viral_import_task,
        enqueue_viral_import_task,
        perform_viral_import_task,
        prepare_viral_import_task,
    )
    from app.viral_media import UrlFetcher
    from app.viral_media_preparation import ViralMediaPreparation
    from app.viral_store import is_customer_visible_viral_video, list_viral_video_page

    _seed_base(pg_state)
    monkeypatch.setenv("VIDEO_REPLICA_VIRAL_CLIENT_DISABLED", "1")
    _exec(
        pg_state,
        "INSERT INTO viral_runtime_controls(id,import_enabled) VALUES(1,1) "
        "ON CONFLICT(id) DO UPDATE SET import_enabled=1",
    )
    _exec(
        pg_state,
        "INSERT INTO viral_videos(platform,video_id,title,category,duration_ms) "
        "VALUES(%s,'private-link','本人链接素材','链接导入',6000)",
        (platform,),
    )
    if legacy_pushed:
        _exec(
            pg_state,
            "UPDATE viral_videos SET collection_published=1,pushed_at=now(),"
            "homepage_featured=1 WHERE video_id='private-link'",
        )
    response = {
        "item": {"platform": platform, "videoId": "private-link"},
        "importIdempotencyKey": "personal-link",
    }
    if receipt_duration is not None:
        response["item"]["durationMs"] = receipt_duration
    _exec(
        pg_state,
        "INSERT INTO viral_link_resolution_receipts(id,owner_user_id,idempotency_key,"
        "normalized_url,purpose,request_hash,status,response_json,lease_owner,lease_expires_at) "
        "VALUES('private-receipt','u1','resolve-personal','https://example.invalid/share',"
        "'replica','hash','SUCCEEDED',%s,'owner',now())",
        (json.dumps(response),),
    )
    storage = FakeStorageAdapter(provider="cos", bucket="personal-import")
    monkeypatch.setattr(
        storage, "_signed_url", lambda method, key, expires_at: f"https://archive.invalid/{key}"
    )
    ViralMediaPreparation(storage=storage).fetch(
        platform=platform,
        video_id="private-link",
        kind="video",
        prepare=lambda key, check: storage.put_object(
            key, b"private-video", content_type="video/mp4"
        ),
    )

    def forbid_source(*args, **kwargs):
        raise AssertionError("个人导入worker只读取已归档素材")

    monkeypatch.setattr(UrlFetcher, "iter_fetch", forbid_source)
    actor = CurrentUser(id="u1", username="u1", display_name="本人", role="employee")

    class CustomerDb:
        @contextmanager
        def write(self):
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), actor

    monkeypatch.setattr(viral_routes, "get_media_storage", lambda conn: storage)
    media = viral_routes.fetch_viral_video_media(
        viral_routes.ViralMediaRequest(platform=platform, videoId="private-link", kind="audio"),
        CustomerDb(),
    )
    assert media.kind == "video" and media.cacheHit
    request = ViralImportRequest(platform=platform, videoId="private-link", purpose="replica")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        assert list_viral_video_page(conn, platform=platform, sort="hot", limit=10).total == 0
        for user_id, expected in (("u1", True), ("u2", False)):
            assert (
                is_customer_visible_viral_video(
                    conn, platform=platform, video_id="private-link", user_id=user_id
                )
                is expected
            )
        with pytest.raises(ViralImportError) as denied:
            enqueue_viral_import_task(
                conn,
                actor=CurrentUser(id="u2", username="u2", display_name="他人", role="employee"),
                request=request,
                idempotency_key="stolen-link",
            )
        assert denied.value.status_code == 404
        row = enqueue_viral_import_task(
            conn,
            actor=actor,
            request=request,
            idempotency_key="personal-link",
        )
        task_id = str(row["id"])
    lease = _acquire("personal-worker")
    assert lease is not None and lease.id == task_id
    with pg_transaction() as raw:
        work = prepare_viral_import_task(
            BusinessConnection.postgres(raw), lease=lease, storage=storage
        )
    outcome = perform_viral_import_task(work)
    with pg_transaction() as raw:
        complete_viral_import_task(BusinessConnection.postgres(raw), lease=lease, outcome=outcome)
    assert _import_row(pg_state, task_id)["status"] == "SUCCEEDED"
    assert _one(pg_state, "SELECT count(*) FROM assets WHERE created_by_user_id='u1'") == 1
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        assert list_viral_video_page(conn, platform=platform, sort="hot", limit=10).total == 0
        assert not is_customer_visible_viral_video(
            conn, platform=platform, video_id="private-link", user_id="u2"
        )


@pytest.mark.parametrize("phase", ["enqueue", "prepare", "complete"])
@pytest.mark.parametrize(
    "state", ["never_pushed", "withdrawn", "hidden", "future", "expired", "deleted"]
)
def test_public_import_rechecks_publication_at_each_delivery_boundary(pg_state, phase, state):
    from app.auth import CurrentUser
    from app.storage import FakeStorageAdapter
    from app.viral_import import (
        ViralImportOutcome,
        ViralImportRequest,
        complete_viral_import_task,
        enqueue_viral_import_task,
        prepare_viral_import_task,
    )

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO viral_runtime_controls(id,import_enabled) VALUES(1,1) "
        "ON CONFLICT(id) DO UPDATE SET import_enabled=1",
    )
    _exec(
        pg_state,
        "INSERT INTO viral_videos(platform,video_id,title,duration_ms,collection_published,"
        "pushed_at,homepage_featured) "
        "VALUES('douyin','publication-guard','人工发布',6000,1,now(),1)",
    )
    _seed_operator_source(pg_state, "publication-guard")
    _exec(
        pg_state,
        "INSERT INTO viral_media_preparations(id,platform,video_id,media_kind,status,storage_uri) "
        "VALUES('publication-media','douyin','publication-guard','video','SUCCEEDED',"
        "'cos://guard/source.mp4')",
    )
    if state != "never_pushed":
        _exec(
            pg_state,
            "INSERT INTO viral_link_resolution_receipts(id,owner_user_id,idempotency_key,"
            "normalized_url,purpose,request_hash,status,response_json,lease_owner,"
            "lease_expires_at) "
            "VALUES('guard-receipt','u1','resolve-guard','https://example.invalid/share',"
            "'replica','hash','SUCCEEDED',%s,'owner',now())",
            (
                json.dumps(
                    {
                        "item": {
                            "platform": "douyin",
                            "videoId": "publication-guard",
                            "durationMs": 6000,
                        }
                    }
                ),
            ),
        )
    actor = CurrentUser(id="u1", username="u1", display_name="本人", role="employee")
    request = ViralImportRequest(platform="douyin", videoId="publication-guard", purpose="replica")
    if phase != "enqueue":
        with pg_transaction() as raw:
            enqueue_viral_import_task(
                BusinessConnection.postgres(raw),
                actor=actor,
                request=request,
                idempotency_key="guard",
            )
        lease = _acquire("guard-worker")
        assert lease is not None
    if state == "hidden":
        _exec(
            pg_state,
            "INSERT INTO viral_video_visibility(platform,video_id,status) "
            "VALUES('douyin','publication-guard','HIDDEN')",
        )
    else:
        assignment = {
            "never_pushed": "pushed_at=NULL",
            "withdrawn": "collection_published=0",
            "future": "homepage_starts_at=now()+interval '1 day'",
            "expired": "homepage_ends_at=now()-interval '1 day'",
            "deleted": "deleted_at=now()",
        }[state]
        _exec(pg_state, f"UPDATE viral_videos SET {assignment} WHERE video_id='publication-guard'")
    storage = FakeStorageAdapter(provider="cos", bucket="guard")
    with pytest.raises(ViralImportError) as denied:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            if phase == "enqueue":
                enqueue_viral_import_task(
                    conn, actor=actor, request=request, idempotency_key="guard"
                )
            elif phase == "prepare":
                prepare_viral_import_task(conn, lease=lease, storage=storage)
            else:
                stored = storage.put_object("source.mp4", b"video", content_type="video/mp4")
                complete_viral_import_task(
                    conn,
                    lease=lease,
                    outcome=ViralImportOutcome(
                        stored=stored, media_kind="video", duration_seconds=6
                    ),
                )
    assert denied.value.detail["code"] in {"VIRAL_VIDEO_NOT_FOUND", "VIRAL_VIDEO_UNAVAILABLE"}
    assert _one(pg_state, "SELECT count(*) FROM assets") == 0


def _seed_import_task(
    dsn: str,
    *,
    task_id: str,
    owner: str = "u1",
    project_id: str | None = "proj-1",
    platform: str = "douyin",
    video_id: str = "vid-1",
    purpose: str = "replica",
    idempotency_key: str | None = None,
    status: str = "PENDING",
    attempt: int = 0,
    locked_by: str | None = None,
    locked_until: str | None = None,
) -> None:
    _exec(
        dsn,
        "INSERT INTO viral_import_tasks (id, owner_user_id, project_id, platform,"
        " video_id, purpose, idempotency_key, request_hash, request_json, status,"
        " attempt, locked_by, locked_until, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{}', %s, %s, %s, %s, %s, %s)",
        (
            task_id,
            owner,
            project_id,
            platform,
            video_id,
            purpose,
            idempotency_key or f"ik-{task_id}",
            f"rh-{task_id}",
            status,
            attempt,
            locked_by,
            locked_until,
            _SEED_STAMP,
            _SEED_STAMP,
        ),
    )


def _import_row(dsn: str, task_id: str) -> dict[str, Any]:
    return _rows(
        dsn,
        "SELECT status, attempt, locked_by, locked_until, retryable, error_code,"
        " started_at FROM viral_import_tasks WHERE id = %s",
        (task_id,),
    )[0]


def _expire_lease(dsn: str, task_id: str) -> None:
    _exec(
        dsn,
        "UPDATE viral_import_tasks SET locked_until = %s WHERE id = %s",
        (_EXPIRED, task_id),
    )


def _acquire(worker_id: str) -> ViralImportLease | None:
    with pg_transaction() as raw:
        return acquire_viral_import_task(BusinessConnection.postgres(raw), worker_id=worker_id)


def _fail(lease: ViralImportLease, cause: Exception) -> None:
    with pg_transaction() as raw:
        fail_viral_import_task(BusinessConnection.postgres(raw), lease=lease, cause=cause)


def test_import_commit_acknowledgement_loss_preserves_published_project_copy(pg_state, monkeypatch):
    from contextlib import contextmanager

    from app import generation_worker as worker
    from app.storage import FakeStorageAdapter
    from app.viral_import import ViralImportOutcome

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO viral_videos(platform,video_id,title,collection_published,pushed_at) "
        "VALUES('douyin','vid-1','已推送素材',1,now())",
    )
    _seed_operator_source(pg_state, "vid-1")
    _exec(
        pg_state,
        "INSERT INTO viral_media_preparations(id,platform,video_id,media_kind,status,storage_uri) "
        "VALUES('commit-media','douyin','vid-1','video','SUCCEEDED','cos://project/source.mp4')",
    )
    _seed_import_task(pg_state, task_id="commit-receipt")
    storage = FakeStorageAdapter(provider="cos", bucket="project")
    stored = storage.put_object("project/attempt-1.mp4", b"project-video", content_type="video/mp4")
    outcome = ViralImportOutcome(stored=stored, media_kind="video", duration_seconds=5)
    monkeypatch.setattr(worker, "prepare_viral_import_task", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "perform_viral_import_task", lambda *_: outcome)
    original = worker.pg_transaction
    lost = False

    @contextmanager
    def commit_then_disconnect():
        nonlocal lost
        with original() as raw:
            yield raw
            published = (
                raw.execute(
                    "SELECT status FROM viral_import_tasks WHERE id='commit-receipt'"
                ).fetchone()[0]
                == "SUCCEEDED"
            )
        if published and not lost:
            lost = True
            raise OSError("committed transaction acknowledgement lost")

    monkeypatch.setattr(worker, "pg_transaction", commit_then_disconnect)
    assert worker.run_pg_worker_once(worker_id="commit-worker", storage=storage, max_tasks=1) == 1
    assert lost
    assert _import_row(pg_state, "commit-receipt")["status"] == "SUCCEEDED"
    assert storage.get_object(stored.key) == b"project-video"
    assert _one(pg_state, "SELECT count(*) FROM assets WHERE storage_uri=%s", (stored.uri,)) == 1


# ===========================================================================
# A. 独占认领（FOR UPDATE SKIP LOCKED + 状态机）
# ===========================================================================


def test_acquire_viral_import_lease_is_exclusive_on_real_pg(pg_state: str) -> None:
    """一个 PENDING 导入任务只被一个 worker 认领：worker-a 得租约（attempt 0→1、
    status→RUNNING、locked_by 落定），worker-b 在真实 PG 的 ``FOR UPDATE SKIP
    LOCKED`` + ``status='PENDING'`` 状态机下拿不到同一任务（返回 None）。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")

    first = _acquire("worker-a")
    assert first is not None
    assert first.id == "vi-1"
    assert first.worker_id == "worker-a"
    assert first.attempt == 1
    assert first.owner_user_id == "u1"
    assert first.project_id == "proj-1"

    second = _acquire("worker-b")
    assert second is None

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-a"
    assert int(row["attempt"]) == 1
    # started_at 首次认领即落定。
    assert row["started_at"] is not None


def test_expired_viral_import_lease_is_reclaimed_and_attempt_increments_on_real_pg(
    pg_state: str,
) -> None:
    """worker-a 持有的租约过期（locked_until 过去）后，acquire 的首段 UPDATE 把它
    复位 PENDING（保留 attempt），次段 UPDATE 由 worker-b 重新认领：attempt 1→2、
    locked_by 换到 worker-b、status 仍 RUNNING。这是 PG lane 崩溃 worker 的任务
    回收路径。"""
    _seed_base(pg_state)
    _seed_import_task(
        pg_state,
        task_id="vi-1",
        status="RUNNING",
        attempt=1,
        locked_by="worker-a",
        locked_until=_HELD,
    )
    _expire_lease(pg_state, "vi-1")

    reacquired = _acquire("worker-b")
    assert reacquired is not None
    assert reacquired.id == "vi-1"
    assert reacquired.attempt == 2

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "RUNNING"
    assert row["locked_by"] == "worker-b"
    assert int(row["attempt"]) == 2


# ===========================================================================
# B. 失败释放锁 + retryable 分级
# ===========================================================================


def test_fail_viral_import_task_releases_lease_and_marks_retryable_on_real_pg(
    pg_state: str,
) -> None:
    """普通异常（非 ViralImportError）→ status FAILED、retryable=1（默认可重试）、
    error_code=VIRAL_IMPORT_FAILED、locked_by/locked_until 释放为 NULL。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")
    lease = _acquire("worker-a")
    assert lease is not None

    _fail(lease, RuntimeError("provider transport dropped"))

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "FAILED"
    assert int(row["retryable"]) == 1
    assert row["error_code"] == "VIRAL_IMPORT_FAILED"
    assert row["locked_by"] is None
    assert row["locked_until"] is None


def test_terminal_viral_import_error_marks_non_retryable_on_real_pg(pg_state: str) -> None:
    """终态 ViralImportError(retryable=False) → status FAILED、retryable=0（不再
    自动重试）、error_code 取异常携带的业务码。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")
    lease = _acquire("worker-a")
    assert lease is not None

    _fail(
        lease,
        ViralImportError(
            409, "VIRAL_IMPORT_PROJECT_CHANGED", "目标项目归属已变化。", retryable=False
        ),
    )

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "FAILED"
    assert int(row["retryable"]) == 0
    assert row["error_code"] == "VIRAL_IMPORT_PROJECT_CHANGED"
    assert row["locked_by"] is None


# ===========================================================================
# C. 栅栏令牌：陈旧租约的迟到失败不得覆盖已恢复状态
# ===========================================================================


def test_stale_viral_import_lease_failure_is_a_noop_on_real_pg(pg_state: str) -> None:
    """worker-a 认领后租约过期、被 worker-b 回收（attempt→2）。worker-a 迟到的
    fail 携带陈旧栅栏（locked_by=worker-a、attempt=1），其 WHERE 与库中现值
    （worker-b、attempt=2）不匹配 → 0 行更新（no-op），任务保持 worker-b 的
    RUNNING/attempt=2 不被覆盖。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1")

    stale_lease = _acquire("worker-a")
    assert stale_lease is not None
    assert stale_lease.attempt == 1

    # 租约过期 → worker-b 回收（attempt 2、locked_by worker-b）。
    _expire_lease(pg_state, "vi-1")
    fresh_lease = _acquire("worker-b")
    assert fresh_lease is not None
    assert fresh_lease.attempt == 2

    # 陈旧 worker-a 的迟到失败：栅栏不匹配 → no-op。
    _fail(stale_lease, RuntimeError("late failure from the stale worker"))

    row = _import_row(pg_state, "vi-1")
    assert row["status"] == "RUNNING", "陈旧失败不得覆盖已回收的 RUNNING 状态"
    assert row["locked_by"] == "worker-b"
    assert int(row["attempt"]) == 2


# ===========================================================================
# D. 幂等键按属主唯一（enqueue ON CONFLICT 重放去重所依赖的约束）
# ===========================================================================


def test_viral_import_idempotency_key_is_unique_per_owner_on_real_pg(pg_state: str) -> None:
    """``uq_viral_import_tasks_owner_idempotency``：同属主重复幂等键触发唯一违例
    （enqueue 的 ``ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING`` 重放
    去重即依赖它）；不同属主可复用同一幂等键（约束按属主范围）。"""
    _seed_base(pg_state)
    _seed_import_task(pg_state, task_id="vi-1", owner="u1", idempotency_key="shared-key")

    with pytest.raises(psycopg.IntegrityError):
        _seed_import_task(pg_state, task_id="vi-2", owner="u1", idempotency_key="shared-key")

    # 另一属主复用同一幂等键：约束是 (owner_user_id, idempotency_key) 复合唯一。
    _seed_import_task(pg_state, task_id="vi-3", owner="u2", idempotency_key="shared-key")

    assert cast(int, _one(pg_state, "SELECT count(*) FROM viral_import_tasks")) == 2


@pytest.mark.parametrize("operator_category", ["建房", "链接导入"])
def test_customer_resolution_preserves_operator_metadata_and_own_receipt(
    pg_state, monkeypatch, operator_category
):
    from contextlib import contextmanager
    from dataclasses import replace

    from app import usage_billing, viral_import_routes
    from app.auth import CurrentUser
    from app.storage import FakeStorageAdapter
    from app.viral_link import ResolvedViralLink
    from app.viral_store import upsert_viral_videos

    _seed_base(pg_state)
    resolved = ResolvedViralLink(
        platform="douyin",
        video_id="7672703482771972081",
        title="客户本次解析的标题",
        author="客户解析作者",
        cover_url="https://cdn.example/personal-cover.jpg",
        video_url="https://cdn.example/current-video.mp4",
        audio_url=None,
        duration_ms=6000,
        source_description="本次解析描述",
    )
    operator_video = replace(
        viral_import_routes._resolved_video(resolved),
        category=operator_category,
        title="运营已编辑的标题",
        author="运营作者",
        cover_url="https://cdn.example/operator-cover.jpg",
        cover_key="operator-cover.jpg",
        tags=["运营标签"],
        native={"source_description": "运营已维护描述"},
    )
    with pg_transaction() as raw:
        upsert_viral_videos(BusinessConnection.postgres(raw), [operator_video])
    _seed_operator_source(pg_state, resolved.video_id)
    _exec(
        pg_state,
        "UPDATE viral_videos SET collection_published=1,pushed_at=now(),homepage_featured=1",
    )
    before = _rows(pg_state, "SELECT * FROM viral_videos")[0]
    actor = CurrentUser(id="u1", username="u1", display_name="本人", role="employee")
    storage = FakeStorageAdapter(provider="cos", bucket="resolution")
    storage.put_object("operator-cover.jpg", b"operator-cover", content_type="image/jpeg")
    preflights = []
    cover_downloads = []

    class CustomerDb:
        @contextmanager
        def write(self):
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), actor

    monkeypatch.setattr(
        viral_import_routes,
        "douyidou_link_client_from_settings",
        lambda conn: SimpleNamespace(resolve=lambda url, purpose: resolved),
    )
    monkeypatch.setattr(viral_import_routes, "get_media_storage", lambda conn: storage)
    monkeypatch.setattr(
        viral_import_routes,
        "preflight_resolved_media",
        lambda item, **kwargs: preflights.append(item),
    )
    monkeypatch.setattr(
        viral_import_routes.UrlFetcher,
        "fetch",
        lambda self, url: cover_downloads.append(url) or b"new-cover",
    )
    for name in (
        "accept_operation",
        "begin_source_attempt",
        "complete_source_attempt",
        "finish_source",
    ):
        monkeypatch.setattr(usage_billing, name, lambda *args, **kwargs: None)

    response = viral_import_routes.resolve_viral_link(
        viral_import_routes.ViralLinkResolutionRequest(
            url=f"https://www.douyin.com/video/{resolved.video_id}", purpose="replica"
        ),
        CustomerDb(),
        "customer-current-result",
    )
    assert response.item.title == resolved.title
    assert response.item.coverUrl == resolved.cover_url
    assert preflights == [resolved]
    assert _rows(pg_state, "SELECT * FROM viral_videos")[0] == before
    receipt = _rows(pg_state, "SELECT * FROM viral_link_resolution_receipts")[0]
    assert receipt["owner_user_id"] == "u1" and receipt["status"] == "SUCCEEDED"
    assert json.loads(receipt["response_json"])["item"]["title"] == resolved.title
    assert {
        row["source_kind"]
        for row in _rows(pg_state, "SELECT source_kind FROM viral_content_sources")
    } == {"admin_link_import", "customer_import"}
    assert cover_downloads == []
    assert list(storage._objects) == ["operator-cover.jpg"]


@pytest.mark.parametrize("operator_owned", [False, True])
@pytest.mark.parametrize("purpose", ["replica", "copy"])
def test_customer_re_resolution_uses_own_latest_metadata_and_cached_media(
    pg_state, monkeypatch, operator_owned, purpose
):
    from contextlib import contextmanager
    from dataclasses import replace

    from app import usage_billing, viral_import_routes
    from app.auth import CurrentUser
    from app.storage import FakeStorageAdapter
    from app.viral_import import (
        ViralImportRequest,
        complete_viral_import_task,
        enqueue_viral_import_task,
        perform_viral_import_task,
        prepare_viral_import_task,
    )
    from app.viral_link import ResolvedViralLink
    from app.viral_media_preparation import ViralMediaPreparation
    from app.viral_store import upsert_viral_videos

    _seed_base(pg_state)
    _exec(
        pg_state,
        "INSERT INTO viral_runtime_controls(id,import_enabled) VALUES(1,1) "
        "ON CONFLICT(id) DO UPDATE SET import_enabled=1",
    )
    old = ResolvedViralLink(
        platform="douyin",
        video_id="7672703482771972081",
        title="首次解析标题",
        author="作者",
        cover_url=None,
        video_url="https://cdn.example/video.mp4",
        audio_url=None,
        duration_ms=0,
        source_description="首次尚未知晓时长",
    )
    current = replace(
        old,
        title="本人本次解析标题",
        duration_ms=6000,
        audio_url="https://cdn.example/audio.m4a" if purpose == "copy" else None,
    )
    if operator_owned:
        operator = replace(
            viral_import_routes._resolved_video(old), title="运营标题", category="建房"
        )
        with pg_transaction() as raw:
            upsert_viral_videos(BusinessConnection.postgres(raw), [operator])
        _seed_operator_source(pg_state, old.video_id)
        _exec(pg_state, "UPDATE viral_videos SET collection_published=1,pushed_at=now()")
    actor = CurrentUser(id="u1", username="u1", display_name="本人", role="employee")
    storage = FakeStorageAdapter(provider="cos", bucket="personal-re-resolution")
    resolutions = iter([old, current])

    class CustomerDb:
        @contextmanager
        def write(self):
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), actor

    def preflight(resolved, *, purpose, storage):
        kind = "audio" if purpose == "copy" and resolved.audio_url else "video"
        ViralMediaPreparation(storage=storage).fetch(
            platform=resolved.platform,
            video_id=resolved.video_id,
            kind=kind,
            prepare=lambda key, check: storage.put_object(
                key, f"cached-{kind}".encode(), content_type=f"{kind}/mp4"
            ),
        )

    def forbid_source(*args, **kwargs):
        raise AssertionError("个人任务只能读取已经预检归档的媒体")

    monkeypatch.setattr(
        viral_import_routes,
        "douyidou_link_client_from_settings",
        lambda conn: SimpleNamespace(resolve=lambda url, purpose: next(resolutions)),
    )
    monkeypatch.setattr(viral_import_routes, "get_media_storage", lambda conn: storage)
    monkeypatch.setattr(viral_import_routes, "preflight_resolved_media", preflight)
    monkeypatch.setattr(viral_import_routes.UrlFetcher, "iter_fetch", forbid_source)
    for name in (
        "accept_operation",
        "begin_source_attempt",
        "complete_source_attempt",
        "finish_source",
    ):
        monkeypatch.setattr(usage_billing, name, lambda *args, **kwargs: None)
    url = f"https://www.douyin.com/video/{old.video_id}"
    first = viral_import_routes.resolve_viral_link(
        viral_import_routes.ViralLinkResolutionRequest(url=url, purpose="copy"),
        CustomerDb(),
        "first-resolution",
    )
    assert first.item.durationMs == 0
    before = _rows(pg_state, "SELECT * FROM viral_videos")[0]
    if purpose == "copy" and not operator_owned:
        # 旧个人视频缓存已回收，最新解析只准备音频；全局行的 audio_url 仍为空。
        _exec(pg_state, "DELETE FROM viral_media_preparations WHERE media_kind='video'")
    latest = viral_import_routes.resolve_viral_link(
        viral_import_routes.ViralLinkResolutionRequest(url=url, purpose=purpose),
        CustomerDb(),
        "latest-resolution",
    )
    assert latest.item.durationMs == 6000 and latest.item.title == current.title
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(ViralImportError) as denied:
            enqueue_viral_import_task(
                conn,
                actor=CurrentUser(id="u2", username="u2", display_name="其他人", role="employee"),
                request=ViralImportRequest(
                    platform="douyin", videoId=old.video_id, purpose="replica"
                ),
                idempotency_key="must-not-borrow-other-receipt",
            )
        assert denied.value.status_code == (422 if operator_owned else 404)
        row = enqueue_viral_import_task(
            conn,
            actor=actor,
            request=ViralImportRequest(platform="douyin", videoId=old.video_id, purpose=purpose),
            idempotency_key=latest.import_idempotency_key,
        )
        task_id, project_id = str(row["id"]), str(row["project_id"])
        assert (
            conn.execute("SELECT name FROM projects WHERE id=%s", (project_id,)).fetchone()[0]
            == current.title
        )
    lease = _acquire("latest-receipt-worker")
    assert lease is not None and lease.id == task_id
    with pg_transaction() as raw:
        work = prepare_viral_import_task(
            BusinessConnection.postgres(raw), lease=lease, storage=storage
        )
    assert work.video.title == current.title and work.video.duration_ms == 6000
    outcome = perform_viral_import_task(work)
    assert outcome.duration_seconds == 6
    assert outcome.media_kind == ("audio" if purpose == "copy" else "video")
    with pg_transaction() as raw:
        complete_viral_import_task(BusinessConnection.postgres(raw), lease=lease, outcome=outcome)
    assert _import_row(pg_state, task_id)["status"] == "SUCCEEDED"
    assert (
        json.loads(_rows(pg_state, "SELECT metadata_json FROM assets")[0]["metadata_json"])[
            "duration_seconds"
        ]
        == 6
    )
    assert _rows(pg_state, "SELECT * FROM viral_videos")[0] == before


def test_customer_first_import_allows_later_admin_takeover_without_duplicate(pg_state):
    from dataclasses import replace

    from test_viral_media import _video

    from app.viral_store import upsert_viral_videos

    _seed_base(pg_state)
    private = replace(_video("douyin", "takeover"), category="链接导入", title="个人解析标题")
    with pg_transaction() as raw:
        inserted = upsert_viral_videos(
            BusinessConnection.postgres(raw), [private], update_existing=False
        )
    assert inserted == {("douyin", "takeover")}
    operator = replace(private, category="建房", title="运营确认标题", tags=["管理员标签"])
    with pg_transaction() as raw:
        assert upsert_viral_videos(BusinessConnection.postgres(raw), [operator]) == set()
    _seed_operator_source(pg_state, operator.video_id)
    before = _rows(pg_state, "SELECT * FROM viral_videos")[0]
    with pg_transaction() as raw:
        assert (
            upsert_viral_videos(BusinessConnection.postgres(raw), [private], update_existing=False)
            == set()
        )
    assert _rows(pg_state, "SELECT * FROM viral_videos") == [before]
    assert before["title"] == operator.title and before["category"] == "建房"


def test_customer_conflict_waits_for_uncommitted_operator_without_overwriting(pg_state):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Event
    from time import monotonic

    from test_viral_media import _video

    from app.viral_store import upsert_viral_videos

    _seed_base(pg_state)
    operator = replace(_video("douyin", "concurrent-import"), category="建房", title="后台标题")
    private = replace(operator, category="链接导入", title="个人标题")
    connected = Event()
    customer_pid = []

    def import_customer():
        with psycopg.connect(pg_state) as raw:
            customer_pid.append(raw.info.backend_pid)
            connected.set()
            return upsert_viral_videos(
                BusinessConnection.postgres(raw), [private], update_existing=False
            )

    with psycopg.connect(pg_state) as operator_raw, ThreadPoolExecutor(max_workers=1) as executor:
        upsert_viral_videos(BusinessConnection.postgres(operator_raw), [operator])
        pending = executor.submit(import_customer)
        try:
            assert connected.wait(5)
            deadline = monotonic() + 5
            while not _one(pg_state, "SELECT pg_blocking_pids(%s)", (customer_pid[0],)):
                assert monotonic() < deadline, "客户写入应等待管理员尚未提交的同视频行"
                Event().wait(0.01)
            # 运营来源在客户语句开始之后才写入，客户不能依赖该语句的旧快照保护元数据。
            operator_raw.execute(
                "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,keyword) "
                "VALUES('concurrent-admin','admin_link_import','douyin','concurrent-import','')"
            )
        finally:
            operator_raw.commit()
        assert pending.result(timeout=5) == set()
    row = _rows(pg_state, "SELECT * FROM viral_videos")[0]
    assert row["title"] == operator.title and row["category"] == operator.category
