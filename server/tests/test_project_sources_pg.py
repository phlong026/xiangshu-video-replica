"""来源恢复的真库合同：换源、幂等、资产归属和版本冲突。"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import create_test_database, drop_test_database, upgrade_test_database_to_head

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.project_sources import (
    BindProjectSource,
    CreateProjectSource,
    bind_project_source,
    create_project_source,
    current_project_source,
    source_response,
)


@pytest.fixture(scope="module")
def source_dsn() -> Iterator[str]:
    dsn = create_test_database("project_sources_test")
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database("project_sources_test")


@pytest.fixture()
def source_conn(source_dsn: str) -> Iterator[BusinessConnection]:
    with psycopg.connect(source_dsn) as raw:
        raw.execute(
            "INSERT INTO users(id,username,display_name,role) VALUES "
            "('owner','owner','用户','employee'),('other','other','其他','employee')"
        )
        raw.execute(
            "INSERT INTO projects(id,name,owner_user_id) VALUES "
            "('p','项目','owner'),('other-p','其他项目','other')"
        )
        for asset_id, project, kind, content_type in [
            ("audio", "p", "reference_audio", "audio/mp4"),
            ("video", "p", "reference_video", "video/mp4"),
            ("other-video", "other-p", "reference_video", "video/mp4"),
        ]:
            raw.execute(
                "INSERT INTO assets(id,project_id,kind,storage_uri,sha256,size_bytes,"
                "content_type,created_by_user_id,metadata_json) "
                "VALUES (%s,%s,%s,'cos://bucket/file',repeat('a',64),1024,%s,%s,%s)",
                (
                    asset_id,
                    project,
                    kind,
                    content_type,
                    "owner" if project == "p" else "other",
                    '{"duration_seconds":60,"upload_status":"COMPLETE"}',
                ),
            )
        try:
            yield BusinessConnection.postgres(raw)
        finally:
            raw.rollback()


def owner() -> CurrentUser:
    return CurrentUser(id="owner", username="owner", display_name="用户", role="employee")


def create(conn: BusinessConnection, key: str = "source-key-1"):
    return create_project_source(
        conn,
        actor=owner(),
        project_id="p",
        request=CreateProjectSource(
            filename="原片.mp4", size_bytes=1024, duration_seconds=60, idempotency_key=key
        ),
    )


def test_source_replay_cannot_replace_newer_current_source(source_conn):
    first = create(source_conn)
    second = create(source_conn, "source-key-2")
    assert create(source_conn)["id"] == first["id"]
    assert current_project_source(source_conn, actor=owner(), project_id="p")["id"] == second["id"]
    with pytest.raises(HTTPException) as error:
        bind_project_source(
            source_conn,
            actor=owner(),
            project_id="p",
            source_id=str(first["id"]),
            request=BindProjectSource(asset_id="audio", expected_revision=1),
        )
    assert error.value.detail["code"] == "SOURCE_REPLACED"


def test_audio_and_original_binding_preserve_separate_media_facts(source_conn):
    source = create(source_conn)
    audio = bind_project_source(
        source_conn,
        actor=owner(),
        project_id="p",
        source_id=str(source["id"]),
        request=BindProjectSource(asset_id="audio", expected_revision=1),
    )
    assert audio["verified_sha256"] is None
    assert audio["original_video_asset_id"] is None
    assert audio["revision"] == 2
    video = bind_project_source(
        source_conn,
        actor=owner(),
        project_id="p",
        source_id=str(source["id"]),
        request=BindProjectSource(asset_id="video", expected_revision=2),
    )
    assert source_response(video).audio_source_asset_id == "audio"
    assert video["verified_duration_seconds"] == 60
    assert video["verified_sha256"] == "a" * 64
    replay = bind_project_source(
        source_conn,
        actor=owner(),
        project_id="p",
        source_id=str(source["id"]),
        request=BindProjectSource(asset_id="video", expected_revision=2),
    )
    assert replay["revision"] == 3


def test_binding_revision_conflict_and_cross_project_access(source_conn):
    source = create(source_conn)
    for asset_id, revision, code in [
        ("audio", 7, "SOURCE_REVISION_CONFLICT"),
        ("other-video", 1, "ASSET_NOT_FOUND"),
    ]:
        with pytest.raises(HTTPException) as error:
            bind_project_source(
                source_conn,
                actor=owner(),
                project_id="p",
                source_id=str(source["id"]),
                request=BindProjectSource(asset_id=asset_id, expected_revision=revision),
            )
        assert error.value.detail["code"] == code


def test_source_request_conflict_does_not_create_another_source(source_conn):
    create(source_conn)
    with pytest.raises(HTTPException) as error:
        create_project_source(
            source_conn,
            actor=owner(),
            project_id="p",
            request=CreateProjectSource(
                filename="另一个.mp4", size_bytes=1024, idempotency_key="source-key-1"
            ),
        )
    assert error.value.detail["code"] == "SOURCE_REQUEST_CONFLICT"
