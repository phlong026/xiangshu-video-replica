"""音轨上传暂存凭据：先登记后写入，失败对象按持久凭据重试清理。"""

from typing import Any
from uuid import uuid4

from app import content_store
from app.db_portable import BusinessConnection
from app.storage import StorageAdapter
from app.viral_import import ViralCopySource, _error, _viral_copy_replay


def lock_upload_request(conn: BusinessConnection, owner: str, key: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"viral-copy-upload:{owner}:{key}",),
    )


def existing_upload(
    conn: BusinessConnection, *, owner: str, key: str, request_hash: str
) -> ViralCopySource | None:
    lock_upload_request(conn, owner, key)
    row = conn.execute(
        "SELECT * FROM viral_import_tasks WHERE owner_user_id=%s AND idempotency_key=%s",
        (owner, key),
    ).fetchone()
    if row is not None:
        return _viral_copy_replay(row, request_hash=request_hash)
    receipt = conn.execute(
        "SELECT * FROM viral_copy_upload_receipts WHERE owner_user_id=%s AND idempotency_key=%s "
        "ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE",
        (owner, key),
    ).fetchone()
    if receipt is not None:
        if receipt["request_hash"] != request_hash:
            raise _error(409, "VIRAL_COPY_IDEMPOTENCY_CONFLICT", "幂等键已用于不同的提取请求。")
        if receipt["state"] == "STAGED":
            active = conn.execute(
                "SELECT 1 FROM viral_copy_upload_receipts WHERE id=%s AND lease_expires_at>now()",
                (receipt["id"],),
            ).fetchone()
            if active is not None:
                raise _error(
                    409, "VIRAL_COPY_UPLOAD_IN_PROGRESS", "音轨正在上传，请使用原请求稍后重试。"
                )
            conn.execute(
                "UPDATE viral_copy_upload_receipts SET state='CLEANUP',next_cleanup_at=now() "
                "WHERE id=%s",
                (receipt["id"],),
            )
    return None


def stage_upload(
    conn: BusinessConnection,
    *,
    owner: str,
    key: str,
    request_hash: str,
    storage: StorageAdapter,
    object_key: str,
) -> str:
    receipt_id = str(uuid4())
    conn.execute(
        "INSERT INTO viral_copy_upload_receipts "
        "(id,owner_user_id,idempotency_key,request_hash,storage_namespace,object_key) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (receipt_id, owner, key, request_hash, storage.cache_namespace, object_key),
    )
    return receipt_id


def lock_staged_upload(conn: BusinessConnection, receipt_id: str) -> Any:
    row = conn.execute(
        "SELECT * FROM viral_copy_upload_receipts WHERE id=%s FOR UPDATE", (receipt_id,)
    ).fetchone()
    if row is None or row["state"] != "STAGED":
        raise _error(409, "VIRAL_COPY_UPLOAD_EXPIRED", "上传请求已失效，请重试。")
    valid = conn.execute(
        "SELECT 1 FROM viral_copy_upload_receipts WHERE id=%s AND lease_expires_at>now()",
        (receipt_id,),
    ).fetchone()
    if valid is None:
        raise _error(409, "VIRAL_COPY_UPLOAD_EXPIRED", "上传请求已失效，请重试。")
    return row


def publish_upload(conn: BusinessConnection, receipt_id: str, asset_id: str) -> None:
    conn.execute(
        "UPDATE viral_copy_upload_receipts SET state='PUBLISHED',asset_id=%s,updated_at=now() "
        "WHERE id=%s AND state='STAGED'",
        (asset_id, receipt_id),
    )


def reclaim_staged_uploads(
    conn: BusinessConnection,
    storage: StorageAdapter,
    *,
    limit: int = 50,
    receipt_id: str | None = None,
) -> dict[str, int]:
    """只清理已登记未发布对象；行锁排除仍在写入者，墓碑继续复查迟到的存储写入。"""
    rows = conn.execute(
        "SELECT * FROM viral_copy_upload_receipts WHERE storage_namespace=%s "
        "AND state<>'PUBLISHED' AND ((%s::text IS NOT NULL AND id=%s) OR "
        "(%s::text IS NULL AND next_cleanup_at<=now())) "
        "ORDER BY next_cleanup_at,id LIMIT %s FOR UPDATE SKIP LOCKED",
        (storage.cache_namespace, receipt_id, receipt_id, receipt_id, limit),
    ).fetchall()
    result = {"deleted": 0, "failed": 0, "skipped": 0}
    for row in rows:
        key = str(row["object_key"])
        try:
            if not key.startswith("viral/copy/"):
                raise RuntimeError("暂存凭据命名空间无效")
            obj = storage.head_object(key)
            # 发布提交结果未知时，以资产引用为准，绝不靠异常类型猜是否能删除。
            referenced = (
                obj is not None
                and conn.execute(
                    "SELECT 1 FROM assets WHERE storage_uri=%s LIMIT 1", (obj.uri,)
                ).fetchone()
                is not None
            )
            if referenced:
                conn.execute(
                    "UPDATE viral_copy_upload_receipts SET state='PUBLISHED',updated_at=now() "
                    "WHERE id=%s",
                    (row["id"],),
                )
                result["skipped"] += 1
                continue
            if obj is not None:
                if not content_store.delete_object_outside_content_namespace(
                    storage, key, actor_id="viral-copy-staging-gc"
                ):
                    raise RuntimeError("暂存对象不在可清理命名空间")
                result["deleted"] += 1
            conn.execute(
                "UPDATE viral_copy_upload_receipts SET state='REAPED',cleanup_error=NULL, "
                "cleanup_attempts=cleanup_attempts+1,next_cleanup_at=now()+interval '1 hour', "
                "updated_at=now() WHERE id=%s",
                (row["id"],),
            )
        except Exception as exc:
            conn.execute(
                "UPDATE viral_copy_upload_receipts SET state='CLEANUP',cleanup_error=%s, "
                "cleanup_attempts=cleanup_attempts+1,next_cleanup_at=now()+interval '5 minutes', "
                "updated_at=now() WHERE id=%s",
                (type(exc).__name__, row["id"]),
            )
            result["failed"] += 1
    return result
