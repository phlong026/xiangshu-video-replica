"""回填生成资产的缩略图键（ADM-15 缩略图治理）。

管理端生成记录的缩略图占位（「暂无缩略图」）有两类根因：历史资产在
``assets.metadata_json`` 里没有 ``thumbnail_key``（抽帧写入点上线前入库，
或当初抽帧失败），而缩略图签发端点对缺键资产宁可占位也不签一张可能 404
的图。本脚本把「键可确定性派生 + 对象确实存在」的资产补上键，让这批历史
记录重新显示缩略图；对象不存在的资产尝试现场抽帧补生成（读原图 → 抽帧 →
落存储 → 记键），失败则保持占位并计数。

只处理对象存储（COS）上的视频/图片资产；本地盘通道按签发端点的同一决策
跳过。默认只读预览；写库与写存储都需要显式 ``--apply``。可重复执行。

用法::

    python -m scripts.backfill_generation_thumbnails             # 预览
    python -m scripts.backfill_generation_thumbnails --apply     # 执行
    python -m scripts.backfill_generation_thumbnails --apply --limit 200
"""

from __future__ import annotations

import argparse
import json
import sys

import psycopg

from app.db_pg import resolve_cli_pg_dsn
from app.db_portable import BusinessConnection
from app.material_thumbs import (
    ensure_thumbnail_object,
    store_video_thumbnail,
    thumbnail_key_for,
)
from app.media_routes import storage_for_asset
from app.storage import StorageBackendUnavailable

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def _is_image_key(key: str) -> bool:
    return key.lower().endswith(_IMAGE_SUFFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill asset thumbnail keys for generation records."
    )
    parser.add_argument("--apply", action="store_true", help="write; default is preview")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    dsn = resolve_cli_pg_dsn()
    stats = {
        "already_ok": 0,
        "key_backfilled": 0,
        "derived_now": 0,
        "failed": 0,
        "skipped_local": 0,
    }
    with psycopg.connect(dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            """
            SELECT id, storage_uri, metadata_json, content_type
            FROM assets
            WHERE COALESCE(content_type, '') LIKE 'video/%'
               OR COALESCE(content_type, '') LIKE 'image/%'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (args.limit,),
        ).fetchall()
        for row in rows:
            asset_id, storage_uri = str(row[0]), str(row[1])
            try:
                metadata = json.loads(str(row[2] or "{}"))
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if isinstance(metadata.get("thumbnail_key"), str) and metadata["thumbnail_key"]:
                stats["already_ok"] += 1
                continue
            # storage_uri 形如 cos://bucket/key 或本地前缀；仅对象存储可派生。
            if not storage_uri.startswith("cos://"):
                stats["skipped_local"] += 1
                continue
            try:
                storage = storage_for_asset(conn, storage_uri)
            except StorageBackendUnavailable:
                stats["failed"] += 1
                continue
            if storage.provider != "cos":
                stats["skipped_local"] += 1
                continue
            object_key = storage_uri.split("://", 1)[1]
            bucket_sep = object_key.find("/")
            if bucket_sep <= 0:
                stats["failed"] += 1
                continue
            object_key = object_key[bucket_sep + 1 :]
            thumb_key = thumbnail_key_for(object_key)
            if ensure_thumbnail_object(storage, thumb_key):
                metadata["thumbnail_key"] = thumb_key
                stats["key_backfilled"] += 1
            elif args.apply:
                # 对象不存在：尝试读原图现场抽帧（失败多为原对象也已缺失）。
                head = storage.head_object(object_key)
                if head is None:
                    stats["failed"] += 1
                    continue
                content = storage.get_object(object_key)
                derived = store_video_thumbnail(
                    storage, object_key, content, is_image=_is_image_key(object_key)
                )
                if derived is None:
                    stats["failed"] += 1
                    continue
                metadata["thumbnail_key"] = derived
                stats["derived_now"] += 1
            else:
                stats["failed"] += 1
                continue
            if args.apply:
                conn.execute(
                    "UPDATE assets SET metadata_json=%s WHERE id=%s",
                    (json.dumps(metadata, ensure_ascii=False), asset_id),
                )
        if args.apply:
            raw.commit()
    print(json.dumps(stats, ensure_ascii=False))
    if not args.apply:
        print("预览模式：未写库未写存储；加 --apply 执行。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
