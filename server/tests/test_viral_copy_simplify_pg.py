"""2026-11-11 爆款链路极简化验收：正文随任务直出 + UNCERTAIN 自动重开.

共享文案缓存（viral_script_cache）退役后两条不变量：

- 每个用户对同一条爆款视频**各自转写、各自付一次** ASR 按秒费；正文随本人任务
  ``result.text`` 直接下发，任务响应不再有 ``viral_source`` /
  ``copy_claim_required`` 字段（claim 交付端点只余 410 壳
  VIRAL_COPY_CLAIM_RETIRED）；``request_json.viral_source`` 保留为内容经营
  报表的爆款归因记录（服务端从 viral_import_tasks 关联写入）。
- SUBMISSION_UNCERTAIN（提交结果不可证明）不再有人工核对队列：超阈值自动重开
  新一轮计费；重开前旧轮 PENDING 计费单全部按失败收口（只退不收），重开次数
  封顶（reopen_count），耗尽落 FAILED ``*_AUTO_REOPEN_EXHAUSTED``；阈值=0 短路
  关闭。

TEST-PG：module 级建库（viral_copy_simplify_test）→ alembic head → 每用例
seed 清理（沿袭 test_viral_audio_shared_cache_pg.py 的夹具形状）。
"""

import hashlib
import json
import os
from collections.abc import Iterator

# Set HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-viral-copy-simplify-tests-minimum-48-bytes",
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

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection

DB_NAME = "viral_copy_simplify_test"
DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

_ASR_TEXT = "各自付费各自拿到的正文"


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


def _pg(dsn: str):
    """autocommit 直连（字典行）；断言型读取与种子写入都用参数绑定.

    BusinessConnection.postgres 会自行覆盖连接的 row_factory（_named_row_factory），
    所以这里固定 dict_row 不影响任何服务端调用。
    """
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _seed_base(dsn: str, users: tuple[str, ...]) -> None:
    """runtime_settings 单行 + 客户账号/钱包 + asr 资费（按秒 2 积分）."""
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
        pg.execute("DELETE FROM billing_tariffs WHERE service='asr'")
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES ('asr',true,2,1)"
        )


def _seed_viral_video(dsn: str, video_id: str) -> None:
    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
            "VALUES ('douyin', %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
            "DO UPDATE SET deleted_at=NULL",
            (video_id,),
        )


def _seed_shared_import(
    dsn: str,
    user: str,
    *,
    project_id: str,
    asset_id: str,
    video_id: str,
) -> None:
    """服务端导入形态的资产：全局 content_object + SUCCEEDED 导入行（归因凭据）."""
    from app.content_store import retain_content_object

    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES (%s, 'simplify', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (project_id, user),
        )
    sha = hashlib.sha256(b"video").hexdigest()
    with psycopg.connect(dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        obj, _ = retain_content_object(
            conn,
            sha256=sha,
            size_bytes=5,
            content_type="video/mp4",
            provider="fake",
            bucket="simplify",
            object_key="video-source.mp4",
            scope="global",
            pinned=True,
        )
    with _pg(dsn) as pg:
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, created_by_user_id, metadata_json, content_object_id) "
            "VALUES (%s, %s, 'reference_video', 'fake://simplify/video-source.mp4', %s, 5,"
            " 'video/mp4', %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                asset_id,
                project_id,
                sha,
                user,
                json.dumps({"duration_seconds": 12, "platform": "douyin", "video_id": video_id}),
                obj.id,
            ),
        )
        pg.execute(
            "INSERT INTO viral_import_tasks (id, owner_user_id, project_id, source_asset_id,"
            " platform, video_id, purpose, idempotency_key, request_hash, request_json, status) "
            "VALUES (%s, %s, %s, %s, 'douyin', %s, 'copy', %s, 'hash', '{}', 'SUCCEEDED') "
            "ON CONFLICT (id) DO NOTHING",
            (f"import-{asset_id}", user, project_id, asset_id, video_id, f"ik-{asset_id}"),
        )


