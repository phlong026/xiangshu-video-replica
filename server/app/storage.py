from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, urlencode

StorageProvider = Literal["cos", "oss", "local", "fake"]


class StoragePermissionError(PermissionError):
    """Raised when the business layer denies object access."""


class SourceUrlExpired(RuntimeError):
    """Raised when a temporary provider URL is no longer safe to archive."""


class StorageBackendUnavailable(RuntimeError):
    """Raised for configured cloud operations that require a real SDK/client."""


@dataclass(frozen=True)
class StorageObjectRef:
    provider: StorageProvider
    bucket: str
    key: str


@dataclass(frozen=True)
class UploadIntent:
    method: Literal["PUT"]
    url: str
    key: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class DownloadIntent:
    method: Literal["GET"]
    url: str
    key: str
    expires_at: datetime


@dataclass(frozen=True)
class StoredObject:
    provider: str
    bucket: str
    key: str
    uri: str
    size: int
    content_type: str
    sha256: str
    updated_at: datetime


@dataclass(frozen=True)
class ArchiveSource:
    url: str
    expires_at: datetime
    content: bytes
    content_type: str


@dataclass(frozen=True)
class StorageAuditEvent:
    action: str
    status: str
    object_key: str
    actor_id: str | None
    at: datetime
    metadata: dict[str, str] = field(default_factory=dict)


class StorageAdapter(Protocol):
    provider: str
    bucket: str

    @property
    def audit_events(self) -> list[StorageAuditEvent]: ...

    def create_upload_intent(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: timedelta,
    ) -> UploadIntent: ...

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent: ...

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject: ...

    def get_object(self, key: str) -> bytes: ...

    def head_object(self, key: str) -> StoredObject | None: ...

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject: ...

    def delete_object(self, key: str, *, actor_id: str | None = None) -> None: ...


@dataclass(frozen=True)
class CloudStorageConfig:
    provider: Literal["cos", "oss"]
    bucket: str
    endpoint: str
    access_key_id: str
    secret_access_key: str
    region: str | None = None
    key_prefix: str = ""
    timeout_seconds: int = 10


def create_storage_adapter(config: CloudStorageConfig) -> StorageAdapter:
    return CloudStorageAdapter(config)


STORAGE_ROOT_ENV = "VIDEO_REPLICA_STORAGE_ROOT"


def local_storage_root() -> Path:
    """Resolve the local storage root; fail fast when the env var is missing."""
    root = os.environ.get(STORAGE_ROOT_ENV)
    if not root:
        raise StorageBackendUnavailable(f"{STORAGE_ROOT_ENV} is required for local storage")
    return Path(root)


def create_local_storage_from_environment() -> StorageAdapter:
    """Build the local-filesystem storage adapter for local/dev runs.

    `active_storage_provider="local"` lets a machine without cloud storage
    credentials (e.g. a macOS dev box) run the full upload/archive flow.
    """
    return LocalStorageAdapter(root=local_storage_root())


def cloud_storage_config_from_settings(
    provider: str,
    config: dict[str, str],
) -> CloudStorageConfig:
    if provider not in {"cos", "oss"}:
        raise ValueError(f"unsupported cloud storage provider: {provider}")
    try:
        return CloudStorageConfig(
            provider=cast(Literal["cos", "oss"], provider),
            bucket=config["bucket"],
            endpoint=config.get("endpoint", ""),
            access_key_id=config["access_key_id"],
            secret_access_key=config["secret_access_key"],
            region=config.get("region"),
        )
    except KeyError as exc:
        raise ValueError("cloud storage settings are incomplete") from exc


def storage_object_ref_from_uri(uri: str) -> StorageObjectRef:
    provider, separator, remainder = uri.partition("://")
    if not separator:
        raise ValueError(f"invalid storage uri: {uri}")
    bucket, slash, key = remainder.partition("/")
    if provider not in {"cos", "oss", "local", "fake"} or not bucket or not slash or not key:
        raise ValueError(f"invalid storage uri: {uri}")
    return StorageObjectRef(
        provider=cast(StorageProvider, provider),
        bucket=bucket,
        key=_safe_key(key),
    )


