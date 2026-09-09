from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from qcloud_cos import CosConfig, CosS3Client

from app.storage import (
    ArchiveSource,
    CloudStorageAdapter,
    CloudStorageConfig,
    FakeStorageAdapter,
    LocalStorageAdapter,
    SourceUrlExpired,
    StorageBackendUnavailable,
    StoragePermissionError,
    cloud_storage_config_from_settings,
    require_storage_match,
    storage_object_ref_from_uri,
)


class FakeCosClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_presigned_url(self, **kwargs: object) -> str:
        self.calls.append(("presign", kwargs))
        return "https://cos.example/upload"

    def get_presigned_download_url(self, **kwargs: object) -> str:
        self.calls.append(("download", kwargs))
        return "https://cos.example/download"

    def put_object(self, **kwargs: object) -> None:
        self.calls.append(("put", kwargs))

    def copy_object(self, **kwargs: object) -> None:
        self.calls.append(("copy", kwargs))

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get", kwargs))
        return {"Body": FakeCosBody(b"video")}

    def head_object(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(("head", kwargs))
        return {
            "Content-Length": "5",
            "Content-Type": "video/mp4",
            "x-cos-meta-sha256": "cached-digest",
        }

    def head_bucket(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(("head-bucket", kwargs))
        return {}

    def delete_object(self, **kwargs: object) -> None:
        self.calls.append(("delete", kwargs))


class FakeCosBody:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def get_raw_stream(self) -> FakeCosBody:
        return self

    def read(self, size: int = -1) -> bytes:
        if not self.content:
            return b""
        if size < 0:
            result, self.content = self.content, b""
            return result
        result, self.content = self.content[:size], self.content[size:]
        return result


class FakeCosNoSuchResourceClient(FakeCosClient):
    def head_object(self, **kwargs: object) -> dict[str, str]:
        del kwargs
        raise RuntimeError("NoSuchResource: The Resource You Head Not Exist")


class FakeCosNoSuchBucketClient(FakeCosClient):
    def head_bucket(self, **kwargs: object) -> dict[str, str]:
        del kwargs
        raise RuntimeError("NoSuchBucket: The specified bucket does not exist")


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


def test_fake_adapter_supports_cos_business_flow() -> None:
    cos = FakeStorageAdapter(provider="cos", bucket="private-bucket")

    assert _flow(cos) == ("PUT", "GET")
    cos_object = cos.head_object("projects/p1/source/reference.mp4")

    assert cos_object is not None
    assert cos_object.uri.startswith("cos://")


def test_fake_adapter_copies_cached_media_to_project_namespace() -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object("viral/douyin/v1.mp4", b"video", content_type="video/mp4")

    copied = storage.copy_object(
        "viral/douyin/v1.mp4",
        "projects/p1/source/reference.mp4",
    )

    assert copied.key == "projects/p1/source/reference.mp4"
    assert copied.sha256
    assert storage.get_object(copied.key) == b"video"


def test_local_adapter_copies_without_removing_source(tmp_path: Path) -> None:
    storage = LocalStorageAdapter(root=tmp_path)
    storage.put_object("viral/douyin/v1.mp4", b"video", content_type="video/mp4")

    copied = storage.copy_object(
        "viral/douyin/v1.mp4",
        "projects/p1/source/reference.mp4",
    )

    assert copied.size == 5
    assert copied.sha256
    assert storage.get_object("viral/douyin/v1.mp4") == b"video"
    assert storage.get_object(copied.key) == b"video"


def test_local_adapter_streams_only_requested_object_range(tmp_path: Path) -> None:
    storage = LocalStorageAdapter(root=tmp_path)
    storage.put_object("viral/douyin/v1.mp4", b"0123456789", content_type="video/mp4")

    chunks = list(
        storage.iter_object(
            "viral/douyin/v1.mp4",
            start=2,
            end=7,
            chunk_size=3,
        )
    )

    assert chunks == [b"234", b"567"]


def test_local_adapter_heads_large_object_without_reading_it_all_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalStorageAdapter(root=tmp_path)
    storage.put_object("viral/douyin/v1.mp4", b"0123456789", content_type="video/mp4")

    def forbid_read_bytes(_path: Path) -> bytes:
        raise AssertionError("head_object must not call Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    stored = storage.head_object("viral/douyin/v1.mp4")

    assert stored is not None
    assert stored.size == 10
    assert stored.sha256 == hashlib.sha256(b"0123456789").hexdigest()


def test_local_storage_maps_filesystem_write_failure_to_backend_unavailable(
    tmp_path: Path,
) -> None:
    adapter = LocalStorageAdapter(root=tmp_path)
    (tmp_path / "generation-results").write_bytes(b"blocks the destination directory")

    with pytest.raises(StorageBackendUnavailable):
        adapter.put_object(
            "generation-results/task-1.mp4",
            b"fake mp4",
            content_type="video/mp4",
        )


def test_cloud_adapter_signs_and_operates_on_one_private_object() -> None:
    client = FakeCosClient()
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
            key_prefix="tenant-a",
        ),
        client=client,
    )

    upload = adapter.create_upload_intent(
        key="projects/p1/source/reference.mp4",
        content_type="video/mp4",
        expires_in=timedelta(minutes=10),
    )
    download = adapter.create_download_intent(
        upload.key,
        expires_in=timedelta(minutes=10),
        can_read=True,
    )
    stored = adapter.put_object(upload.key, b"video", content_type="video/mp4")
    head = adapter.head_object(upload.key)

    assert upload.method == "PUT"
    assert upload.headers == {"Content-Type": "video/mp4"}
    assert download.method == "GET"
    assert stored.uri == "cos://private-bucket/tenant-a/projects/p1/source/reference.mp4"
    assert next(call for call in client.calls if call[0] == "put")[1]["Metadata"] == {
        "x-cos-meta-sha256": hashlib.sha256(b"video").hexdigest()
    }
    assert adapter.get_object(upload.key) == b"video"
    assert head is not None
    assert head.size == 5
    assert head.content_type == "video/mp4"

    adapter.delete_object(upload.key, actor_id="admin")

    calls = client.calls
    assert calls[0] == (
        "presign",
        {
            "Bucket": "private-bucket",
            "Key": "tenant-a/projects/p1/source/reference.mp4",
            "Method": "PUT",
            "Expired": 600,
            "Headers": {"Content-Type": "video/mp4"},
            "SignHost": True,
        },
    )
    assert calls[1] == (
        "download",
        {
            "Bucket": "private-bucket",
            "Key": "tenant-a/projects/p1/source/reference.mp4",
            "Expired": 600,
            "SignHost": True,
        },
    )


def test_cloud_adapter_uses_server_side_copy_for_project_asset() -> None:
    client = FakeCosClient()
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
            key_prefix="tenant-a",
        ),
        client=client,
    )

    copied = adapter.copy_object(
        "viral/douyin/v1.mp4",
        "projects/p1/source/reference.mp4",
    )

    assert copied.key == "tenant-a/projects/p1/source/reference.mp4"
    assert copied.sha256 == "cached-digest"
    assert client.calls[0] == (
        "copy",
        {
            "Bucket": "private-bucket",
            "Key": "tenant-a/projects/p1/source/reference.mp4",
            "CopySource": {
                "Bucket": "private-bucket",
                "Key": "tenant-a/viral/douyin/v1.mp4",
                "Region": "ap-shanghai",
            },
        },
    )


