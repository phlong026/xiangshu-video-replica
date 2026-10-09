"""漏斗口径改为全来源：客户导入进入内容池也登记 cohort 来源，并回填起计以来的存量。

依据 2026-10-08 拍板（docs/后台管理系统业务对标与改进方案-2026-10-08.md 附篇 A）：
此前 viral_content_sources 只有关键词批次采集与管理端实时搜索两个写入方，
客户链接导入与转存只 upsert viral_videos，导致内容概览"本期采集"恒为 0。
本迁移把起计以来已入池、尚无来源行的视频回填为 source_kind='backfill'。
"""

from alembic import op

revision = "20261008T1000_viral_cohort_import_sources"
down_revision = "20261006T1400_viral_collection_default_off"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    # 回填语义与运行时 record_content_source 一致：collected_at 取进入内容池时间
    # （viral_videos.created_at 是 TEXT 的 CURRENT_TIMESTAMP，PG 可直接 cast）；
    # 阶段时间戳只标"回填执行时首次观察到"，不推断历史准备/上首页时刻。
    # ready/live 判据内联自 app.viral_content_state.ready_sql 与
    # app.viral_homepage.live_homepage_sql 在本迁移时点的定义——迁移不引用应用代码，
    # 判据日后演进不影响已执行完的回填。
    op.execute("""INSERT INTO viral_content_sources
        (batch_id,source_kind,platform,video_id,keyword,collected_at,prepared_at,homepage_at)
        SELECT 'backfill','backfill',v.platform,v.video_id,'',v.created_at::timestamptz,
          CASE WHEN ((COALESCE(v.cover_url,'')='' OR v.cover_key IS NOT NULL) AND EXISTS
            (SELECT 1 FROM viral_media_preparations cs_media
             WHERE cs_media.platform=v.platform AND cs_media.video_id=v.video_id
             AND cs_media.media_kind='video' AND cs_media.status='SUCCEEDED'
             AND cs_media.storage_uri IS NOT NULL)) THEN now() END,
          CASE WHEN ((COALESCE(v.cover_url,'')='' OR v.cover_key IS NOT NULL) AND EXISTS
            (SELECT 1 FROM viral_media_preparations cs_media
             WHERE cs_media.platform=v.platform AND cs_media.video_id=v.video_id
             AND cs_media.media_kind='video' AND cs_media.status='SUCCEEDED'
             AND cs_media.storage_uri IS NOT NULL))
            AND (v.homepage_featured=1
              AND (v.homepage_starts_at IS NULL OR v.homepage_starts_at<=now())
              AND (v.homepage_ends_at IS NULL OR v.homepage_ends_at>now())) THEN now() END
        FROM viral_videos v
        WHERE v.deleted_at IS NULL AND v.platform IN ('douyin','wechat_channels')
          AND v.created_at::timestamptz >=
            (SELECT started_at FROM viral_content_measurement_state WHERE id=1)
          AND NOT EXISTS (SELECT 1 FROM viral_content_sources s
            WHERE s.platform=v.platform AND s.video_id=v.video_id)
        ON CONFLICT DO NOTHING""")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    # 只撤回填行；升级后由新代码写入的 customer_import 来源属于业务数据，不回滚。
    op.execute("DELETE FROM viral_content_sources WHERE batch_id='backfill'")