def require_storage_match(storage: StorageAdapter, reference: StorageObjectRef) -> None:
    if storage.provider != reference.provider or storage.bucket != reference.bucket:
        raise StorageBackendUnavailable(
            f"storage adapter does not match {reference.provider}://{reference.bucket}"
        )


class _BaseStorageAdapter:
    def __init__(self, *, provider: str, bucket: str, key_prefix: str = "") -> None:
        self.provider = provider
        self.bucket = bucket
        self._key_prefix = key_prefix.strip("/")
        self._audit_events: list[StorageAuditEvent] = []

    @property
    def audit_events(self) -> list[StorageAuditEvent]:
        return self._audit_events

    def create_upload_intent(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: timedelta,
    ) -> UploadIntent:
        object_key = self._object_key(key)
        expires_at = _expires_at(expires_in)
        return UploadIntent(
            method="PUT",
            url=self._signed_url("PUT", object_key, expires_at),
            key=object_key,
            headers={"content-type": content_type},
            expires_at=expires_at,
        )

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent:
        object_key = self._object_key(key)
        if not can_read:
            self._audit("download_intent.denied", "denied", object_key, None)
            raise StoragePermissionError(f"download denied for {object_key}")
        expires_at = _expires_at(expires_in)
        return DownloadIntent(
            method="GET",
            url=self._signed_url("GET", object_key, expires_at),
            key=object_key,
            expires_at=expires_at,
        )

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject:
        object_key = self._object_key(destination_key)
        if source.expires_at <= datetime.now(UTC):
            self._audit("archive_result.failed", "expired_source", object_key, actor_id)
            raise SourceUrlExpired(source.url)
        archived = self.put_object(object_key, source.content, content_type=source.content_type)
        self._audit("archive_result.succeeded", "succeeded", object_key, actor_id)
        return archived

    def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
        object_key = self._object_key(key)
        self._delete_object(object_key)
        self._audit("object.deleted", "succeeded", object_key, actor_id)

    def _object_key(self, key: str) -> str:
        safe_key = _safe_key(key)
        if not self._key_prefix:
            return safe_key
        if safe_key.startswith(f"{self._key_prefix}/") or safe_key == self._key_prefix:
            return safe_key
        return f"{self._key_prefix}/{safe_key}"

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        raise NotImplementedError

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        raise NotImplementedError

    def get_object(self, key: str) -> bytes:
        raise NotImplementedError

    def head_object(self, key: str) -> StoredObject | None:
        raise NotImplementedError

    def _delete_object(self, key: str) -> None:
        raise NotImplementedError

    def _audit(
        self,
        action: str,
        status: str,
        object_key: str,
        actor_id: str | None,
    ) -> None:
        self._audit_events.append(
            StorageAuditEvent(
                action=action,
                status=status,
                object_key=object_key,
                actor_id=actor_id,
                at=datetime.now(UTC),
                metadata={"provider": self.provider, "bucket": self.bucket},
            )
        )


class FakeStorageAdapter(_BaseStorageAdapter):
    def __init__(
        self,
        *,
        provider: Literal["cos", "oss", "fake"],
        bucket: str,
        key_prefix: str = "",
    ) -> None:
        super().__init__(provider=provider, bucket=bucket, key_prefix=key_prefix)
        self._objects: dict[str, tuple[bytes, StoredObject]] = {}

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        stored = _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=content,
            content_type=content_type,
        )
        self._objects[object_key] = (content, stored)
        return stored

    def get_object(self, key: str) -> bytes:
        object_key = self._object_key(key)
        return self._objects[object_key][0]

    def head_object(self, key: str) -> StoredObject | None:
        object_key = self._object_key(key)
        stored = self._objects.get(object_key)
        return None if stored is None else stored[1]

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        query = urlencode(
            {
                "x-storage-provider": self.provider,
                "x-method": method,
                "x-expires": str(_timestamp(expires_at)),
            }
        )
        return f"{self.provider}://{self.bucket}/{quote(key)}?{query}"

    def _delete_object(self, key: str) -> None:
        self._objects.pop(key, None)


