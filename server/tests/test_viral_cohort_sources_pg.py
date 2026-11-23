"""运营内容漏斗按管理员主动采集隔离，历史来源记录保留供个人任务使用。

1. customer_import 不计入运营漏斗，也不污染关键词榜；
2. content_overview 输出 sourcesByKind 来源分布；
3. 迁移 20261008T1000 对起计以来已入池、无来源行的视频做一次性回填。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
)

SERVER_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = SERVER_DIR / "migrations"
PREVIOUS_HEAD = "20261002T1015_merge_oral_compatibility_alerts"


def _alembic_config(dsn: str) -> Config:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


def _insert_pool_video(
    conn: psycopg.Connection,
    video_id: str,
    *,
    platform: str = "douyin",
    deleted: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO viral_videos(platform,video_id,title,category,published_at,"
        "collection_published,deleted_at) VALUES(%s,%s,'测试标题','推荐',"
        "extract(epoch from now())::bigint,1,%s)",
        (platform, video_id, "now()" if deleted else None),
    )


def test_customer_import_source_is_excluded_from_operator_funnel() -> None:
    require_pg_or_explicit_skip()
    dsn = create_test_database("cohort_overview_test")
    try:
        command.upgrade(_alembic_config(dsn), "head")
        from app.db_portable import BusinessConnection
        from app.viral_content_observations import record_content_source
        from app.viral_content_overview import content_overview

        with psycopg.connect(dsn, autocommit=True) as raw:
            _insert_pool_video(raw, "v1")
            _insert_pool_video(raw, "v2")
            conn = BusinessConnection.postgres(raw)
            record_content_source(
                conn,
                batch_id="import:r1",
                source_kind="customer_import",
                platform="douyin",
                video_id="v1",
                keyword="",
                is_new=True,
            )
            record_content_source(
                conn,
                batch_id="kw1",
                source_kind="collection",
                platform="douyin",
                video_id="v2",
                keyword="庭院",
                is_new=True,
            )
        with psycopg.connect(dsn) as raw:
            overview = content_overview(
                BusinessConnection.postgres(raw),
                start=date.today(),
                end=date.today() + timedelta(days=1),
            )
        funnel = {step["stage"]: step["count"] for step in overview["funnel"]}
        assert funnel["collected"] == 1, "客户导入不能计入后台运营采集"
        kinds = {row["sourceKind"]: row["videos"] for row in overview["sourcesByKind"]}
        assert kinds == {"collection": 1}
        # 个人导入来源同时退出运营漏斗和关键词榜。
        assert [k["keyword"] for k in overview["bestKeywords"]] == ["庭院"]
    finally:
        drop_test_database("cohort_overview_test")


def test_private_read_before_operator_import_does_not_count_as_keyword_use() -> None:
    require_pg_or_explicit_skip()
    dsn = create_test_database("cohort_overview_test")
    try:
        command.upgrade(_alembic_config(dsn), "head")
        from app.db_portable import BusinessConnection
        from app.viral_content_overview import content_overview

        with psycopg.connect(dsn) as raw:
            _insert_pool_video(raw, "adopted")
            raw.execute(
                "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,"
                "keyword,detail_at) VALUES "
                "('private-first','customer_import','douyin','adopted','',now()),"
                "('operator-later','admin_search','douyin','adopted','庭院',NULL)"
            )
            overview = content_overview(
                BusinessConnection.postgres(raw),
                start=date.today(),
                end=date.today() + timedelta(days=1),
            )
        assert overview["bestKeywords"] == [
            {"keyword": "庭院", "platform": "douyin", "collected": 1, "used": 0}
        ]
        assert overview["sourcesByKind"] == [{"sourceKind": "admin_search", "videos": 1}]
    finally:
        drop_test_database("cohort_overview_test")


def test_backfill_migration_registers_unsourced_pool_videos_since_start() -> None:
    require_pg_or_explicit_skip()
    dsn = create_test_database("cohort_backfill_test")
    try:
        # 先停在上一头，制造"迁移前已入池"的存量，再升到新头触发回填。
        command.upgrade(_alembic_config(dsn), PREVIOUS_HEAD)
        with psycopg.connect(dsn, autocommit=True) as raw:
            _insert_pool_video(raw, "fresh")
            _insert_pool_video(raw, "gone", deleted=True)
            _insert_pool_video(raw, "xhs1", platform="xiaohongshu")
            _insert_pool_video(raw, "hassrc")
            raw.execute(
                "INSERT INTO viral_content_sources(batch_id,source_kind,platform,"
                "video_id,keyword) VALUES('batch1','collection','douyin','hassrc','庭院')"
            )
        command.upgrade(_alembic_config(dsn), "head")
        with psycopg.connect(dsn) as raw:
            rows = raw.execute(
                "SELECT platform,video_id,batch_id,source_kind,keyword "
                "FROM viral_content_sources ORDER BY video_id,batch_id"
            ).fetchall()
        by_video: dict[str, list[tuple]] = {}
        for row in rows:
            by_video.setdefault(str(row[1]), []).append(tuple(row))
        # 无来源的入池视频按进入内容池时间回填，keyword 留空串。
        assert [r[2] for r in by_video["fresh"]] == ["backfill"]
        assert by_video["fresh"][0][4] == ""
        # 已有来源的不重复回填；已删除与小红书（漏斗平台之外）不回填。
        assert [r[2] for r in by_video["hassrc"]] == ["batch1"]
        assert "gone" not in by_video
        assert "xhs1" not in by_video
    finally:
        drop_test_database("cohort_backfill_test")