def _audio_fixture(dsn: str, monkeypatch: pytest.MonkeyPatch, users: tuple[str, ...]) -> list[str]:
    """ASR 资费 + 假供应商 + 存储适配器：记录每次真实转写调用."""
    from app import script_from_audio as pipeline
    from app.asr import TranscriptResult
    from app.storage import FakeStorageAdapter

    _seed_base(dsn, users)
    calls: list[str] = []

    class Provider:
        name = "fake-simplify-asr"

        def transcribe(self, url: str, *, duration_sec: float | None = None) -> TranscriptResult:
            calls.append(url)
            return TranscriptResult(_ASR_TEXT, 12, "zh")

    monkeypatch.setattr(pipeline, "_configured_asr", lambda conn: Provider())
    monkeypatch.setattr(pipeline, "resolve_media_binary", lambda name: name)
    monkeypatch.setattr(
        pipeline, "extract_audio", lambda binary, source, dest: dest.write_bytes(b"audio")
    )
    monkeypatch.setattr(pipeline, "probe_duration_seconds", lambda binary, path: 12)
    storage = FakeStorageAdapter(provider="fake", bucket="simplify")
    storage.put_object("video-source.mp4", b"video", content_type="video/mp4")
    return calls


def _enqueue(
    dsn: str,
    user: str,
    *,
    project_id: str,
    asset_id: str,
    key: str,
) -> object:
    from app.script_from_audio import enqueue_script_from_audio_task

    with psycopg.connect(dsn) as raw:
        return enqueue_script_from_audio_task(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(user, user, user, "customer"),
            project_id=project_id,
            source_asset_id=asset_id,
            idempotency_key=key,
        )


def _run_worker(dsn: str, worker: str = "simplify-worker") -> None:
    """把队列跑空：任何用例遗留的 PENDING 任务都要被清掉，避免串场."""
    from app.generation_worker import _pg_audio_connection, _run_audio_lease
    from app.script_from_audio import acquire_script_from_audio_task
    from app.storage import FakeStorageAdapter

    storage = FakeStorageAdapter(provider="fake", bucket="simplify")
    storage.put_object("video-source.mp4", b"video", content_type="video/mp4")
    while True:
        with pg_transaction() as raw:
            lease = acquire_script_from_audio_task(
                BusinessConnection.postgres(raw), worker_id=worker
            )
        if lease is None:
            return
        _run_audio_lease(lease, storage=storage, connection=_pg_audio_connection)


def _task_row(dsn: str, task_id: str) -> dict:
    with _pg(dsn) as pg:
        row = pg.execute("SELECT * FROM script_from_audio_tasks WHERE id=%s", (task_id,)).fetchone()
    assert row is not None, task_id
    return dict(row)


def _asr_rounds(dsn: str, task_id: str) -> list[tuple[int, str, int]]:
    with _pg(dsn) as pg:
        rows = pg.execute(
            "SELECT billing_round, state, charged_credits FROM billing_operations "
            "WHERE service='asr' AND source_id=%s ORDER BY billing_round",
            (task_id,),
        ).fetchall()
    return [(int(r["billing_round"]), str(r["state"]), int(r["charged_credits"])) for r in rows]


def _mark_uncertain(dsn: str, task_id: str, *, minutes_ago: int = 40) -> None:
    """把任务手工摆成「提交结果不可证明」：provider 已受理但拿不到回执."""
    with _pg(dsn) as pg:
        pg.execute(
            "UPDATE script_from_audio_tasks SET status='SUBMISSION_UNCERTAIN',"
            "provider_started_at=now()::text,provider_task_id=NULL,"
            "completed_at=(now() - (%s * interval '1 minute'))::text,"
            "error_code=NULL,error_message_redacted=NULL,retryable=0,"
            "locked_by=NULL,locked_until=NULL,updated_at=now() WHERE id=%s",
            (minutes_ago, task_id),
        )


def _wallet(dsn: str, user: str) -> tuple[int, int]:
    with _pg(dsn) as pg:
        row = pg.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s",
            (user,),
        ).fetchone()
    if row is None:
        return (0, 0)
    return (int(row["available_credits"]), int(row["reserved_credits"]))