class LocalStorageAdapter(_BaseStorageAdapter):
    def __init__(
        self,
        *,
        root: Path,
        provider: Literal["local"] = "local",
        bucket: str = "local-private",
        key_prefix: str = "",
    ) -> None:
        super().__init__(provider=provider, bucket=bucket, key_prefix=key_prefix)
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        path = self._path_for(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=content,
            content_type=content_type,
        )

    def get_object(self, key: str) -> bytes:
        return self._path_for(self._object_key(key)).read_bytes()

    def head_object(self, key: str) -> StoredObject | None:
        object_key = self._object_key(key)
        path = self._path_for(object_key)
        if not path.exists():
            return None
        return _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=path.read_bytes(),
            content_type="application/octet-stream",
        )

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        query = urlencode({"method": method, "expires": str(_timestamp(expires_at))})
        return f"local://{self.bucket}/{quote(key)}?{query}"

    def _delete_object(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            path.unlink()

    def _path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"unsafe object key: {key}") from exc
        return path


class CloudStorageAdapter(_BaseStorageAdapter):
    def __init__(self, config: CloudStorageConfig, *, client: Any | None = None) -> None:
        super().__init__(
            provider=config.provider,
            bucket=config.bucket,
            key_prefix=config.key_prefix,
        )
        self._endpoint = config.endpoint.rstrip("/")
        self._access_key_id = config.access_key_id
        self._secret_access_key = config.secret_access_key
        self._region = config.region or ""
        self._timeout_seconds = config.timeout_seconds
        self._client = client if client is not None else self._create_provider_client()

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._object_key(key)
        try:
            if self.provider == "cos":
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=object_key,
                    Body=content,
                    ContentType=content_type,
                )
            else:
                self._client.put_object(object_key, content, headers={"Content-Type": content_type})
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object upload failed") from exc
        return _stored_object(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            content=content,
            content_type=content_type,
        )

    def create_upload_intent(
        self,
        key: str,
        *,
        content_type: str,
        expires_in: timedelta,
    ) -> UploadIntent:
        object_key = self._object_key(key)
        expires_at = _expires_at(expires_in)
        headers = {"Content-Type": content_type}
        try:
            if self.provider == "cos":
                url = self._client.get_presigned_url(
                    Bucket=self.bucket,
                    Key=object_key,
                    Method="PUT",
                    Expired=_seconds(expires_in),
                    Headers=headers,
                    SignHost=True,
                )
            else:
                url = self._client.sign_url(
                    "PUT",
                    object_key,
                    _seconds(expires_in),
                    headers=headers,
                    slash_safe=True,
                )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud upload URL signing failed") from exc
        return UploadIntent(
            method="PUT",
            url=str(url),
            key=object_key,
            headers=headers,
            expires_at=expires_at,
        )

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent:
        object_key = self._object_key(key)
        if not can_read:
            return super().create_download_intent(object_key, expires_in=expires_in, can_read=False)
        expires_at = _expires_at(expires_in)
        try:
            if self.provider == "cos":
                url = self._client.get_presigned_download_url(
                    Bucket=self.bucket,
                    Key=object_key,
                    Expired=_seconds(expires_in),
                    SignHost=True,
                )
            else:
                url = self._client.sign_url(
                    "GET",
                    object_key,
                    _seconds(expires_in),
                    slash_safe=True,
                )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud download URL signing failed") from exc
        return DownloadIntent(method="GET", url=str(url), key=object_key, expires_at=expires_at)

    def get_object(self, key: str) -> bytes:
        object_key = self._object_key(key)
        try:
            if self.provider == "cos":
                response = self._client.get_object(Bucket=self.bucket, Key=object_key)
                return bytes(response["Body"].get_raw_stream().read())
            return bytes(self._client.get_object(object_key).read())
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object download failed") from exc

    def head_object(self, key: str) -> StoredObject | None:
        object_key = self._object_key(key)
        try:
            if self.provider == "cos":
                headers = dict(self._client.head_object(Bucket=self.bucket, Key=object_key))
            else:
                headers = dict(self._client.head_object(object_key).headers)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StorageBackendUnavailable("cloud object metadata lookup failed") from exc
        return StoredObject(
            provider=self.provider,
            bucket=self.bucket,
            key=object_key,
            uri=f"{self.provider}://{self.bucket}/{object_key}",
            size=int(_header(headers, "content-length", "0")),
            content_type=str(_header(headers, "content-type", "application/octet-stream")),
            sha256="",
            updated_at=datetime.now(UTC),
        )

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject:
        return super().archive_result(source, destination_key=destination_key, actor_id=actor_id)

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        seconds_remaining = max(1, _timestamp(expires_at) - _timestamp(datetime.now(UTC)))
        expires_in = timedelta(seconds=seconds_remaining)
        if method == "GET":
            return self.create_download_intent(key, expires_in=expires_in, can_read=True).url
        if method == "PUT":
            return self.create_upload_intent(
                key,
                content_type="application/octet-stream",
                expires_in=expires_in,
            ).url
        raise StorageBackendUnavailable(f"cloud URL signing does not support {method}")

    def _delete_object(self, key: str) -> None:
        try:
            if self.provider == "cos":
                self._client.delete_object(Bucket=self.bucket, Key=key)
            else:
                self._client.delete_object(key)
        except Exception as exc:
            raise StorageBackendUnavailable("cloud object deletion failed") from exc

    def _create_provider_client(self) -> Any:
        try:
            if self.provider == "cos":
                from qcloud_cos import CosConfig, CosS3Client  # type: ignore[import-untyped]

                kwargs: dict[str, Any] = {
                    "Region": self._region,
                    "SecretId": self._access_key_id,
                    "SecretKey": self._secret_access_key,
                    "Scheme": "https",
                    "Timeout": self._timeout_seconds,
                }
                if self._endpoint:
                    kwargs["Endpoint"] = self._endpoint
                return CosS3Client(CosConfig(**kwargs))

            import oss2  # type: ignore[import-untyped]

            if not self._endpoint:
                raise ValueError("OSS endpoint is required")
            auth = oss2.Auth(self._access_key_id, self._secret_access_key)
            return oss2.Bucket(
                auth,
                self._endpoint,
                self.bucket,
                region=self._region or None,
                connect_timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise StorageBackendUnavailable("cloud storage client initialization failed") from exc


def _safe_key(key: str) -> str:
    if key.startswith("/") or key in {"", ".", ".."}:
        raise ValueError(f"unsafe object key: {key}")
    normalized = os.path.normpath(key).replace("\\", "/")
    if normalized.startswith("../") or normalized == "..":
        raise ValueError(f"unsafe object key: {key}")
    return normalized


def _expires_at(expires_in: timedelta) -> datetime:
    if expires_in <= timedelta(seconds=0):
        raise ValueError("expires_in must be positive")
    return datetime.now(UTC) + expires_in


def _timestamp(value: datetime) -> int:
    return int(value.timestamp())


def _seconds(value: timedelta) -> int:
    return max(1, int(value.total_seconds()))


def _is_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    error_text = str(exc)
    return (
        status == 404
        or "NoSuchKey" in error_text
        or "NoSuchResource" in error_text
        or "NotFound" in error_text
    )


def _header(headers: dict[str, Any], name: str, default: str) -> Any:
    return next((value for key, value in headers.items() if key.lower() == name), default)


def _stored_object(
    *,
    provider: str,
    bucket: str,
    key: str,
    content: bytes,
    content_type: str,
) -> StoredObject:
    digest = hashlib.sha256(content).hexdigest()
    return StoredObject(
        provider=provider,
        bucket=bucket,
        key=key,
        uri=f"{provider}://{bucket}/{key}",
        size=len(content),
        content_type=content_type,
        sha256=digest,
        updated_at=datetime.now(UTC),
    )
