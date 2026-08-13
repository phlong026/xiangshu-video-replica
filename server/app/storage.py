from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

StorageProvider = Literal["cos", "oss", "local", "fake"]


class StoragePermissionError(PermissionError):
    """Raised when the business layer denies object access."""


class SourceUrlExpired(RuntimeError):
    """Raised when a temporary provider URL is no longer safe to archive."""


class StorageBackendUnavailable(RuntimeError):
    """Raised for configured cloud operations that require a real SDK/client."""


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


def create_storage_adapter(config: CloudStorageConfig) -> StorageAdapter:
    return CloudStorageAdapter(config)


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
    def __init__(self, config: CloudStorageConfig) -> None:
        super().__init__(
            provider=config.provider,
            bucket=config.bucket,
            key_prefix=config.key_prefix,
        )
        self._endpoint = config.endpoint.rstrip("/")
        self._access_key_id = config.access_key_id
        self._secret_access_key = config.secret_access_key
        self._region = config.region or ""

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        raise StorageBackendUnavailable("cloud put_object requires the provider SDK/client")

    def get_object(self, key: str) -> bytes:
        raise StorageBackendUnavailable("cloud get_object requires the provider SDK/client")

    def head_object(self, key: str) -> StoredObject | None:
        raise StorageBackendUnavailable("cloud head_object requires the provider SDK/client")

    def archive_result(
        self,
        source: ArchiveSource,
        *,
        destination_key: str,
        actor_id: str | None = None,
    ) -> StoredObject:
        raise StorageBackendUnavailable(
            "cloud archive_result requires an HTTP client and SDK/client"
        )

    def _signed_url(self, method: str, key: str, expires_at: datetime) -> str:
        expires = _timestamp(expires_at)
        canonical = "\n".join([method, self.provider, self.bucket, key, str(expires), self._region])
        signature = hmac.new(
            self._secret_access_key.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query = urlencode(
            {
                "x-storage-provider": self.provider,
                "x-access-key-id": self._access_key_id,
                "x-expires": str(expires),
                "x-signature": signature,
            }
        )
        return f"{self._endpoint}/{quote(key)}?{query}"

    def _delete_object(self, key: str) -> None:
        raise StorageBackendUnavailable("cloud delete_object requires the provider SDK/client")


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
