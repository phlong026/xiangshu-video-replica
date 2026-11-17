"""统一来源动作按服务端事实派发，重放不重复创建付费任务。"""

import json

import pytest
from fastapi import HTTPException
from test_project_sources_pg import source_conn, source_dsn, owner

from app.project_sources import CreateProjectSource, BindProjectSource, create_project_source, bind_project_source
from app.source_analysis import SourceAnalysisRequest, dispatch_source_analysis


def prepared_source(conn, duration):
    conn.execute("INSERT INTO wallets(user_id,available_credits,reserved_credits) VALUES ('owner',10000,0)")
    conn.execute("INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) VALUES ('analysis',true,5,1) ON CONFLICT DO NOTHING")
    conn.execute("UPDATE assets SET metadata_json=%s WHERE id='video'", (json.dumps({"duration_seconds": duration, "upload_status": "COMPLETE"}),))
    return create_project_source(conn, actor=owner(), project_id="p", request=CreateProjectSource(filename="原片.mp4", size_bytes=1024, duration_seconds=duration, idempotency_key="source-action-key"))


@pytest.mark.parametrize("duration,route", [(30, "replica"), (30.1, "replica"), (30.11, "replica_prompt"), (180.1, "replica_prompt")])
def test_server_routes_by_verified_duration_and_reuses_one_task(source_conn, duration, route):
    source = prepared_source(source_conn, duration)
    bound = bind_project_source(source_conn, actor=owner(), project_id="p", source_id=source["id"], request=BindProjectSource(asset_id="video", expected_revision=1))
    request = SourceAnalysisRequest(source_id=source["id"], source_revision=bound["revision"], idempotency_key="analyze-action-key")
    first = dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=request)
    assert first.status == "DISPATCHED"
    assert first.route == route
    assert dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=request) == first
    second_key = request.model_copy(update={"idempotency_key": "another-action-key"})
    assert dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=second_key).task_id == first.task_id
    assert source_conn.execute("SELECT count(*) FROM billing_operations WHERE service='analysis'").fetchone()[0] == 1


def test_needs_upload_action_continues_after_same_source_binding(source_conn):
    source = prepared_source(source_conn, 60)
    request = SourceAnalysisRequest(source_id=source["id"], source_revision=1, idempotency_key="before-upload-key")
    waiting = dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=request)
    assert waiting.status == "NEEDS_ORIGINAL_UPLOAD"
    assert source_conn.execute("SELECT count(*) FROM billing_operations").fetchone()[0] == 0
    bind_project_source(source_conn, actor=owner(), project_id="p", source_id=source["id"], request=BindProjectSource(asset_id="video", expected_revision=1))
    assert dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=request).status == "DISPATCHED"


def test_over_180_requires_range_without_creating_bill(source_conn):
    source = prepared_source(source_conn, 600)
    bound = bind_project_source(source_conn, actor=owner(), project_id="p", source_id=source["id"], request=BindProjectSource(asset_id="video", expected_revision=1))
    response = dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=SourceAnalysisRequest(source_id=source["id"], source_revision=bound["revision"], idempotency_key="range-action-key"))
    assert response.status == "NEEDS_RANGE_SELECTION"
    assert source_conn.execute("SELECT count(*) FROM billing_operations").fetchone()[0] == 0


def test_stale_source_revision_cannot_create_bill(source_conn):
    source = prepared_source(source_conn, 20)
    bind_project_source(source_conn, actor=owner(), project_id="p", source_id=source["id"], request=BindProjectSource(asset_id="video", expected_revision=1))
    with pytest.raises(HTTPException) as error:
        dispatch_source_analysis(source_conn, actor=owner(), project_id="p", request=SourceAnalysisRequest(source_id=source["id"], source_revision=1, idempotency_key="stale-action-key"))
    assert error.value.detail["code"] == "SOURCE_REVISION_CONFLICT"
    assert source_conn.execute("SELECT count(*) FROM billing_operations").fetchone()[0] == 0
