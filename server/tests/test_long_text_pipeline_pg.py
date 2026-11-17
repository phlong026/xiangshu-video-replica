"""长任务恢复、计费与长文档的真库回归，不调用真实付费服务。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import create_test_database, drop_test_database, upgrade_test_database_to_head

from app.asr import (
    AsrConfiguration,
    AsrSubmissionUncertain,
    DashScopeFunAsr,
    TranscriptResult,
    TranscriptSpan,
)
from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.script_from_audio import acquire_script_from_audio_task
from app.script_rewrite import ScriptRewriteRequest, acquire_script_rewrite_task
from app.segmented_asr import run_segmented_audio, segment_plan
from app.segmented_rewrite import enqueue_long_rewrite, run_segmented_rewrite, split_text
from app.text_documents import (
    EditBlock,
    create_document,
    document_text,
    edit_block,
    owned_document,
    snapshot_document,
)
from app.usage_billing import accept_operation


@pytest.fixture(scope="module")
def pipeline_dsn():
    dsn = create_test_database("long_text_pipeline_test")
    upgrade_test_database_to_head(dsn)
    yield dsn
    drop_test_database("long_text_pipeline_test")


@pytest.fixture()
def pipeline(pipeline_dsn):
    uid, project, task = (str(uuid4()) for _ in range(3))

    @contextmanager
    def connection():
        with psycopg.connect(pipeline_dsn) as raw:
            yield BusinessConnection.postgres(raw)

    with connection() as conn:
        conn.execute(
            "INSERT INTO users(id,username,display_name,role) VALUES (%s,%s,'测试','employee')",
            (uid, uid),
        )
        conn.execute(
            "INSERT INTO wallets(user_id,available_credits,reserved_credits) VALUES (%s,10000,0)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO projects(id,name,owner_user_id) VALUES (%s,'长文案测试',%s)",
            (project, uid),
        )
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) VALUES ('asr',true,1,2) "
            "ON CONFLICT(service) DO UPDATE SET enabled=true,unit_credits=1,unit_cost_fen=2"
        )
        conn.execute(
            "INSERT INTO script_from_audio_tasks(id,project_id,source_asset_id,created_by_user_id,idempotency_key,"
            "request_hash,request_json,status,processing_version) VALUES (%s,%s,'source',%s,%s,'hash','{}','PENDING','segmented-v1')",
            (task, project, uid, task),
        )
        for index, times in enumerate(segment_plan(1200)):
            conn.execute(
                "INSERT INTO asr_segments(task_id,segment_index,core_start,core_end,input_start,input_end,status,object_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,'READY',%s)",
                (task, index, *times, f"projects/{project}/asr/{task}/{index}.m4a"),
            )
        accept_operation(conn, user_id=uid, service="asr", source_id=task, units=1200)
    actor = CurrentUser(id=uid, username=uid, display_name="测试", role="employee")
    return SimpleNamespace(connection=connection, user=uid, project=project, task=task, actor=actor)


class ReceiptedProvider(DashScopeFunAsr):
    def __init__(self, *, uncertain=False):
        super().__init__(
            AsrConfiguration("https://asr.example", "test-key", "asr", "flash", 300, 0, 1)
        )
        self.submissions = []
        self.polls = []
        self.uncertain = uncertain

    def submit_segment(self, url):
        self.submissions.append(url)
        if self.uncertain:
            raise AsrSubmissionUncertain("中断")
        return f"receipt-{len(self.submissions)}"

    def poll_segment(self, task_id):
        self.polls.append(task_id)
        return TranscriptResult(
            "同一句真实重复。", 601, "zh", (TranscriptSpan("同一句真实重复。", 10, 11),)
        )


def step(pipeline, monkeypatch, provider):
    monkeypatch.setattr("app.segmented_asr._configured_asr", lambda conn: provider)
    with pipeline.connection() as conn:
        conn.execute(
            "UPDATE script_from_audio_tasks SET next_attempt_at=NULL WHERE id=%s", (pipeline.task,)
        )
        conn.commit()
        lease = acquire_script_from_audio_task(conn, worker_id="worker-after-restart")
    assert lease is not None
    storage = SimpleNamespace(
        create_download_intent=lambda *args, **kwargs: SimpleNamespace(
            url="https://media.example/segment"
        )
    )
    run_segmented_audio(lease, connection=pipeline.connection, storage=storage)


def test_restart_polls_receipts_and_bills_unique_core_once(pipeline, monkeypatch):
    provider = ReceiptedProvider()
    for _ in range(4):
        step(pipeline, monkeypatch, provider)
    assert len(provider.submissions) == 2
    assert provider.polls == ["receipt-1", "receipt-2"]
    with pipeline.connection() as conn:
        row = conn.execute(
            "SELECT * FROM script_from_audio_tasks WHERE id=%s", (pipeline.task,)
        ).fetchone()
        assert row["status"] == "SUCCEEDED"
        result = json.loads(row["result_json"])
        assert result["duration_sec"] == 1200
        assert document_text(conn, result["document"]["id"]) == "同一句真实重复。\n同一句真实重复。"
        wallet = conn.execute(
            "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s",
            (pipeline.user,),
        ).fetchone()
        assert tuple(wallet) == (8800, 0)
        attempts = conn.execute(
            "SELECT a.usage FROM billing_attempts a JOIN billing_operations o ON o.id=a.operation_id WHERE o.source_id=%s",
            (pipeline.task,),
        ).fetchall()
        assert sum(row[0] for row in attempts) == 1202


def test_cancel_drains_receipt_preserves_cost_and_refunds_parent(pipeline, monkeypatch):
    provider = ReceiptedProvider()
    step(pipeline, monkeypatch, provider)
    with pipeline.connection() as conn:
        conn.execute(
            "UPDATE script_from_audio_tasks SET cancel_requested_at=now() WHERE id=%s",
            (pipeline.task,),
        )
    step(pipeline, monkeypatch, provider)
    assert len(provider.submissions) == 1
    assert provider.polls == ["receipt-1"]
    with pipeline.connection() as conn:
        assert (
            conn.execute(
                "SELECT status FROM script_from_audio_tasks WHERE id=%s", (pipeline.task,)
            ).fetchone()[0]
            == "FAILED"
        )
        assert (
            conn.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s",
                (pipeline.user,),
            ).fetchone()[0]
            == 10000
        )
        assert (
            conn.execute(
                "SELECT usage FROM billing_attempts WHERE operation_id=(SELECT id FROM billing_operations WHERE source_id=%s)",
                (pipeline.task,),
            ).fetchone()[0]
            == 601
        )


def test_uncertain_segment_does_not_submit_immediately_and_reopens_only_it(pipeline, monkeypatch):
    provider = ReceiptedProvider(uncertain=True)
    step(pipeline, monkeypatch, provider)
    step(pipeline, monkeypatch, provider)
    assert len(provider.submissions) <= 2
    with pipeline.connection() as conn:
        rows = conn.execute(
            "SELECT segment_index,status FROM asr_segments WHERE task_id=%s ORDER BY segment_index",
            (pipeline.task,),
        ).fetchall()
        assert rows[0]["status"] == "UNCERTAIN"
        assert (
            conn.execute(
                "SELECT reopen_count FROM script_from_audio_tasks WHERE id=%s", (pipeline.task,)
            ).fetchone()[0]
            == 0
        )
        conn.execute(
            "UPDATE asr_segments SET submitted_at=now()-interval '31 minutes' WHERE task_id=%s AND segment_index=0",
            (pipeline.task,),
        )
    step(pipeline, monkeypatch, provider)
    with pipeline.connection() as conn:
        assert (
            conn.execute(
                "SELECT reopen_count FROM script_from_audio_tasks WHERE id=%s", (pipeline.task,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM billing_operations WHERE source_id=%s", (pipeline.task,)
            ).fetchone()[0]
            == 1
        )


def test_document_snapshot_is_idempotent_and_owner_scoped(pipeline):
    with pipeline.connection() as conn:
        document = create_document(
            conn,
            project_id=pipeline.project,
            owner_user_id=pipeline.user,
            text="内容" * 15000,
            immutable=False,
        )
        first = snapshot_document(conn, pipeline.actor, document.id, document.revision)
        assert snapshot_document(conn, pipeline.actor, document.id, document.revision) == first
        assert len(document_text(conn, first.id)) == 30000
        other = CurrentUser(id="other", username="other", display_name="其他", role="employee")
        with pytest.raises(HTTPException) as error:
            owned_document(conn, other, document.id)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            snapshot_document(conn, pipeline.actor, document.id, document.revision + 1)
        assert error.value.status_code == 409


def test_edit_revision_conflict_preserves_snapshot_and_latest_content(pipeline):
    with pipeline.connection() as conn:
        reference = create_document(
            conn,
            project_id=pipeline.project,
            owner_user_id=pipeline.user,
            text="原文" * 5000,
            immutable=False,
        )
        frozen = snapshot_document(conn, pipeline.actor, reference.id, reference.revision)

        @contextmanager
        def write():
            yield conn, pipeline.actor

        db = SimpleNamespace(write=write)
        saved = edit_block(reference.id, 0, EditBlock(revision=1, text="第一位编辑的修改"), db)
        assert saved.revision == 2
        with pytest.raises(HTTPException) as error:
            edit_block(reference.id, 0, EditBlock(revision=1, text="过期的覆盖"), db)
        assert error.value.status_code == 409
        assert document_text(conn, reference.id).startswith("第一位编辑的修改")
        assert document_text(conn, frozen.id) == "原文" * 5000


def enqueue_rewrite(pipeline, monkeypatch, *, target_words=None):
    monkeypatch.setenv("VIDEO_REPLICA_SEGMENTED_REWRITE_ENABLED", "1")
    monkeypatch.setattr(
        "app.segmented_rewrite.load_script_rewrite_configuration",
        lambda conn: ("https://text.example", "test-key", "model"),
    )
    with pipeline.connection() as conn:
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) VALUES ('rewrite',true,5,2) "
            "ON CONFLICT(service) DO UPDATE SET enabled=true,unit_credits=5,unit_cost_fen=2"
        )
        document = create_document(
            conn,
            project_id=pipeline.project,
            owner_user_id=pipeline.user,
            text="房屋预算100万元。" * 2500,
        )
        row = enqueue_long_rewrite(
            conn,
            actor=pipeline.actor,
            project_id=pipeline.project,
            request=ScriptRewriteRequest(
                document_id=document.id, document_revision=1, target_words=target_words
            ),
            idempotency_key="rewrite-test-key",
        )
        return str(row["id"]), document


def rewrite_step(pipeline, task_id):
    with pipeline.connection() as conn:
        conn.execute("UPDATE script_rewrite_tasks SET next_attempt_at=NULL WHERE id=%s", (task_id,))
        conn.commit()
        lease = acquire_script_rewrite_task(conn, worker_id="rewrite-after-restart")
    assert lease is not None
    assert lease.id == task_id
    run_segmented_rewrite(lease, connection=pipeline.connection)


def test_long_rewrite_checkpoints_parts_and_bills_parent_once(pipeline, monkeypatch):
    task_id, document = enqueue_rewrite(pipeline, monkeypatch)
    calls = []

    def rewrite(**kwargs):
        calls.append(kwargs["source_text"])
        return kwargs["source_text"]

    monkeypatch.setattr("app.segmented_rewrite.request_deepseek_text", rewrite)
    for _ in range(40):
        rewrite_step(pipeline, task_id)
        with pipeline.connection() as conn:
            row = conn.execute(
                "SELECT * FROM script_rewrite_tasks WHERE id=%s", (task_id,)
            ).fetchone()
            if row["status"] == "SUCCEEDED":
                result = json.loads(row["result_json"])
                assert len(result["rewritten_text"]) <= 2000
                assert result["document"]["char_count"] >= document.char_count
                operation = conn.execute(
                    "SELECT * FROM billing_operations WHERE source_id=%s", (task_id,)
                ).fetchone()
                assert operation["state"] == "SUCCEEDED"
                assert (
                    conn.execute(
                        "SELECT count(*) FROM billing_operations WHERE source_id=%s", (task_id,)
                    ).fetchone()[0]
                    == 1
                )
                break
    else:
        pytest.fail("长文案改写未完成")
    assert len(calls) > 1
    assert all(len(part.encode("utf-8")) <= 6000 for part in calls)
    assert "".join(calls) == "房屋预算100万元。" * 2500


def test_condense_reduces_bounded_outlines_before_final_call(pipeline, monkeypatch):
    task_id, _ = enqueue_rewrite(pipeline, monkeypatch, target_words=200)
    kinds = []

    def rewrite(**kwargs):
        final = "完整口播稿" in kwargs["instructions"]
        kinds.append("final" if final else "summary")
        assert len(kwargs["source_text"].encode("utf-8")) <= 6000
        return "预算100万元，须按实际设计核算。" if final else "预算100万元。"

    monkeypatch.setattr("app.segmented_rewrite.request_deepseek_text", rewrite)
    for _ in range(50):
        rewrite_step(pipeline, task_id)
        with pipeline.connection() as conn:
            row = conn.execute(
                "SELECT status,result_json FROM script_rewrite_tasks WHERE id=%s", (task_id,)
            ).fetchone()
            if row["status"] == "SUCCEEDED":
                assert "100万元" in json.loads(row["result_json"])["rewritten_text"]
                break
    else:
        pytest.fail("缩写任务未完成")
    assert kinds[-1] == "final"
    assert kinds.count("final") == 1
    assert kinds.count("summary") > 1


def test_byte_budget_never_splits_unicode_or_loses_real_repeats():
    text = "预算100万元。\n" * 10000 + "🏡" * 100
    parts = split_text(text)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-8")) <= 6000 for part in parts)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), 0, -1, 43201])
def test_invalid_segment_duration_fails_closed(duration):
    with pytest.raises(ValueError):
        segment_plan(duration)
