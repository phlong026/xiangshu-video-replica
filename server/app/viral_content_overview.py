"""同采集批次转化与业务台账汇总；历史未观测数据不推断、不分摊。"""

from datetime import date

from app.billing_reports import date_bounds
from app.db_portable import BusinessConnection
from app.viral_content_cohort import STAGES, cohort_sql
from app.viral_resource_metering import resource_records, resource_summary


def content_overview(
    conn: BusinessConnection, *, start: date, end: date, platform: str | None = None
) -> dict[str, object]:
    lower, upper = date_bounds(start, end)
    started = conn.execute(
        "SELECT started_at FROM viral_content_measurement_state WHERE id=1"
    ).fetchone()[0]
    cohort, cohort_params = cohort_sql(start, end, platform)
    counts = conn.execute(
        f"WITH cohort AS ({cohort}) SELECT "
        + ",".join(f"count(*) FILTER(WHERE {condition})" for condition in STAGES.values())
        + " FROM cohort",
        tuple(cohort_params),
    ).fetchone()
    steps = []
    previous = None
    for index, name in enumerate(("本期采集", "素材就绪", "首页展示", "客户打开详情")):
        count = int(counts[index])
        steps.append(
            {
                "stage": list(STAGES)[index],
                "name": name,
                "count": count,
                "conversion": count / previous if previous else None,
            }
        )
        previous = count
    revenue = conn.execute(
        """SELECT service,count(*),COALESCE(sum(revenue_fen),0),
          count(*) FILTER(WHERE revenue_fen IS NULL)
          FROM billing_operations o WHERE (service IN ('viral_search','viral_detail','viral_copy')
          OR (service='asr' AND EXISTS(SELECT 1 FROM script_from_audio_tasks t
              WHERE t.id=o.source_id
              AND jsonb_typeof(t.request_json::jsonb->'viral_source')='array')))
          AND user_id IS NOT NULL AND state='SUCCEEDED'
          AND completed_at>=%s AND completed_at<%s GROUP BY service ORDER BY service""",
        (lower, upper),
    ).fetchall()
    costs = conn.execute(
        """SELECT COALESCE(sum(a.effective_cost_fen),0),
          count(*) FILTER(WHERE a.effective_cost_fen IS NULL) FROM billing_effective_attempts a
          JOIN billing_operations o ON o.id=a.operation_id
          WHERE a.created_at>=%s AND a.created_at<%s AND
          (o.service IN ('viral_search','viral_detail','viral_copy')
            OR (o.service='viral_data' AND o.user_id IS NULL)
            OR (o.service='asr' AND EXISTS(SELECT 1 FROM script_from_audio_tasks t
              WHERE t.id=o.source_id
              AND jsonb_typeof(t.request_json::jsonb->'viral_source')='array')))""",
        (lower, upper),
    ).fetchone()
    missing = conn.execute(
        """SELECT count(*) FROM billing_operations o
          WHERE o.service='viral_data' AND o.user_id IS NULL
          AND o.state IN ('SUCCEEDED','FAILED') AND COALESCE(o.completed_at,o.created_at)>=%s
          AND COALESCE(o.completed_at,o.created_at)<%s AND NOT EXISTS
          (SELECT 1 FROM billing_attempts a WHERE a.operation_id=o.id)""",
        (lower, upper),
    ).fetchone()[0]
    known_revenue = sum(float(row[2]) for row in revenue)
    unknown_revenue = sum(int(row[3]) for row in revenue)
    known_cost = float(costs[0])
    unknown_cost = int(costs[1]) + int(missing)
    resources = resource_summary(resource_records(conn, lower=lower, upper=upper))
    resource_known = float(resources["knownCostFen"])
    resource_unknown = sum(row["cost_fen"] is None for row in resources["records"])
    resources.pop("records")
    top = conn.execute(
        """SELECT e.platform,e.video_id,v.title,count(*) AS requests
          FROM viral_content_usage_events e LEFT JOIN viral_videos v
          ON v.platform=e.platform AND v.video_id=e.video_id
          WHERE e.created_at>=%s AND e.created_at<%s GROUP BY e.platform,e.video_id,v.title
          ORDER BY count(*) DESC,e.platform,e.video_id LIMIT 10""",
        (lower, upper),
    ).fetchall()
    keywords = conn.execute(
        # C10 归因去重：同一视频被多个关键词命中时只归给最早命中的那个词，
        # 否则关键词榜的 collected 之和会大于真实采集数、效果被稀释。
        """WITH first_hit AS (
             SELECT DISTINCT ON (platform,video_id) platform,video_id,keyword
             FROM viral_content_sources
             WHERE collected_at>=%s AND collected_at<%s AND keyword<>''
             ORDER BY platform,video_id,collected_at,batch_id
           ), flagged AS (
             SELECT f.keyword,f.platform,f.video_id,
                    (MAX(s.detail_at) IS NOT NULL) AS used_any
             FROM first_hit f
             JOIN viral_content_sources s
               ON s.platform=f.platform AND s.video_id=f.video_id
             GROUP BY f.keyword,f.platform,f.video_id
           )
           SELECT keyword,platform,count(*) AS collected,
             count(*) FILTER(WHERE used_any) AS used
           FROM flagged GROUP BY keyword,platform
           ORDER BY count(*) FILTER(WHERE used_any)::numeric
                    /NULLIF(count(*),0) DESC,keyword,platform""",
        (lower, upper),
    ).fetchall()
    kind_params: list[object] = [lower, upper]
    kind_platform_filter = ""
    if platform:
        kind_platform_filter = " AND s.platform=%s"
        kind_params.append(platform)
    sources_by_kind = conn.execute(
        "SELECT s.source_kind,count(DISTINCT (s.platform,s.video_id)) AS videos "
        "FROM viral_content_sources s WHERE s.collected_at>=%s AND s.collected_at<%s "
        "AND s.platform IN ('douyin','wechat_channels')"
        + kind_platform_filter
        + " GROUP BY s.source_kind ORDER BY videos DESC,s.source_kind",
        tuple(kind_params),
    ).fetchall()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "platform": platform,
        "measurementStartedAt": str(started),
        "cohortRule": (
            "同一区间进入内容池的视频（关键词采集、管理端搜索、客户导入）"
            "按平台和视频去重，后续阶段只跟踪这一批；逐步交集转化，"
            "不混入全部库存；保留已屏蔽的视频，已永久清理的视频无法追踪。"
        ),
        "historyNote": "采集来源和实际读取从起计时点记录；更早的批次、读取次数无法还原。",
        "funnel": steps,
        "sourcesByKind": [{"sourceKind": str(r[0]), "videos": int(r[1])} for r in sources_by_kind],
        "finance": {
            "knownRevenueFen": known_revenue,
            "unknownRevenueCount": unknown_revenue,
            "revenueFen": None if unknown_revenue else known_revenue,
            "knownCostFen": known_cost + resource_known,
            "unknownCostCount": unknown_cost + resource_unknown,
            "knownInterfaceCostFen": known_cost,
            "knownResourceCostFen": resource_known,
            "resources": resources,
            "recordedCostFen": None if unknown_cost else known_cost,
            "costCoverageComplete": False,
            "costFen": None,
            "knownLedgerGrossFen": known_revenue - known_cost - resource_known,
            "grossFen": None,
            "grossRate": None,
            "services": [
                {
                    "service": str(r[0]),
                    "operations": int(r[1]),
                    "knownRevenueFen": float(r[2]),
                    "unknownRevenueCount": int(r[3]),
                }
                for r in revenue
            ],
            "countingRule": (
                "收入为区间成功搜索、详情、文案和精确关联视频的转写台账；"
                "成本为区间对应业务及平台爆款数据接口的有效调用成本与资源事件确认账单，"
                "包含失败调用。不按视频数量分摊。历史平台调用未能区分采集/准备/互动，分类无法还原。"
                "新素材资源事件不推测供应商计价，历史及云端直接流量证据仍不完整，"
                "完整成本和业务毛利保持待核对；已确认金额仍单列。"
            ),
        },
        "topVideos": [dict(r) for r in top],
        "bestKeywords": [dict(r) for r in keywords[:5]],
        "worstKeywords": [dict(r) for r in list(reversed(keywords))[:5]],
        "keywordRule": (
            "按本期采集视频获得客户详情读取的比例排序；仅统计带关键词的来源"
            "（关键词批次采集与管理端搜索），客户导入不参与关键词榜。"
        ),
    }
