"""概览和列表共享同批次、逐步交集条件，避免点击后的数量换了口径。"""

from datetime import date
from typing import Literal

from app.billing_reports import date_bounds

CohortStage = Literal["collected", "prepared", "homepage", "detail"]
STAGES: dict[str, str] = {
    "collected": "TRUE",
    "prepared": "prepared",
    "homepage": "prepared AND featured",
    "detail": "prepared AND featured AND detail",
}


def cohort_sql(
    start: date, end: date, platform: str | None = None, keyword: str = ""
) -> tuple[str, list[object]]:
    lower, upper = date_bounds(start, end)
    params: list[object] = [lower, upper]
    platform_filter = ""
    if platform:
        platform_filter = " AND s.platform=%s"
        params.append(platform)
    if keyword.strip():
        platform_filter += " AND s.keyword=%s"
        params.append(keyword.strip())
    # 2026-11-11 共享缓存退役：漏斗第 5 段「获取文案」（copy_at）停更删除；
    # copy_at 列保留在表中但不再被任何口径消费。
    return (
        "SELECT s.platform,s.video_id,bool_or(s.prepared_at IS NOT NULL) AS prepared,"
        "bool_or(s.homepage_at IS NOT NULL) AS featured,bool_or(s.detail_at IS NOT NULL) AS detail "
        "FROM viral_content_sources s "
        "JOIN viral_videos v ON v.platform=s.platform AND v.video_id=s.video_id "
        "WHERE s.collected_at>=%s AND s.collected_at<%s "
        "AND s.platform IN ('douyin','wechat_channels')"
        + platform_filter
        + " GROUP BY s.platform,s.video_id",
        params,
    )
