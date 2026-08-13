from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from app.storage import (
    ArchiveSource,
    CloudStorageConfig,
    FakeStorageAdapter,
    LocalStorageAdapter,
    SourceUrlExpired,
    StorageBackendUnavailable,
    StoragePermissionError,
    create_storage_adapter,
    require_storage_match,
    storage_object_ref_from_uri,
)


def _flow(adapter: FakeStorageAdapter) -> tuple[str, str]:
    upload = adapter.create_upload_intent(
        key="projects/p1/source/reference.mp4",
        content_type="video/mp4",
        expires_in=timedelta(minutes=15),
    )
    adapter.put_object(upload.key, b"video", content_type="video/mp4")
    download = adapter.create_download_intent(
        upload.key,
        expires_in=timedelta(minutes=30),
        can_read=True,
    )
    return upload.method, download.method


def test_fake_adapter_supports_provider_switch_without_business_flow_changes() -> None:
    cos = FakeStorageAdapter(provider="cos", bucket="private-bucket")
    oss = FakeStorageAdapter(provider="oss", bucket="private-bucket")

    assert _flow(cos) == ("PUT", "GET")
    assert _flow(oss) == ("PUT", "GET")
    cos_object = cos.head_object("projects/p1/source/reference.mp4")
    oss_object = oss.head_object("projects/p1/source/reference.mp4")

    assert cos_object is not None
    assert oss_object is not None
    assert cos_object.uri.startswith("cos://")
    assert oss_object.uri.startswith("oss://")


def test_cloud_adapters_refuse_signing_before_real_sdk_integration() -> None:
    cases: tuple[tuple[Literal["cos", "oss"], str], ...] = (
        ("cos", "https://bucket.cos.ap-shanghai.myqcloud.com"),
        ("oss", "https://bucket.oss-cn-shanghai.aliyuncs.com"),
    )
    for provider, endpoint in cases:
        adapter = create_storage_adapter(
            CloudStorageConfig(
                provider=provider,
                bucket="private-bucket",
                endpoint=endpoint,
                access_key_id="public-id",
                secret_access_key="very-secret-key",
                region="ap-shanghai",
                key_prefix="tenant-a",
            )
        )

        with pytest.raises(StorageBackendUnavailable):
            adapter.create_upload_intent(
                key="projects/p1/source/reference.mp4",
                content_type="video/mp4",
                expires_in=timedelta(minutes=10),
            )
        with pytest.raises(StorageBackendUnavailable):
            adapter.create_download_intent(
                key="projects/p1/source/reference.mp4",
                expires_in=timedelta(minutes=20),
                can_read=True,
            )


def test_download_intent_requires_business_permission() -> None:
    adapter = FakeStorageAdapter(provider="cos", bucket="private-bucket")
    adapter.put_object("projects/p1/source/reference.mp4", b"video", content_type="video/mp4")

    with pytest.raises(StoragePermissionError):
        adapter.create_download_intent(
            "projects/p1/source/reference.mp4",
            expires_in=timedelta(minutes=5),
            can_read=False,
        )

    assert adapter.audit_events[-1].action == "download_intent.denied"
    assert adapter.audit_events[-1].status == "denied"


def test_archive_failure_can_retry_archive_only() -> None:
    adapter = FakeStorageAdapter(provider="cos", bucket="private-bucket")
    source = ArchiveSource(
        url="https://h3.example/tmp/result.mp4",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        content=b"first response",
        content_type="video/mp4",
    )

    with pytest.raises(SourceUrlExpired):
        adapter.archive_result(source, destination_key="projects/p1/outputs/b1/result.mp4")

    refreshed = ArchiveSource(
        url="https://h3.example/tmp/result.mp4?retry=1",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        content=b"second response",
        content_type="video/mp4",
    )
    archived = adapter.archive_result(
        refreshed,
        destination_key="projects/p1/outputs/b1/result.mp4",
        actor_id="worker-a",
    )

    assert archived.key == "projects/p1/outputs/b1/result.mp4"
    assert adapter.get_object(archived.key) == b"second response"
    assert [event.action for event in adapter.audit_events] == [
        "archive_result.failed",
        "archive_result.succeeded",
    ]


def test_delete_records_redacted_audit_without_presigned_url() -> None:
    adapter = FakeStorageAdapter(provider="oss", bucket="private-bucket")
    adapter.put_object("projects/p1/outputs/b1/result.mp4", b"video", content_type="video/mp4")
    adapter.delete_object("projects/p1/outputs/b1/result.mp4", actor_id="admin")

    assert adapter.head_object("projects/p1/outputs/b1/result.mp4") is None
    assert adapter.audit_events[-1].action == "object.deleted"
    assert adapter.audit_events[-1].actor_id == "admin"
    assert adapter.audit_events[-1].metadata == {"provider": "oss", "bucket": "private-bucket"}


def test_local_adapter_stores_objects_under_root_and_rejects_path_escape(tmp_path: Path) -> None:
    adapter = LocalStorageAdapter(root=tmp_path, provider="local", bucket="dev-private")
    adapter.put_object("projects/p1/source/reference.mp4", b"video", content_type="video/mp4")

    assert (tmp_path / "projects" / "p1" / "source" / "reference.mp4").read_bytes() == b"video"
    assert adapter.get_object("projects/p1/source/reference.mp4") == b"video"
    assert adapter.head_object("projects/p1/source/reference.mp4") is not None

    with pytest.raises(ValueError, match="unsafe object key"):
        adapter.put_object("../escape.mp4", b"bad", content_type="video/mp4")


def test_storage_uri_reference_rejects_wrong_provider_or_bucket() -> None:
    reference = storage_object_ref_from_uri("cos://private-bucket/projects/p1/output.mp4")

    assert reference.provider == "cos"
    assert reference.bucket == "private-bucket"
    assert reference.key == "projects/p1/output.mp4"

    with pytest.raises(StorageBackendUnavailable, match="does not match"):
        require_storage_match(
            FakeStorageAdapter(provider="oss", bucket="private-bucket"), reference
        )