def test_viral_task_delivers_text_and_bills_each_account_once(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """爆款任务正文直出（契约 2026-11-11）：同视频两账号各自转写、各自扣一次."""
    from fastapi import HTTPException

    from app.script_from_audio import (
        ScriptFromAudioTaskResponse as ResponseModel,
    )
    from app.script_from_audio import script_from_audio_task_response
    from app.viral_routes import claim_viral_video_copy

    calls = _audio_fixture(pg_state, monkeypatch, ("simplify-a1", "simplify-b1"))
    video_id = "simplify-video-1"
    _seed_viral_video(pg_state, video_id)
    _seed_shared_import(
        pg_state,
        "simplify-a1",
        project_id="proj-simplify-a1",
        asset_id="asset-simplify-a1",
        video_id=video_id,
    )
    _seed_shared_import(
        pg_state,
        "simplify-b1",
        project_id="proj-simplify-b1",
        asset_id="asset-simplify-b1",
        video_id=video_id,
    )

    first = _enqueue(
        pg_state,
        "simplify-a1",
        project_id="proj-simplify-a1",
        asset_id="asset-simplify-a1",
        key="a1-first",
    )
    assert str(first["status"]) == "PENDING"
    _run_worker(pg_state)
    row = _task_row(pg_state, str(first["id"]))
    assert row["status"] == "SUCCEEDED"

    # GET 任务接口返回的就是 script_from_audio_task_response 的序列化结果
    # （两个读取端点原样下发该模型），据此断言正文直出与退役字段确实消失。
    response = script_from_audio_task_response(row)
    payload = response.model_dump(mode="json")
    assert response.status == "SUCCEEDED"
    assert payload["result"]["text"] == _ASR_TEXT
    assert payload["result"]["duration_sec"] == 12.0
    assert "viral_source" not in payload
    assert "copy_claim_required" not in payload
    # Schema 级护栏：字段从响应模型上删除，防止将来被加回。
    assert "viral_source" not in ResponseModel.model_fields
    assert "copy_claim_required" not in ResponseModel.model_fields
    # 归因记录保留在 request_json 里（服务端写入，供内容经营报表聚合）。
    request_payload = json.loads(str(row["request_json"]))
    assert request_payload["viral_source"] == ["douyin", video_id]

    # 第二个用户同视频再走一遍：各自建任务、各自真实转写、各自扣一次 ASR 按秒费。
    second = _enqueue(
        pg_state,
        "simplify-b1",
        project_id="proj-simplify-b1",
        asset_id="asset-simplify-b1",
        key="b1-first",
    )
    assert str(second["id"]) != str(first["id"])
    assert str(second["status"]) == "PENDING"
    _run_worker(pg_state)
    second_row = _task_row(pg_state, str(second["id"]))
    second_response = script_from_audio_task_response(second_row)
    assert second_response.status == "SUCCEEDED"
    assert second_response.result is not None and second_response.result.text == _ASR_TEXT
    assert len(calls) == 2
    assert _asr_rounds(pg_state, str(first["id"])) == [(1, "SUCCEEDED", 24)]
    assert _asr_rounds(pg_state, str(second["id"])) == [(1, "SUCCEEDED", 24)]
    assert _wallet(pg_state, "simplify-a1") == (976, 0)
    assert _wallet(pg_state, "simplify-b1") == (976, 0)

    # claim 交付端点只余 410 壳：旧客户端发起新提取时得到明确升级引导。
    with pytest.raises(HTTPException) as retired:
        claim_viral_video_copy()
    assert retired.value.status_code == 410
    assert retired.value.detail["code"] == "VIRAL_COPY_CLAIM_RETIRED"


def test_uncertain_auto_reopen_reopens_twice_then_exhausts(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈值 UNCERTAIN 自动重开：每轮收口旧单只退不收，封顶后落 FAILED."""
    from app.uncertain_auto_reopen import reopen_uncertain_script_from_audio_tasks

    # 入队前会 fail-fast 解析 ASR provider：与正文用例共用假供应商桩。
    _audio_fixture(pg_state, monkeypatch, ("simplify-reopen",))
    _seed_shared_import(
        pg_state,
        "simplify-reopen",
        project_id="proj-simplify-reopen",
        asset_id="asset-simplify-reopen",
        video_id="simplify-video-reopen",
    )
    task = _enqueue(
        pg_state,
        "simplify-reopen",
        project_id="proj-simplify-reopen",
        asset_id="asset-simplify-reopen",
        key="reopen-1",
    )
    task_id = str(task["id"])
    assert _asr_rounds(pg_state, task_id) == [(1, "PENDING", 0)]
    _mark_uncertain(pg_state, task_id)

    conn_dsn = pg_state
    with psycopg.connect(conn_dsn) as raw:
        outcome = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=30, max_reopens=2
        )
        raw.commit()
    assert outcome["reopened"] == 1
    row = _task_row(pg_state, task_id)
    assert row["status"] == "PENDING"
    assert int(row["reopen_count"]) == 1
    # 字段完整复位：新一轮从排队开始，不带上一轮的提交痕迹。
    assert row["provider_started_at"] is None
    assert row["provider_task_id"] is None
    assert row["completed_at"] is None
    # 旧轮（第 1 轮）按失败收口只退不收；新轮第 2 轮 PENDING 等待 worker。
    assert _asr_rounds(pg_state, task_id) == [(1, "FAILED", 0), (2, "PENDING", 0)]
    # 新一轮重新按资产时长预留（与首次受理同一口径），旧预留已原额退回。
    assert _wallet(pg_state, "simplify-reopen") == (976, 24)
    with _pg(pg_state) as pg:
        audit = pg.execute(
            "SELECT count(*) AS hits FROM audit_logs WHERE action='asr.auto_reopened' "
            "AND entity_type='script_from_audio_task' AND entity_id=%s",
            (task_id,),
        ).fetchone()["hits"]
    assert audit == 1

    # 第二次超阈值：reopen_count 1 < 2，允许再重开一轮。
    _mark_uncertain(pg_state, task_id)
    with psycopg.connect(conn_dsn) as raw:
        outcome = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=30, max_reopens=2
        )
        raw.commit()
    assert outcome["reopened"] == 1
    assert _task_row(pg_state, task_id)["status"] == "PENDING"
    assert int(_task_row(pg_state, task_id)["reopen_count"]) == 2
    assert _asr_rounds(pg_state, task_id) == [
        (1, "FAILED", 0),
        (2, "FAILED", 0),
        (3, "PENDING", 0),
    ]

    # 第三次：reopen_count 已达上限——不再重开，落 FAILED 终态并写批量审计。
    _mark_uncertain(pg_state, task_id)
    with psycopg.connect(conn_dsn) as raw:
        outcome = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=30, max_reopens=2
        )
        raw.commit()
    assert outcome["reopened"] == 0
    assert outcome["exhausted"] >= 1
    row = _task_row(pg_state, task_id)
    assert row["status"] == "FAILED"
    assert row["error_code"] == "SCRIPT_FROM_AUDIO_AUTO_REOPEN_EXHAUSTED"
    assert row["retryable"] in (0, False)
    # 计费单停在最新一轮 PENDING（终态失败没有新轮次，也没有双扣）；
    # 该轮预留仍在途（任务终态后由对账/重试路径处置，不在扫掠职责内）。
    assert _asr_rounds(pg_state, task_id) == [
        (1, "FAILED", 0),
        (2, "FAILED", 0),
        (3, "PENDING", 0),
    ]
    assert _wallet(pg_state, "simplify-reopen") == (976, 24)
    with _pg(pg_state) as pg:
        exhausted_audit = pg.execute(
            "SELECT count(*) AS hits FROM audit_logs "
            "WHERE action='script_from_audio.auto_reopen_exhausted'"
        ).fetchone()["hits"]
    assert exhausted_audit >= 1


def test_reopen_leaves_single_settled_round(pg_state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """重开收口杜绝双扣：全库只有最新轮一个 SUCCEEDED 结算，sum(charged) 一轮的钱."""
    from app.uncertain_auto_reopen import reopen_uncertain_script_from_audio_tasks
    from app.usage_billing import accept_operation

    calls = _audio_fixture(pg_state, monkeypatch, ("simplify-ledger",))
    _seed_shared_import(
        pg_state,
        "simplify-ledger",
        project_id="proj-simplify-ledger",
        asset_id="asset-simplify-ledger",
        video_id="simplify-video-ledger",
    )
    task = _enqueue(
        pg_state,
        "simplify-ledger",
        project_id="proj-simplify-ledger",
        asset_id="asset-simplify-ledger",
        key="ledger-1",
    )
    task_id = str(task["id"])
    # 复现「扫掠外的多轮遗留」：入队轮之外再手工挂一轮 PENDING（历史缺陷的形状）。
    with psycopg.connect(pg_state) as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="simplify-ledger",
            service="asr",
            source_id=task_id,
            units=12,
            billing_round=2,
        )
        raw.commit()
    assert _wallet(pg_state, "simplify-ledger") == (952, 48)
    _mark_uncertain(pg_state, task_id)

    with psycopg.connect(pg_state) as raw:
        outcome = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=30, max_reopens=2
        )
        raw.commit()
    assert outcome["reopened"] == 1
    # 全部旧轮 PENDING 按失败释放（只退不收），只有最新一轮等待 worker 结算。
    assert _asr_rounds(pg_state, task_id) == [
        (1, "FAILED", 0),
        (2, "FAILED", 0),
        (3, "PENDING", 0),
    ]
    assert _wallet(pg_state, "simplify-ledger") == (976, 24)

    _run_worker(pg_state)
    row = _task_row(pg_state, task_id)
    assert row["status"] == "SUCCEEDED"
    rounds = _asr_rounds(pg_state, task_id)
    assert rounds == [(1, "FAILED", 0), (2, "FAILED", 0), (3, "SUCCEEDED", 24)]
    assert sum(charged for _, _, charged in rounds) == 24
    assert sum(1 for _, state, _ in rounds if state == "SUCCEEDED") == 1
    assert len(calls) == 1
    assert _wallet(pg_state, "simplify-ledger") == (976, 0)


def test_rewrite_uncertain_auto_reopen_symmetry(pg_state: str) -> None:
    """改写任务与转写同一套自动重开：units=1 计费、封顶耗尽落 FAILED."""
    from app.uncertain_auto_reopen import reopen_uncertain_script_rewrite_tasks
    from app.usage_billing import accept_operation

    _seed_base(pg_state, ("simplify-rewrite",))
    with _pg(pg_state) as pg:
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES ('rewrite',true,1,1)"
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) "
            "VALUES ('proj-simplify-rewrite', 'simplify-rw', 'simplify-rewrite') "
            "ON CONFLICT (id) DO NOTHING"
        )
        pg.execute(
            "INSERT INTO script_rewrite_tasks ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_json, status"
            ") VALUES ('rw-simplify', 'proj-simplify-rewrite', 'simplify-rewrite',"
            " 'ik-rw-simplify', 'hash-rw', '{}', 'PENDING')"
        )
    with psycopg.connect(pg_state) as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="simplify-rewrite",
            service="rewrite",
            source_id="rw-simplify",
            units=1,
        )
        raw.commit()
    # 项目行已建（候选查询按 project_id 找活跃兄弟）。改写表没有 provider_task_id
    # 列（provider 只在 ASR 侧受理），UNCERTAIN 只摆 provider_started_at。
    with _pg(pg_state) as pg:
        pg.execute(
            "UPDATE script_rewrite_tasks SET status='SUBMISSION_UNCERTAIN',"
            "provider_started_at=now()::text,"
            "completed_at=(now() - interval '40 minutes')::text,"
            "locked_by=NULL,locked_until=NULL WHERE id='rw-simplify'"
        )

    with psycopg.connect(pg_state) as raw:
        outcome = reopen_uncertain_script_rewrite_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=30, max_reopens=2
        )
        raw.commit()
    assert outcome["reopened"] == 1
    with _pg(pg_state) as pg:
        row = pg.execute(
            "SELECT status, reopen_count FROM script_rewrite_tasks WHERE id='rw-simplify'"
        ).fetchone()
        assert row is not None
        assert (str(row["status"]), int(row["reopen_count"])) == ("PENDING", 1)
        rounds = pg.execute(
            "SELECT billing_round, state, charged_credits FROM billing_operations "
            "WHERE service='rewrite' AND source_id='rw-simplify' ORDER BY billing_round"
        ).fetchall()
        assert [
            (int(r["billing_round"]), str(r["state"]), int(r["charged_credits"])) for r in rounds
        ] == [
            (1, "FAILED", 0),
            (2, "PENDING", 0),
        ]
        audit = pg.execute(
            "SELECT count(*) AS hits FROM audit_logs WHERE action='rewrite.auto_reopened' "
            "AND entity_type='script_rewrite_task' AND entity_id='rw-simplify'"
        ).fetchone()["hits"]
    assert audit == 1

    # 耗尽：把任务再次摆回 UNCERTAIN 并顶满 reopen_count。
    with _pg(pg_state) as pg:
        pg.execute(
            "UPDATE script_rewrite_tasks SET status='SUBMISSION_UNCERTAIN',"
            "completed_at=(now() - interval '40 minutes')::text,reopen_count=2,"
            "locked_by=NULL,locked_until=NULL WHERE id='rw-simplify'"
        )
    with psycopg.connect(pg_state) as raw:
        outcome = reopen_uncertain_script_rewrite_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=30, max_reopens=2
        )
        raw.commit()
    assert outcome["reopened"] == 0
    assert outcome["exhausted"] >= 1
    with _pg(pg_state) as pg:
        row = pg.execute(
            "SELECT status, error_code FROM script_rewrite_tasks WHERE id='rw-simplify'"
        ).fetchone()
        assert row is not None
        assert (str(row["status"]), str(row["error_code"])) == (
            "FAILED",
            "SCRIPT_REWRITE_AUTO_REOPEN_EXHAUSTED",
        )


def test_zero_threshold_disables_auto_reopen(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阈值=0（关闭自动重开）：扫掠直接短路，UNCERTAIN 原样保留."""
    from app.uncertain_auto_reopen import reopen_uncertain_script_from_audio_tasks

    # 入队前会 fail-fast 解析 ASR provider：与正文用例共用假供应商桩。
    _audio_fixture(pg_state, monkeypatch, ("simplify-zero",))
    _seed_shared_import(
        pg_state,
        "simplify-zero",
        project_id="proj-simplify-zero",
        asset_id="asset-simplify-zero",
        video_id="simplify-video-zero",
    )
    task = _enqueue(
        pg_state,
        "simplify-zero",
        project_id="proj-simplify-zero",
        asset_id="asset-simplify-zero",
        key="zero-1",
    )
    task_id = str(task["id"])
    _mark_uncertain(pg_state, task_id)
    with _pg(pg_state) as pg:
        pg.execute("UPDATE runtime_settings SET uncertain_auto_reopen_minutes=0 WHERE id=1")

    with psycopg.connect(pg_state) as raw:
        outcome = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(raw), threshold_minutes=0, max_reopens=2
        )
        raw.commit()
    # 短路：不动作、无新轮、无审计，任务保持 UNCERTAIN。
    assert outcome == {"reopened": 0, "deferred": 0, "exhausted": 0}
    row = _task_row(pg_state, task_id)
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert int(row["reopen_count"]) == 0
    assert _asr_rounds(pg_state, task_id) == [(1, "PENDING", 0)]
    with _pg(pg_state) as pg:
        audit = pg.execute(
            "SELECT count(*) AS hits FROM audit_logs "
            "WHERE action LIKE '%%auto_reopen%%' AND entity_id=%s",
            (task_id,),
        ).fetchone()["hits"]
    assert audit == 0
    # 本用例故意遗留一条 UNCERTAIN（别的用例断言都按自己的 task/user 圈定），
    # 收尾置 FAILED 防止它的 completed_at 落进后续扫掠窗口。
    with _pg(pg_state) as pg:
        pg.execute(
            "UPDATE script_from_audio_tasks SET status='FAILED',"
            "error_code='TEST_NEUTRALIZED' WHERE id=%s",
            (task_id,),
        )


def test_legacy_cache_hit_task_never_leaks_free_text(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史「缓存命中秒回」任务不下发正文，也不进提取记录（Codex 阻塞 2）.

    旧链路在共享缓存命中时不收 ASR 费，正文靠 claim 闸门收费后才交付；claim
    已退役为 410。若这类任务的 request_json 带 ``viral_archive_sha256`` 的
    正文仍随响应直出，等于对未付费产出开放免费正文。
    """
    from app.script_from_audio import script_from_audio_task_response
    from app.viral_routes import list_viral_copy_extractions

    _seed_base(pg_state, ("legacy-owner",))
    _seed_viral_video(pg_state, "legacy-hit-1")
    _seed_shared_import(
        pg_state,
        "legacy-owner",
        project_id="proj-legacy",
        asset_id="asset-legacy",
        video_id="legacy-hit-1",
    )
    with _pg(pg_state) as pg:
        pg.execute(
            "INSERT INTO script_from_audio_tasks ("
            "id, project_id, source_asset_id, created_by_user_id, idempotency_key,"
            " request_hash, request_json, status, result_json, completed_at)"
            " VALUES ('legacy-task', 'proj-legacy', 'asset-legacy', 'legacy-owner',"
            " 'ik-legacy', 'rh-legacy', %s, 'SUCCEEDED', %s, now()::text)",
            (
                json.dumps(
                    {
                        "source_asset_id": "asset-legacy",
                        "viral_source": ["douyin", "legacy-hit-1"],
                        "viral_archive_sha256": "a" * 64,
                        "viral_archive_uri": "fake://simplify/video-source.mp4",
                        "viral_archive_kind": "video",
                    }
                ),
                json.dumps({"text": "旧共享缓存的正文", "duration_sec": 12.0, "language": "zh"}),
            ),
        )
        row = pg.execute("SELECT * FROM script_from_audio_tasks WHERE id='legacy-task'").fetchone()
    assert row is not None
    response = script_from_audio_task_response(row)
    assert response.result is not None
    assert response.result.text is None
    # 提取记录同样不展示这条（未付费正文不得经由历史接口回流）。
    with _pg(pg_state) as pg:
        items = list_viral_copy_extractions(pg, CurrentUser("legacy-owner", "u", "u", "customer"))
    assert all(item.videoId != "legacy-hit-1" for item in items.items)


def test_reopen_defers_on_insufficient_credits_without_state_change(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """余额不足时重开只延期不留痕：状态保持 UNCERTAIN、计数不增、落 deferred 审计.

    accept_operation 对预留>0 且余额不足抛 402；扫掠必须只回滚本候选的
    SAVEPOINT 并延期（locked_until 顶到阈值处），下一轮再试（Codex 阻塞 2）。
    """
    from app.uncertain_auto_reopen import reopen_uncertain_script_from_audio_tasks

    _audio_fixture(pg_state, monkeypatch, ("insufficient-owner",))
    _seed_viral_video(pg_state, "defer-1")
    _seed_shared_import(
        pg_state,
        "insufficient-owner",
        project_id="proj-defer",
        asset_id="asset-defer",
        video_id="defer-1",
    )
    task = _enqueue(
        pg_state,
        "insufficient-owner",
        project_id="proj-defer",
        asset_id="asset-defer",
        key="ik-defer",
    )
    task_id = str(task["id"])
    _mark_uncertain(pg_state, task_id)
    # 模拟「对账已按失败释放旧轮 + 钱包被别的消费掏空」：用既有结算 API 正确
    # 释放（手动 UPDATE 会撞 billing_operations 的 CHECK 约束），再清零余额。
    from app.usage_billing import finish_source

    with psycopg.connect(pg_state) as raw:
        conn = BusinessConnection.postgres(raw)
        finish_source(conn, task_id, units=0, succeeded=False)
        conn.commit()
    with _pg(pg_state) as pg:
        pg.execute(
            "UPDATE wallets SET available_credits=0,reserved_credits=0 WHERE user_id=%s",
            ("insufficient-owner",),
        )
    # reopen 走真实事务：上下文管理器退出即提交（try/finally close 会回滚扫掠）。
    with psycopg.connect(pg_state, row_factory=dict_row) as conn:
        result = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(conn), threshold_minutes=30, max_reopens=2
        )
    assert result["deferred"] == 1
    assert result["reopened"] == 0
    row = _task_row(pg_state, task_id)
    assert str(row["status"]) == "SUBMISSION_UNCERTAIN"
    assert int(row["reopen_count"]) == 0
    assert row["locked_until"] is not None
    with _pg(pg_state) as pg:
        deferred = pg.execute(
            "SELECT count(*) AS hits FROM audit_logs "
            "WHERE action='asr.auto_reopen_deferred' AND entity_id=%s",
            (task_id,),
        ).fetchone()
    assert int(deferred["hits"]) == 1
    # 收尾中和，避免延期时间窗影响后续用例的扫掠候选集。
    with _pg(pg_state) as pg:
        pg.execute(
            "UPDATE script_from_audio_tasks SET status='FAILED',"
            "error_code='TEST_NEUTRALIZED' WHERE id=%s",
            (task_id,),
        )


def test_reopen_closes_stale_attempts_as_unknown(
    pg_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧轮未收口的供应商调用按 UNKNOWN 收口——不虚构成本，也绝不记成零成本.

    Codex 阻塞 1 的回归：重开前必须把该任务全部 PENDING 计费单按失败释放，
    关联的 PENDING attempt 置 UNKNOWN（cost_fen 留 NULL），否则遗留旧轮会按
    新任务的 SUCCEEDED 再结算一次（双扣）。
    """
    from app.uncertain_auto_reopen import reopen_uncertain_script_from_audio_tasks

    _audio_fixture(pg_state, monkeypatch, ("unknown-attempt-owner",))
    _seed_viral_video(pg_state, "unknown-attempt-1")
    _seed_shared_import(
        pg_state,
        "unknown-attempt-owner",
        project_id="proj-unknown",
        asset_id="asset-unknown",
        video_id="unknown-attempt-1",
    )
    task = _enqueue(
        pg_state,
        "unknown-attempt-owner",
        project_id="proj-unknown",
        asset_id="asset-unknown",
        key="ik-unknown",
    )
    task_id = str(task["id"])
    _mark_uncertain(pg_state, task_id)
    # 直插一条 PENDING attempt 构造「旧轮未收口的供应商调用」：重开必须把它
    # 置 UNKNOWN（cost_fen 留 NULL 不虚构成本），而不是记成零成本。
    with _pg(pg_state) as pg:
        pg.execute(
            "INSERT INTO billing_attempts("
            "id, operation_id, attempt_key, service, provider, unit, unit_cost_fen) "
            "SELECT 'stale-attempt', id, 'stale-key', 'asr', 'fake', 'second', 1 "
            "FROM billing_operations WHERE service='asr' AND source_id=%s",
            (task_id,),
        )
    # reopen 走真实事务：上下文管理器退出即提交。
    with psycopg.connect(pg_state, row_factory=dict_row) as conn:
        result = reopen_uncertain_script_from_audio_tasks(
            BusinessConnection.postgres(conn), threshold_minutes=30, max_reopens=2
        )
    assert result["reopened"] == 1
    with _pg(pg_state) as pg:
        attempts = pg.execute(
            "SELECT a.state, a.cost_fen FROM billing_attempts a "
            "JOIN billing_operations o ON o.id=a.operation_id "
            "WHERE o.source_id=%s",
            (task_id,),
        ).fetchall()
    # 旧轮的 attempt 已被 UNKNOWN 收口，且没有杜撰成本。
    stale = [a for a in attempts if a["state"] == "UNKNOWN"]
    assert stale, attempts
    assert all(a["cost_fen"] is None for a in stale)
    # 单轮结算防线：旧轮计费单全部失败释放、新轮 PENDING 恰好 1 条。
    rounds = _asr_rounds(pg_state, task_id)
    assert sum(1 for _, state, _ in rounds if state == "PENDING") == 1
    assert all(state != "SUCCEEDED" for _, state, _ in rounds)
