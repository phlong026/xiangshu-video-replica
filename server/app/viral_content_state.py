"""运营状态由可见性、素材和最新准备任务共同决定，筛选与行投影共用。"""

from app.viral_homepage import live_homepage_sql

MANAGED_SOURCE_KINDS_SQL = "('admin_link_import','admin_upload','admin_search','collection')"


def managed_source_sql(alias: str = "v") -> str:
    """平台运营资格来自主动采集，个人缓存、分类和历史发布位均不能代替。"""
    return (
        "(EXISTS (SELECT 1 FROM viral_content_sources managed_origin "
        f"WHERE managed_origin.platform={alias}.platform "
        f"AND managed_origin.video_id={alias}.video_id "
        f"AND managed_origin.source_kind IN {MANAGED_SOURCE_KINDS_SQL}) "
        # 旧管理员导入可能缺来源行，只认可确切的导入/上传审计；旧的误上架不算接管。
        "OR EXISTS (SELECT 1 FROM audit_logs managed_audit "
        "WHERE managed_audit.entity_type='viral_video' "
        f"AND managed_audit.entity_id={alias}.platform||':'||{alias}.video_id "
        "AND managed_audit.actor_user_id IS NOT NULL "
        "AND managed_audit.action IN ('viral_video.admin_link_import','viral_video.admin_upload')))"
    )


def ready_sql(alias: str = "v") -> str:
    return (
        f"((COALESCE({alias}.cover_url,'')='' OR {alias}.cover_key IS NOT NULL) AND EXISTS "
        "(SELECT 1 FROM viral_media_preparations cs_media "
        f"WHERE cs_media.platform={alias}.platform AND cs_media.video_id={alias}.video_id "
        "AND cs_media.media_kind='video' AND cs_media.status='SUCCEEDED' "
        "AND cs_media.storage_uri IS NOT NULL))"
    )


def archive_task_status_sql(task: str, video_id: str) -> str:
    return (
        f"CASE WHEN {task}.status='FAILED' AND "
        f"{task}.collection_config_json::jsonb->>'kind'='archive_batch' THEN "
        f"CASE WHEN COALESCE({task}.checkpoint_json::jsonb->'failed_video_ids','[]'::jsonb) "
        f"@> jsonb_build_array({video_id}) THEN 'FAILED' ELSE 'SUCCEEDED' END "
        f"ELSE {task}.status END"
    )


def archive_task_match_sql(task: str, video: str) -> str:
    return (
        f"(({task}.collection_config_json::jsonb->>'kind'='single_archive' AND "
        f"{task}.collection_config_json::jsonb->>'video_id'={video}.video_id) OR "
        f"({task}.collection_config_json::jsonb->>'kind'='archive_batch' AND "
        f"{task}.collection_config_json::jsonb->'video_ids' "
        f"@> jsonb_build_array({video}.video_id)))"
    )


def content_state_sql(alias: str = "v") -> str:
    visibility = (
        "COALESCE((SELECT status FROM viral_video_visibility cs_visibility "
        f"WHERE cs_visibility.platform={alias}.platform "
        f"AND cs_visibility.video_id={alias}.video_id),'AVAILABLE')"
    )
    task = (
        f"(SELECT {archive_task_status_sql('cs_task', f'{alias}.video_id')} "
        "FROM viral_refresh_tasks cs_task "
        f"WHERE cs_task.platform={alias}.platform AND cs_task.sort='latest' "
        f"AND {archive_task_match_sql('cs_task', alias)} "
        "ORDER BY cs_task.created_at DESC,cs_task.id DESC LIMIT 1)"
    )
    failed = (
        "EXISTS (SELECT 1 FROM viral_media_preparations cs_failed "
        f"WHERE cs_failed.platform={alias}.platform AND cs_failed.video_id={alias}.video_id "
        "AND cs_failed.media_kind='video' AND cs_failed.status='FAILED')"
    )
    ready = ready_sql(alias)
    return (
        f"CASE WHEN {alias}.deleted_at IS NOT NULL THEN 'blocked' "
        f"WHEN {visibility} IN ('HIDDEN','UNAVAILABLE') THEN 'removed' "
        f"WHEN {ready} AND {live_homepage_sql(alias)} THEN 'featured' "
        f"WHEN {ready} THEN 'ready' WHEN {task} IN ('PENDING','RUNNING') THEN 'pending_prepare' "
        f"WHEN {failed} OR {task}='FAILED' THEN 'prepare_failed' ELSE 'pending_prepare' END"
    )


def customer_visible_sql(alias: str = "v", *, at: str = "now()") -> str:
    # 只有管理员上传或手工推送授予公共展示权限；历史采集的首页标记不能代替推送。
    # 发布期由运营排期和下架控制，与源站发布时间、采集快照无关。
    predicate = (
        f"({managed_source_sql(alias)} AND {ready_sql(alias)} "
        f"AND {alias}.deleted_at IS NULL AND NOT EXISTS "
        f"(SELECT 1 FROM viral_video_visibility cs_vis WHERE cs_vis.platform={alias}.platform "
        f"AND cs_vis.video_id={alias}.video_id AND cs_vis.status!='AVAILABLE') "
        f"AND regexp_replace({alias}.title,'<em[^>]*>|</em>','','gi') "
        r"!~* 'minecraft|我的世界|\mmc\M' "
        f"AND ({alias}.homepage_starts_at IS NULL OR {alias}.homepage_starts_at<=now()) "
        f"AND ({alias}.homepage_ends_at IS NULL OR {alias}.homepage_ends_at>now()) "
        f"AND {alias}.platform IN ('douyin','wechat_channels','xiaohongshu') "
        f"AND {alias}.collection_published=1 AND {alias}.pushed_at IS NOT NULL)"
    )

    # at 仅由内部 SQL 调用点提供，用同一公开规则计算排期边界两侧的状态。
    return predicate.replace("now()", at)