def test_cloud_adapter_streams_with_provider_range() -> None:
    client = FakeCosClient()
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
            key_prefix="tenant-a",
        ),
        client=client,
    )

    assert (
        b"".join(adapter.iter_object("viral/douyin/v1.mp4", start=2, end=4, chunk_size=2))
        == b"video"
    )
    assert client.calls[0] == (
        "get",
        {
            "Bucket": "private-bucket",
            "Key": "tenant-a/viral/douyin/v1.mp4",
            "Range": "bytes=2-4",
        },
    )


def test_cloud_upload_intent_headers_isolated_from_sdk_auth_mutation() -> None:
    # 真实 qcloud_cos 客户端会把签名原地注入调用方传入的 Headers 字典。
    # 预签名 URL 已携带签名，intent 不得把 SDK 注入的 Authorization 下发给客户端，
    # 否则浏览器上传会额外要求 COS 桶 CORS 放行 authorization 请求头。
    real_client = CosS3Client(
        CosConfig(
            Region="ap-shanghai",
            SecretId="test-id",
            SecretKey="test-key",
            Scheme="https",
        )
    )
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket-1250000000",
            access_key_id="test-id",
            secret_access_key="test-key",
            region="ap-shanghai",
        ),
        client=real_client,
    )

    upload = adapter.create_upload_intent(
        key="projects/p1/source/reference.mp4",
        content_type="video/mp4",
        expires_in=timedelta(minutes=10),
    )

    assert upload.url.startswith("https://private-bucket-1250000000.cos.ap-shanghai.myqcloud.com/")
    assert upload.headers == {"Content-Type": "video/mp4"}


