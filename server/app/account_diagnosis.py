"""账号诊断业务编排：门禁 → 取数 → 打分 → 落报告.

为什么 P0 在请求内同步执行：

- 一次诊断只有 2~3 次上游调用（主页 + 1~2 页作品），耗时与"链接解析"同量级，
  而链接解析本来就在请求内同步完成；引入 worker 队列会让 P0 多出迁移、租约、
  两处 worker 循环和部署角色同步。
- 事务边界给出一个很强的保证：任一步失败都会回滚，免费额度和积分都不会被吃掉；
  成功的报告才提交，因此不存在"扣了钱但没报告"的中间态。
- 若后续加了星图增强调用导致耗时上升，再迁到 worker，业务接口不变。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.account_diagnosis_actions import (
    build_action_cards,
    build_calendar,
    pick_reference_works,
)
from app.account_diagnosis_entitlement import (
    claim_free_quota,
    current_device_id,
    free_quota_claimed,
    has_paid_package,
    member_required_error,
    wallet_owner,
)
from app.account_diagnosis_llm import (
    build_llm_narrative,
    enrich_action_cards,
    load_narrative_llm_config,
    narrative_llm_disabled,
)
from app.account_diagnosis_scoring import (
    DiagnosisAccount,
    DiagnosisReport,
    DiagnosisSnapshot,
    DiagnosisVideo,
    DimensionScore,
    diagnose,
    report_to_dict,
)
from app.account_link import AccountTarget
from app.billing_catalog import read_tariff
from app.db_portable import BusinessConnection
from app.sub_account_permissions import enforce_sub_account_feature
from app.usage_billing import accept_operation, finish_source
from app.viral_content_state import customer_visible_sql
from app.viral_tikhub import (
    DouyinUserPost,
    DouyinUserProfile,
    DouyinXingtuVideoMetrics,
    ViralSourceClient,
    ViralSourceError,
    ViralSourceUnavailable,
    account_diagnosis_client_from_settings,
)

logger = logging.getLogger(__name__)

SERVICE = "account_diagnosis"
CACHE_TTL = timedelta(hours=24)
POSTS_PAGE_SIZE = 20
# 5 页 × 20 条 = 最多 100 条样本：分布类指标（爆款率/CV/中位偏离）对小样本敏感，
# 40 条常把尾部账号判成"低爆款率"；5 页是耗时（每页一次上游调用）与样本的折中。
POSTS_MAX_PAGES = 5
# 同一目标账号每日计费诊断上限（24h 缓存命中不占额度）。
DAILY_TARGET_LIMIT = 3

STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"


@dataclass(frozen=True)
class AccountSummary:
    sec_user_id: str
    unique_id: str | None
    nickname: str
    avatar_url: str | None
    follower_count: int
    aweme_count: int


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _load_report_row(conn: BusinessConnection, report_id: str) -> Any:
    return conn.execute(
        "SELECT * FROM account_diagnosis_reports WHERE id=%s", (report_id,)
    ).fetchone()


def _payload(row: Any, *, cached: bool = False) -> dict[str, Any]:
    report = json.loads(row["report_json"]) if row["report_json"] else None
    narrative = json.loads(row["narrative_json"]) if row["narrative_json"] else None
    return {
        "id": str(row["id"]),
        "status": str(row["status"]),
        "platform": str(row["platform"]),
        "isFree": bool(row["is_free"]),
        "cached": cached,
        "dataLevel": row["data_level"],
        "createdAt": _iso(row["created_at"]),
        "completedAt": _iso(row["completed_at"]),
        "account": (report or {}).get("account"),
        "report": report,
        "narrative": narrative,
        "error": (
            {"code": row["error_code"], "message": row["error_message"]}
            if row["error_code"]
            else None
        ),
    }


def _find_cached_report(conn: BusinessConnection, owner_id: str, target: AccountTarget) -> Any:
    if target.sec_uid:
        return conn.execute(
            "SELECT * FROM account_diagnosis_reports "
            "WHERE wallet_owner_id=%s AND platform='douyin' AND status='SUCCEEDED' "
            "AND sec_user_id=%s AND completed_at >= %s "
            "ORDER BY completed_at DESC LIMIT 1",
            (owner_id, target.sec_uid, _now() - CACHE_TTL),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM account_diagnosis_reports "
        "WHERE wallet_owner_id=%s AND platform='douyin' AND status='SUCCEEDED' "
        "AND unique_id=%s AND completed_at >= %s "
        "ORDER BY completed_at DESC LIMIT 1",
        (owner_id, target.unique_id, _now() - CACHE_TTL),
    ).fetchone()


def _load_profile(client: ViralSourceClient, target: AccountTarget) -> DouyinUserProfile:
    if target.sec_uid:
        return client.douyin_user_profile(sec_user_id=target.sec_uid)
    if target.unique_id:
        profile = client.douyin_user_profile_by_unique_id(unique_id=target.unique_id)
        if not profile.sec_user_id:
            raise HTTPException(
                404,
                detail={
                    "code": "ACCOUNT_DIAGNOSIS_ACCOUNT_NOT_FOUND",
                    "message": "没有找到该抖音号，请确认账号是否存在或改为粘贴主页链接。",
                },
            )
        return profile
    raise HTTPException(
        422,
        detail={"code": "ACCOUNT_LINK_INVALID", "message": "请提供抖音账号主页链接或抖音号。"},
    )


def _load_posts(client: ViralSourceClient, sec_user_id: str) -> list[DouyinUserPost]:
    posts: list[DouyinUserPost] = []
    cursor = 0
    for _ in range(POSTS_MAX_PAGES):
        page = client.douyin_user_posts(
            sec_user_id=sec_user_id,
            max_cursor=cursor,
            count=POSTS_PAGE_SIZE,
            sort_type=0,
        )
        posts.extend(page.posts)
        if not page.has_more or page.max_cursor is None or page.max_cursor == cursor:
            break
        cursor = page.max_cursor
    return posts


def _snapshot(
    profile: DouyinUserProfile,
    posts: list[DouyinUserPost],
    *,
    collected_at: datetime,
) -> DiagnosisSnapshot:
    videos: list[DiagnosisVideo] = []
    for post in posts:
        created_at = datetime.fromtimestamp(post.created_at, tz=UTC) if post.created_at else None
        videos.append(
            DiagnosisVideo(
                aweme_id=post.aweme_id,
                title=post.title,
                created_at=created_at,
                digg_count=post.digg_count,
                comment_count=post.comment_count,
                share_count=post.share_count,
                collect_count=post.collect_count,
                play_count=post.play_count,
            )
        )
    return DiagnosisSnapshot(
        platform="douyin",
        account=DiagnosisAccount(
            nickname=profile.nickname,
            avatar_url=profile.avatar_url,
            signature=profile.signature,
            province=profile.province,
            ip_location=profile.ip_location,
            unique_id=profile.unique_id,
            follower_count=profile.follower_count,
            total_favorited=profile.total_favorited,
            aweme_count=profile.aweme_count,
        ),
        videos=tuple(videos),
        collected_at=collected_at,
    )


def _account_summary(profile: DouyinUserProfile) -> AccountSummary:
    return AccountSummary(
        sec_user_id=profile.sec_user_id,
        unique_id=profile.unique_id,
        nickname=profile.nickname,
        avatar_url=profile.avatar_url,
        follower_count=profile.follower_count,
        aweme_count=profile.aweme_count,
    )


_SUGGESTIONS: dict[tuple[str, str], str] = {
    (
        "engagement",
        "fan_engagement",
    ): "互动率低于同量级基准：在结尾抛一个具体问题或争议点，把「看完」转成「说两句」。",
    (
        "engagement",
        "structure",
    ): "评论和分享占比偏低：内容偏「看完就走」，加一个可被复述的观点或可转发的清单。",
    (
        "productivity",
        "frequency",
    ): "发布频率偏低：先固定每周 2~3 条的节奏，断更对推荐权重的影响比单条质量更大。",
    (
        "quality",
        "viral_rate",
    ): "爆款率低：把同赛道近 30 天的爆款拆开，复用它的开头 3 秒结构和选题角度。",
    (
        "quality",
        "zero_interaction",
    ): "零互动占比偏高：先排查是否限流（创作者中心账号检测），再谈内容优化。",
    (
        "trend",
        "recent_trend",
    ): "近期互动明显下滑：回看早期高互动作品，检查选题、时长和开头是否偏离。",
    (
        "trend",
        "recency",
    ): "超过两周未更新：先恢复更新，再用一条低成本的日常内容重新激活账号。",
    (
        "trend",
        "concentration",
    ): "流量过度集中在单条作品：把那条爆款拆成系列选题，别让它成为唯一入口。",
    (
        "profile",
        "signature",
    ): "简介信息不足：一句话说清「你是谁 + 帮谁解决什么 + 为什么信你」。",
}
_RISK_SUGGESTIONS: dict[str, str] = {
    "zombie_followers": "粉丝互动率显著偏低：优先做老粉激活（投票、问答），而不是继续投流涨粉。",
    "fake_engagement": "获赞与粉丝结构异常：先核查是否有刷量或异常流量，避免继续放大。",
    "decline": "近期互动较早期腰斩：把早期爆款的结构列出来，逐条对照当前作品找差异。",
    "throttled": "零互动作品占比过高：先做账号检测与申诉，恢复推荐后再恢复更新频率。",
    "inactive": "已断更超过两周：恢复更新前先确认账号状态，避免带着问题继续发。",
    "single_post_dependency": "流量依赖单条作品：用同选题做 3~5 条系列，把偶然爆款变成稳定选题。",
}


def _normalized(dimension: DimensionScore) -> float:
    if dimension.effective_full <= 0:
        return 0.0
    return float(round(dimension.score / dimension.effective_full * 100, 1))


def build_narrative(report: DiagnosisReport) -> dict[str, Any]:
    """用确定性规则生成解读与建议.

    为什么 P0 不上大模型：诊断结论必须可复现、可回归，而文字解读一旦由模型生成，
    同一份数据换个时间就可能给出不同建议，客服和用户都难对账。先用规则把
    「分数 → 短板 → 动作」讲清楚，LLM 只作为后续的可选润色层。
    """

    ranked = sorted(report.dimensions, key=_normalized)
    strengths = [
        {"dimension": item.label, "score": _normalized(item)}
        for item in reversed(ranked)
        if _normalized(item) >= 80
    ][:2]
    weaknesses = [
        {"dimension": item.label, "score": _normalized(item), "comment": item.comment}
        for item in ranked
        if _normalized(item) < 70
    ][:3]

    actions: list[dict[str, str]] = []
    for item in ranked:
        for sub in sorted(
            (sub for sub in item.sub_scores if sub.counted),
            key=lambda sub: sub.full - sub.score,
            reverse=True,
        )[:2]:
            suggestion = _SUGGESTIONS.get((item.key, sub.key))
            if suggestion and sub.score < sub.full * 0.6:
                actions.append(
                    {
                        "dimension": item.label,
                        "title": sub.label,
                        "evidence": sub.detail,
                        "suggestion": suggestion,
                    }
                )
    for risk in report.risks:
        suggestion = _RISK_SUGGESTIONS.get(risk.kind)
        if suggestion:
            actions.append(
                {
                    "dimension": "风险预警",
                    "title": risk.label,
                    "evidence": risk.evidence,
                    "suggestion": suggestion,
                }
            )

    rating_text = {
        "优质": "整体健康，重点是把已经有效的选题做成系列。",
        "正常": "基本盘稳定，优先补短板而不是换方向。",
        "待优化": "有明显短板，先解决互动与更新节奏，再谈放大。",
        "风险": "存在结构性风险，先排查限流/刷量等账号问题，再恢复内容节奏。",
    }[report.rating]
    return {
        "summary": (
            f"综合 {report.score} 分（{report.rating}，{report.tier_label}量级）。{rating_text}"
        ),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "actions": actions[:6],
        "generatedBy": "rules",
    }


def _works_document(posts: list[DouyinUserPost]) -> list[dict[str, Any]]:
    """作品明细 + 复刻推荐标记.

    P0 的报告页要支撑一个主行动：「拆解这条爆款」。把互动最高的 3 条标出来，
    前端只负责渲染按钮，不在客户端重复排序。
    """

    ranked = sorted(
        posts,
        key=lambda post: (
            post.digg_count + post.comment_count + post.share_count + post.collect_count
        ),
        reverse=True,
    )
    recommended = {post.aweme_id for post in ranked[:3]}
    return [
        {
            "awemeId": post.aweme_id,
            "title": post.title,
            "createdAt": (
                datetime.fromtimestamp(post.created_at, tz=UTC).isoformat()
                if post.created_at
                else None
            ),
            "digg": post.digg_count,
            "comment": post.comment_count,
            "share": post.share_count,
            "collect": post.collect_count,
            "interaction": post.digg_count
            + post.comment_count
            + post.share_count
            + post.collect_count,
            "playCount": post.play_count,
            "recommended": post.aweme_id in recommended,
        }
        for post in posts
    ]


def _ensure_configured(conn: BusinessConnection) -> None:
    tariff = read_tariff(conn, SERVICE)
    if tariff is None or not tariff.enabled or not tariff.unit_credits:
        raise HTTPException(
            503,
            detail={
                "code": "ACCOUNT_DIAGNOSIS_NOT_CONFIGURED",
                "message": "账号诊断暂未开放，请联系管理员配置价格后使用。",
            },
        )


def availability(conn: BusinessConnection, *, user_id: str) -> dict[str, Any]:
    owner = wallet_owner(conn, user_id)
    tariff = read_tariff(conn, SERVICE)
    return {
        "enabled": bool(tariff and tariff.enabled and tariff.unit_credits),
        "freeQuota": not free_quota_claimed(conn, owner),
        "member": has_paid_package(conn, owner),
        "priceCredits": (
            float(tariff.unit_credits) if tariff and tariff.unit_credits is not None else None
        ),
    }


def _insert_pending(
    conn: BusinessConnection,
    *,
    report_id: str,
    user_id: str,
    owner_id: str,
    target: AccountTarget,
    input_url: str,
    is_free: bool,
    idempotency_key: str | None,
    created_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO account_diagnosis_reports "
        "(id, user_id, wallet_owner_id, platform, sec_user_id, unique_id, input_url, "
        "status, is_free, idempotency_key, created_at) "
        "VALUES (%s, %s, %s, 'douyin', %s, %s, %s, 'PENDING', %s, %s, %s)",
        (
            report_id,
            user_id,
            owner_id,
            target.sec_uid,
            target.unique_id,
            input_url[:2000],
            1 if is_free else 0,
            idempotency_key,
            created_at,
        ),
    )


def _merge_xingtu_metrics(
    posts: list[DouyinUserPost],
    xingtu: dict[str, DouyinXingtuVideoMetrics],
) -> list[DouyinUserPost]:
    """星图命中的作品用 watch_cnt 补播放量；未命中保持原值."""

    merged: list[DouyinUserPost] = []
    for post in posts:
        item = xingtu.get(post.aweme_id)
        play_count = post.play_count
        if item is not None and item.watch_cnt is not None:
            play_count = item.watch_cnt
        merged.append(
            DouyinUserPost(
                aweme_id=post.aweme_id,
                title=post.title,
                created_at=post.created_at,
                digg_count=post.digg_count,
                comment_count=post.comment_count,
                share_count=post.share_count,
                collect_count=post.collect_count,
                play_count=play_count,
                duration_ms=post.duration_ms,
            )
        )
    return merged


def _avg_rate(values: list[float]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 2)


def _l2_document(xingtu: dict[str, DouyinXingtuVideoMetrics]) -> dict[str, Any] | None:
    if not xingtu:
        return None
    return {
        "source": "xingtu",
        "sampleSize": len(xingtu),
        "avgFinishRate": _avg_rate(
            [item.finish_rate for item in xingtu.values() if item.finish_rate is not None]
        ),
        "avgInteractRate": _avg_rate(
            [item.interact_rate for item in xingtu.values() if item.interact_rate is not None]
        ),
        "finishRates": {
            item_id: item.finish_rate
            for item_id, item in xingtu.items()
            if item.finish_rate is not None
        },
    }


def _load_xingtu_metrics(client: Any, sec_user_id: str) -> dict[str, DouyinXingtuVideoMetrics]:
    """尝试星图增强链路；任何失败都静默回落 L1，绝不阻塞基础报告."""

    kol_id_lookup = getattr(client, "douyin_xingtu_kol_id", None)
    videos_lookup = getattr(client, "douyin_xingtu_author_videos", None)
    if kol_id_lookup is None or videos_lookup is None:
        return {}
    try:
        kol_id = kol_id_lookup(sec_user_id=sec_user_id)
        if not kol_id:
            return {}
        metrics = videos_lookup(o_author_id=str(kol_id), page=1)
        return {item.item_id: item for item in metrics}
    except Exception as exc:  # noqa: BLE001 - L2 是加分项，失败只记日志
        logger.warning("账号诊断星图增强数据不可用（%s），按 L1 出报告", type(exc).__name__)
        return {}


def _load_reference_pool(conn: BusinessConnection) -> list[dict[str, Any]]:
    """与客户爆款库共用可见规则，避免把未推送或下架内容写入诊断报告."""

    rows = conn.execute(
        "SELECT v.video_id, v.title, v.author, v.likes FROM viral_videos v "
        f"WHERE v.platform='douyin' AND {customer_visible_sql('v')} "
        "ORDER BY v.likes DESC NULLS LAST LIMIT 60"
    ).fetchall()
    return [
        {
            "video_id": str(row[0]),
            "title": str(row[1] or ""),
            "author": str(row[2] or ""),
            "likes": int(row[3] or 0),
        }
        for row in rows
    ]


def _load_prior_actions(
    conn: BusinessConnection, *, owner_id: str, target: AccountTarget, exclude_report_id: str
) -> dict[str, Any] | None:
    """取上一次成功报告的行动卡（含机器目标），供修复回顾对比."""

    row = conn.execute(
        "SELECT narrative_json, report_json FROM account_diagnosis_reports "
        "WHERE wallet_owner_id=%s AND platform='douyin' AND status='SUCCEEDED' "
        "AND id <> %s "
        "AND (sec_user_id IS NOT DISTINCT FROM %s) "
        "AND (unique_id IS NOT DISTINCT FROM %s) "
        "AND completed_at >= now() - interval '60 days' "
        "ORDER BY completed_at DESC LIMIT 1",
        (owner_id, exclude_report_id, target.sec_uid, target.unique_id),
    ).fetchone()
    if row is None:
        return None
    narrative = row[0]
    if not narrative:
        return None
    try:
        payload = json.loads(narrative)
    except (json.JSONDecodeError, TypeError):
        return None
    cards = payload.get("actionCards") if isinstance(payload, dict) else None
    if not isinstance(cards, list) or not cards:
        return None
    report_json = row[1]
    prior_metrics: dict[str, Any] = {}
    if report_json:
        try:
            prior_metrics = json.loads(report_json).get("metrics") or {}
        except (json.JSONDecodeError, TypeError):
            prior_metrics = {}
    return {"cards": cards, "priorMetrics": prior_metrics}


def _prior_review(
    prior: dict[str, Any] | None, current_metrics: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """把上次行动卡与本次指标对比，产出修复回顾清单（fixed/open/worse）."""

    if prior is None:
        return None
    prior_metrics = prior.get("priorMetrics") or {}
    review: list[dict[str, Any]] = []
    for card in prior.get("cards", []):
        if not isinstance(card, dict):
            continue
        title = str(card.get("title") or "")
        metric = card.get("metric")
        target_value = card.get("targetValue")
        if not metric or not isinstance(target_value, (int, float)):
            review.append(
                {"title": title, "status": "manual", "note": "人工自查项，请对照上次建议确认"}
            )
            continue
        current = current_metrics.get(metric)
        if not isinstance(current, (int, float)):
            review.append({"title": title, "status": "open", "note": "本次样本不足，未能自动判定"})
            continue
        if current >= target_value:
            note = f"当前 {current} 已达目标 {target_value}"
            status = "fixed"
        elif prior_metrics.get(metric) is not None and current < float(prior_metrics[metric]):
            note = (
                f"当前 {current} 较上次 {prior_metrics[metric]} 进一步下滑（目标 {target_value}）"
            )
            status = "worse"
        else:
            note = f"当前 {current}，目标 {target_value}，仍需继续执行"
            status = "open"
        review.append({"title": title, "status": status, "note": note})
    return review or None


def _run(
    conn: BusinessConnection,
    *,
    report_id: str,
    target: AccountTarget,
    is_free: bool,
    owner_id: str,
) -> dict[str, Any]:
    collected_at = _now()
    conn.execute(
        "UPDATE account_diagnosis_reports SET status=%s WHERE id=%s",
        (STATUS_RUNNING, report_id),
    )
    try:
        client = account_diagnosis_client_from_settings(conn)
        profile = _load_profile(client, target)
        posts = _load_posts(client, profile.sec_user_id)
    except ViralSourceUnavailable as exc:
        raise HTTPException(
            503,
            detail={"code": "ACCOUNT_DIAGNOSIS_UNAVAILABLE", "message": str(exc)},
        ) from exc
    except ViralSourceError as exc:
        # 这里的失败会让整个请求事务回滚：免费额度与预留积分都不会落账。
        raise HTTPException(
            503,
            detail={
                "code": "ACCOUNT_DIAGNOSIS_SOURCE_FAILED",
                "message": "账号数据暂时取不到，本次未扣积分，请稍后重试。",
            },
        ) from exc

    # L2 增强：星图命中时把播放量写回作品、记录完播/互动率，data_level 升级。
    xingtu = _load_xingtu_metrics(client, profile.sec_user_id)
    posts = _merge_xingtu_metrics(posts, xingtu)

    result = diagnose(_snapshot(profile, posts, collected_at=collected_at))
    account = _account_summary(profile)
    document: dict[str, Any] = {
        **report_to_dict(result),
        "platform": "douyin",
        "account": {
            "secUserId": account.sec_user_id,
            "uniqueId": account.unique_id,
            "nickname": account.nickname,
            "avatarUrl": account.avatar_url,
            "followerCount": account.follower_count,
            "awemeCount": account.aweme_count,
        },
        "collectedAt": collected_at.isoformat(),
        "works": _works_document(posts),
        "l2": _l2_document(xingtu),
    }

    # 行动卡引擎（规则层）：短板 → 可执行动作卡（问题锚点/可计算归因/待自查/
    # 分步做法/Before-After 话术/目标值/优先级），再补同赛道参考爆款与修复回顾。
    report_dict = report_to_dict(result)
    cards = build_action_cards(_snapshot(profile, posts, collected_at=collected_at), report_dict)
    prior = _load_prior_actions(
        conn,
        owner_id=owner_id,
        target=target,
        exclude_report_id=report_id,
    )
    review = _prior_review(prior, result.metrics)
    calendar = build_calendar([card.to_dict() for card in cards], collected_at)
    refs = pick_reference_works(
        _load_reference_pool(conn), _snapshot(profile, posts, collected_at=collected_at)
    )

    narrative = build_narrative(result)

    # LLM 解读：打分不变，只替换解读文案；未配置/失败/开关关闭都回落规则。
    cards_dict = [card.to_dict() for card in cards]
    if not narrative_llm_disabled():
        llm_config = load_narrative_llm_config(conn)
        if llm_config is not None:
            llm = build_llm_narrative(
                base_url=llm_config[0],
                api_key=llm_config[1],
                model=llm_config[2],
                report=document,
            )
            if llm is not None:
                narrative = {**narrative, **llm}
            # 行动卡话术升级：LLM 把规则步骤重写成贴合账号类目的具体做法
            # （含 Before/After 话术）；逐卡校验，未命中的卡保留规则文案。
            enriched = enrich_action_cards(
                base_url=llm_config[0],
                api_key=llm_config[1],
                model=llm_config[2],
                cards=cards_dict,
                titles=[v.title for v in posts if v.title][:12],
            )
            if enriched:
                for card in cards_dict:
                    patch = enriched.get(card["key"])
                    if patch:
                        card["steps"] = patch["steps"]
                        if patch.get("beforeAfter"):
                            card["beforeAfter"] = patch["beforeAfter"]
                        card["generatedBy"] = "llm"
                    else:
                        card["generatedBy"] = "rules"

    document["actionCards"] = cards_dict
    document["referenceWorks"] = [ref.to_dict() for ref in refs]
    document["priorReview"] = review
    document["calendar"] = [week.to_dict() for week in calendar]

    conn.execute(
        "UPDATE account_diagnosis_reports SET "
        "status='SUCCEEDED', sec_user_id=%s, unique_id=%s, data_level=%s, score=%s, "
        "tier=%s, rating=%s, report_json=%s, narrative_json=%s, completed_at=%s "
        "WHERE id=%s",
        (
            account.sec_user_id,
            account.unique_id,
            result.data_level,
            result.score,
            result.tier,
            result.rating,
            json.dumps(document, ensure_ascii=False),
            json.dumps(narrative, ensure_ascii=False),
            _now(),
            report_id,
        ),
    )
    if not is_free:
        finish_source(conn, report_id, units=1, succeeded=True, service=SERVICE)
    row = _load_report_row(conn, report_id)
    return _payload(row)


def _daily_target_count(conn: BusinessConnection, owner_id: str, target: AccountTarget) -> int:
    """当天（库时区）对该目标账号已完成的有效诊断数."""

    row = conn.execute(
        "SELECT count(*) FROM account_diagnosis_reports "
        "WHERE wallet_owner_id=%s AND status='SUCCEEDED' "
        "AND created_at >= date_trunc('day', now()) "
        "AND (sec_user_id IS NOT DISTINCT FROM %s) "
        "AND (unique_id IS NOT DISTINCT FROM %s)",
        (owner_id, target.sec_uid, target.unique_id),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def create_diagnosis(
    conn: BusinessConnection,
    *,
    user_id: str,
    target: AccountTarget,
    input_url: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """受理一次诊断：缓存 → 免费额度 → 会员 → 计费 → 取数打分."""

    _ensure_configured(conn)
    # 从账号功能权限要在免费与计费两条路上都生效；accept_operation 只覆盖计费路。
    enforce_sub_account_feature(conn, actor_id=user_id, service=SERVICE)
    owner = wallet_owner(conn, user_id)

    # 同一个幂等键 = 同一次受理：网络重试、按钮连点都返回同一份报告，不重复扣分。
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM account_diagnosis_reports "
            "WHERE wallet_owner_id=%s AND idempotency_key=%s LIMIT 1",
            (owner, idempotency_key),
        ).fetchone()
        if existing is not None:
            return _payload(existing, cached=True)

    cached = _find_cached_report(conn, owner, target)
    if cached is not None:
        return _payload(cached, cached=True)

    # 同一目标账号每日 ≤3 次（缓存命中不占额度）：防止单账号被反复外呼刷上游成本。
    if _daily_target_count(conn, owner, target) >= DAILY_TARGET_LIMIT:
        raise HTTPException(
            429,
            detail={
                "code": "ACCOUNT_DIAGNOSIS_DAILY_LIMIT",
                "message": f"同一账号每天最多诊断 {DAILY_TARGET_LIMIT} 次，请明天再试。",
            },
        )

    # 同一付费主体同时只允许一个诊断在跑：诊断是同步外呼，并发双开只会
    # 放大上游成本与耗时，排队语义在同步模型里等价于"拒绝第二次"。
    lock_row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
        (f"account_diagnosis:owner:{owner}",),
    ).fetchone()
    if lock_row is None or not lock_row[0]:
        raise HTTPException(
            409,
            detail={
                "code": "ACCOUNT_DIAGNOSIS_IN_PROGRESS",
                "message": "已有一个诊断正在进行，请等待完成后再试。",
            },
        )

    report_id = str(uuid4())
    device_id = current_device_id(conn, user_id)
    is_free = claim_free_quota(conn, owner_id=owner, device_id=device_id, report_id=report_id)
    if not is_free and not has_paid_package(conn, owner):
        raise member_required_error()

    _insert_pending(
        conn,
        report_id=report_id,
        user_id=user_id,
        owner_id=owner,
        target=target,
        input_url=input_url,
        is_free=is_free,
        idempotency_key=idempotency_key,
        created_at=_now(),
    )
    if not is_free:
        # 预留积分；余额不足时 accept_operation 抛 402，事务回滚，报告行与免费额度都不留。
        accept_operation(
            conn,
            user_id=user_id,
            service=SERVICE,
            source_id=report_id,
            units=1,
        )
    return _run(conn, report_id=report_id, target=target, is_free=is_free, owner_id=owner)


def get_report(conn: BusinessConnection, *, user_id: str, report_id: str) -> dict[str, Any]:
    owner = wallet_owner(conn, user_id)
    row = conn.execute(
        "SELECT * FROM account_diagnosis_reports WHERE id=%s AND wallet_owner_id=%s",
        (report_id, owner),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail={"code": "REPORT_NOT_FOUND", "message": "报告不存在。"})
    return _payload(row)


def list_reports(
    conn: BusinessConnection, *, user_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    owner = wallet_owner(conn, user_id)
    rows = conn.execute(
        "SELECT * FROM account_diagnosis_reports WHERE wallet_owner_id=%s "
        "ORDER BY created_at DESC LIMIT %s",
        (owner, max(1, min(limit, 50))),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _payload(row)
        report = payload.get("report") or {}
        items.append(
            {
                "id": payload["id"],
                "status": payload["status"],
                "score": (report or {}).get("score"),
                "tier": (report or {}).get("tier"),
                "rating": (report or {}).get("rating"),
                "isFree": payload["isFree"],
                "createdAt": payload["createdAt"],
                "account": payload.get("account"),
            }
        )
    return items