def test_cos_head_maps_deleted_object_no_such_resource_to_none() -> None:
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
        ),
        client=FakeCosNoSuchResourceClient(),
    )

    assert adapter.head_object("projects/p1/deleted.mp4") is None


def test_cos_readiness_rejects_a_missing_bucket() -> None:
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="missing-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
        ),
        client=FakeCosNoSuchBucketClient(),
    )

    with pytest.raises(StorageBackendUnavailable, match="bucket readiness"):
        adapter.check_readiness()


def test_cos_readiness_uses_bucket_head_instead_of_object_head() -> None:
    client = FakeCosClient()
    adapter = CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
        ),
        client=client,
    )

    adapter.check_readiness()

    assert client.calls == [("head-bucket", {"Bucket": "private-bucket"})]


def test_cloud_adapter_configures_sdk_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeCosConfig:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeCosS3Client:
        def __init__(self, config: object) -> None:
            captured["config"] = config

    monkeypatch.setitem(
        sys.modules,
        "qcloud_cos",
        SimpleNamespace(CosConfig=FakeCosConfig, CosS3Client=FakeCosS3Client),
    )

    CloudStorageAdapter(
        CloudStorageConfig(
            provider="cos",
            bucket="private-bucket",
            access_key_id="public-id",
            secret_access_key="very-secret-key",
            region="ap-shanghai",
        )
    )

    assert captured["Timeout"] == 10


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
    adapter = FakeStorageAdapter(provider="cos", bucket="private-bucket")
    adapter.put_object("projects/p1/outputs/b1/result.mp4", b"video", content_type="video/mp4")
    adapter.delete_object("projects/p1/outputs/b1/result.mp4", actor_id="admin")

    assert adapter.head_object("projects/p1/outputs/b1/result.mp4") is None
    assert adapter.audit_events[-1].action == "object.deleted"
    assert adapter.audit_events[-1].actor_id == "admin"
    assert adapter.audit_events[-1].metadata == {"provider": "cos", "bucket": "private-bucket"}


def test_local_adapter_stores_objects_under_root_and_rejects_path_escape(tmp_path: Path) -> None:
    adapter = LocalStorageAdapter(root=tmp_path, provider="local", bucket="dev-private")
    adapter.put_object("projects/p1/source/reference.mp4", b"video", content_type="video/mp4")

    assert (tmp_path / "projects" / "p1" / "source" / "reference.mp4").read_bytes() == b"video"
    assert adapter.get_object("projects/p1/source/reference.mp4") == b"video"
    stored = adapter.head_object("projects/p1/source/reference.mp4")
    assert stored is not None
    assert stored.content_type == "video/mp4"

    for unsafe_key in (
        "../escape.mp4",
        "projects/p1/../../users/victim/identity.jpg",
        r"projects/p1/uploads/id/..\..\..\users\victim.jpg",
        "projects//p1/output.mp4",
        "projects/./p1/output.mp4",
    ):
        with pytest.raises(ValueError, match="unsafe object key"):
            adapter.put_object(unsafe_key, b"bad", content_type="video/mp4")


def test_storage_uri_reference_rejects_wrong_provider_or_bucket() -> None:
    reference = storage_object_ref_from_uri("cos://private-bucket/projects/p1/output.mp4")

    assert reference.provider == "cos"
    assert reference.bucket == "private-bucket"
    assert reference.key == "projects/p1/output.mp4"

    with pytest.raises(StorageBackendUnavailable, match="does not match"):
        require_storage_match(FakeStorageAdapter(provider="cos", bucket="other-bucket"), reference)


def test_removed_oss_storage_uri_and_config_are_rejected() -> None:
    with pytest.raises(ValueError, match="invalid storage uri"):
        storage_object_ref_from_uri("oss://private-bucket/projects/p1/output.mp4")

    with pytest.raises(ValueError, match="unsupported cloud storage provider: oss"):
        cloud_storage_config_from_settings(
            "oss",
            {
                "access_key_id": "oss-id",
                "secret_access_key": "oss-secret",
                "bucket": "private-bucket",
                "endpoint": "https://oss.example",
            },
        )
