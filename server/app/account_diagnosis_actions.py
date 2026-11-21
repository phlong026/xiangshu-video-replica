"""账号诊断的行动卡引擎：把短板翻译成可执行动作（规则层，确定性）.

三个设计决策（为什么不在 LLM 里一起做）：

1. **目标值/优先级/数据锚点全部规则计算**——同一份快照必须得到同一组行动卡，
   可回归测试；LLM 只负责把卡的"怎么做"写成更具体的话术（见 llm 模块）。
2. **归因只写可计算信号**：标题句式相似度（n-gram）、发布节奏、互动结构、
   爆款集中度；凡涉及视频内容本身的（结尾钩子、作者回复）一律归入
   "待自查"，公开数据拿不到，不能编造事实。
3. **动作模板按类目可扩展**：默认通用模板，装修类目为首发内置（本产品
   主客群）；动作示例（钩子句式/选题结构）来自行业公开方法论与产品主客群
   运营经验的整理，不承诺具体收益数字，目标值只锚定量级基准线。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from app.account_diagnosis_scoring import (
    DiagnosisSnapshot,
    DiagnosisVideo,
    baseline_for_tier,
)

Priority = Literal["P0", "P1", "P2"]

# 零互动阈值（与 scoring 保持一致：点赞<10 视为零互动）。
_ZERO_DIGG = 10
# 标题句式相似度告警线：超过该值说明选题/标题同质化。
_TITLE_SIMILAR_WARN = 0.4
# 发布频率断更线（条/天）。
CADENCE_MIN = 0.5
# 装修/家装类目关键词（用于命中类目专属动作模板）。
_RENOVATION_KEYWORDS = re.compile(r"装修|设计|家装|改造|旧房|户型|定制|工地|实景|老破小|全屋")


@dataclass(frozen=True)
class ActionCard:
    key: str
    priority: Priority
    dimension: str
    title: str
    problem: str
    evidence: tuple[str, ...]
    self_checks: tuple[str, ...]
    steps: tuple[str, ...]
    before_after: tuple[tuple[str, str], ...]
    target: str
    # 机器可读目标（修复回顾用）：metric 对应 metrics 键；None = 人工自查项。
    metric: str | None
    target_value: float | None
    due_week: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "priority": self.priority,
            "dimension": self.dimension,
            "title": self.title,
            "problem": self.problem,
            "evidence": list(self.evidence),
            "selfChecks": list(self.self_checks),
            "steps": list(self.steps),
            "beforeAfter": [{"before": b, "after": a} for b, a in self.before_after],
            "target": self.target,
            "metric": self.metric,
            "targetValue": self.target_value,
            "dueWeek": self.due_week,
        }


@dataclass(frozen=True)
class RefWork:
    """同赛道参考爆款（从已发布爆款池中按标题相似度+热度挑选）."""

    video_id: str
    title: str
    author: str
    likes: int
    score: float
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "videoId": self.video_id,
            "title": self.title,
            "author": self.author,
            "likes": self.likes,
            "score": self.score,
            "url": self.url,
        }


@dataclass(frozen=True)
class CalendarWeek:
    label: str
    days: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "days": list(self.days)}


def _char_bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _similarity(a: str, b: str) -> float:
    ga, gb = _char_bigrams(a), _char_bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _avg_pairwise_similarity(titles: list[str]) -> float:
    if len(titles) < 2:
        return 0.0
    pairs = 0
    total = 0.0
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            total += _similarity(titles[i], titles[j])
            pairs += 1
    return total / pairs if pairs else 0.0


def _interaction_struct_ratio(posts: list[DiagnosisVideo]) -> float | None:
    total_digg = sum(v.digg_count for v in posts)
    if total_digg <= 0 or not posts:
        return None
    return round(sum(v.comment_count for v in posts) / total_digg * 100, 2)


def _recent_14d_count(posts: list[DiagnosisVideo], now: datetime) -> int:
    return sum(1 for v in posts if v.created_at and (now - v.created_at).days <= 14)


def _category_posts(posts: list[DiagnosisVideo]) -> bool:
    titles = " ".join(v.title for v in posts)
    return bool(_RENOVATION_KEYWORDS.search(titles))


def _interact_card(
    report: dict[str, Any],
    baseline: dict[str, float],
    evidence: list[str],
    self_checks: list[str],
    renovation: bool,
) -> ActionCard:
    good = baseline["engagement_good"]
    rate = report["metrics"].get("engagement_rate")
    rate_text = f"{rate}%" if isinstance(rate, (int, float)) else "样本不足"
    if renovation:
        hook = "你家厨房做 U 型还是 L 型？评论区告诉我，下期按点赞最高的方案出报价清单。"
        title_example = "评论区留户型图，免费出布局建议"
    else:
        hook = "你最纠结的问题是什么？评论区告诉我，下期专门出一期。"
        title_example = "评论区征集：下期想看什么？"
    steps = (
        f"结尾 3 秒固定放 1 个开放式提问，替代「感谢观看」类收尾；话术参考：{hook}",
        "发布后 1 小时内回复前 10 条评论（黄金互动窗口），把带争议锚点的评论置顶。",
        f"简介区补一句互动承诺（如「{title_example}」），把被动等评论变成固定入口。",
    )
    return ActionCard(
        key="interact-hook",
        priority="P0",
        dimension="互动健康度",
        title="把「看完就走」变成「看完留言」——互动钩子改造",
        problem=(
            f"粉丝互动率 {rate_text}，低于本量级良好线 {good}%；"
            "互动健康度占综合权重 30%，是当前最大失分项。"
        ),
        evidence=tuple(evidence),
        self_checks=(
            "自查：最近作品的结尾 3 秒是否有互动提问（视频内容，公开数据看不到）？",
            "自查：发布后 1 小时内是否回复过评论？",
            "自查：简介是否有互动承诺？",
        ),
        steps=steps,
        before_after=(("感谢观看，关注我看更多干货。", f"{hook}"),),
        target=f"粉丝互动率 ≥ {good}%（下次诊断对照本量级良好线）",
        metric="engagement_rate",
        target_value=good,
        due_week=1,
    )


def _topic_card(
    report: dict[str, Any],
    baseline: dict[str, float],
    evidence: list[str],
    self_checks: list[str],
    renovation: bool,
) -> ActionCard:
    viral_line = baseline["viral_line"]
    if renovation:
        picks = (
            "改造前后反差（烂→惊艳，前 3 秒先展示最差现状）",
            "清单体（最不该省的 5 项/最贵的 3 个决定）",
            "争议锚点（全屋定制 vs 木工打，让评论区站队）",
        )
    else:
        picks = (
            "前后反差对比（问题现状 → 解决后效果）",
            "清单体（最常见误区 TOP3）",
            "争议锚点（两种主流做法二选一，让评论区站队）",
        )
    steps = (
        f"本周先拍 3 条已验证结构：{'；'.join(picks)}。",
        "建立 ≥20 条的选题池：每条注明钩子句式 + 拍摄场景 + 预计时长，不再当天想当天拍。",
        "标题句式连用不超过 2 条：数字 + 情绪词开头优先（参考你自己互动最高的那条的标题结构）。",
    )
    return ActionCard(
        key="topic-pool",
        priority="P0",
        dimension="内容质量",
        title="建立选题池——用已验证的爆款结构替代单一选题",
        problem=(
            f"爆款率未达本量级爆款线（触发线：单条点赞 > 粉丝数×{viral_line}%）；"
            "内容质量占权重 20%。"
        ),
        evidence=tuple(evidence),
        self_checks=(
            "自查：最近 3 条标题是否同一句式？",
            "自查：选题是否来自固定选题池（而非当天临时想）？",
        ),
        steps=steps,
        before_after=(),
        target=f"爆款率 ≥ {viral_line * 2}%（爆款触发线的 2 倍，下次诊断验收）",
        metric="viral_rate",
        target_value=viral_line * 2,
        due_week=1,
    )


def _cadence_card(
    evidence: list[str],
    self_checks: list[str],
) -> ActionCard:
    return ActionCard(
        key="cadence",
        priority="P1",
        dimension="内容趋势",
        title="修复发布节奏——断更是衰退与掉互动的第一诱因",
        problem="发布频率或最新活跃度未达健康线：低于 0.5 条/天即接近断更。",
        evidence=tuple(evidence),
        self_checks=(
            "自查：近两周是否出现 ≥7 天的断更窗口？",
            "自查：是否有可垫档的轻量内容素材（工地/日常 30 秒手机实拍）？",
        ),
        steps=(
            "每周固定 3 条，选粉丝活跃时段发布（默认周二/四/六 18:00 起）。",
            "拍不完时用轻量内容垫档：宁拍轻不断更，断更对权重的影响大于单条质量。",
            "每周日固定 30 分钟复盘当周数据并锁定下周选题（从选题池取）。",
        ),
        before_after=(),
        target="发布频率 ≥ 0.5 条/天 且最新活跃度 ≤ 7 天（下次诊断验收）",
        metric="publish_frequency",
        target_value=0.5,
        due_week=1,
    )


def _profile_card(report: dict[str, Any], renovation: bool) -> ActionCard:
    if renovation:
        after = "深耕旧房改造 8 年｜每周 3 条真实工地实拍｜评论区留户型图，免费出布局建议"
    else:
        after = "帮谁解决什么 + 你的独特方法/经历 + 互动承诺（评论区告诉我你的情况）"
    return ActionCard(
        key="profile",
        priority="P2",
        dimension="账号基础画像",
        title="补全简介定位句——5 分钟改完",
        problem="简介信息完整度不足：缺少「帮谁 + 解决什么 + 为什么信你」三要素。",
        evidence=("画像维度是零成本失分项：改一次文案即可拿满。",),
        self_checks=("自查：简介是否含'帮谁 + 解决什么 + 为什么信你'三要素？",),
        steps=(
            f"按模板重写简介：{after}",
            "头像与背景图同步检查：清晰、无过期活动信息。",
        ),
        before_after=(),
        target="简介 ≥ 15 字且含互动承诺（画像维度拿满）",
        metric=None,
        target_value=None,
        due_week=2,
    )


def build_action_cards(snapshot: DiagnosisSnapshot, report: dict[str, Any]) -> list[ActionCard]:
    """由诊断报告 + 快照生成行动卡（确定性，无 LLM）.

    触发条件与 scoring 的阈值同源（同一套 S/A/B/C 基准），保证"分数低 →
    一定有对应行动卡"。
    """

    posts = list(snapshot.videos)
    baseline = baseline_for_tier(report["tier"])
    metrics = report["metrics"]
    risks = {r["kind"] for r in report["risks"]}
    dim_scores = {d["key"]: d for d in report["dimensions"]}
    renovation = _category_posts(posts)
    now = snapshot.collected_at

    titles = [v.title for v in posts if v.title][:12]
    similarity = round(_avg_pairwise_similarity(titles), 2)
    struct_ratio = _interaction_struct_ratio(posts)
    recent14 = _recent_14d_count(posts, now)
    frequency = metrics.get("publish_frequency")
    days_since = metrics.get("days_since_latest")
    engagement_rate = metrics.get("engagement_rate")

    cards: list[ActionCard] = []

    # 互动健康度（30% 权重）：低于良好线即 P0
    eng_dim = dim_scores.get("engagement")
    eng_bad = (
        eng_dim is not None
        and eng_dim["effective_full"] > 0
        and (engagement_rate is None or engagement_rate < baseline["engagement_good"])
    )
    if eng_bad:
        evidence = [
            f"评论占赞比 {struct_ratio}%（健康线 ≥4%）"
            if struct_ratio is not None
            else "互动结构比未达健康线"
        ]
        if similarity >= _TITLE_SIMILAR_WARN:
            evidence.append(
                f"近 {len(titles)} 条标题句式相似度 {int(similarity * 100)}%（>40% 视为同质化）"
            )
        cards.append(_interact_card(report, baseline, evidence, [], renovation))

    # 内容质量/趋势：爆款率低、集中度高 → 选题池 P0
    qual_dim = dim_scores.get("quality")
    qual_bad = (
        qual_dim is not None
        and qual_dim["effective_full"] > 0
        and qual_dim["score"] / qual_dim["effective_full"] < 0.6
    )
    topic_evidence = []
    if similarity >= _TITLE_SIMILAR_WARN:
        topic_evidence.append(
            f"近 {len(titles)} 条标题句式相似度 {int(similarity * 100)}%（>40% 即同质化），"
            "爆款集中度提示流量依赖单条"
        )
    if "decline" in risks or "single_post_dependency" in risks:
        topic_evidence.append("风险预警存在衰退/单条依赖：选题结构复制是第一优先动作")
    if qual_bad or topic_evidence:
        cards.append(_topic_card(report, baseline, topic_evidence, [], renovation))

    # 节奏：频率<0.5 或 活跃度>7 天 → P1
    cadence_evidence = []
    if recent14 == 0 and posts:
        cadence_evidence.append("近 14 天 0 条新作品")
    elif recent14 < 6:
        cadence_evidence.append(f"近 14 天仅 {recent14} 条（健康线 6 条）")
    if days_since is not None and days_since > 7:
        cadence_evidence.append(f"最近一条作品距今 {days_since} 天（>7 天触发断更预警）")
    if frequency is not None and frequency < CADENCE_MIN:
        cadence_evidence.append(f"发布频率 {frequency:.2f} 条/天（< {CADENCE_MIN} 接近断更）")
    if cadence_evidence:
        cards.append(_cadence_card(cadence_evidence, []))

    # 画像：P2 顺手改
    prof_dim = dim_scores.get("profile")
    if prof_dim is not None and prof_dim["score"] < prof_dim["effective_full"]:
        cards.append(_profile_card(report, renovation))

    # 优先级微调：含衰退风险的行动卡提前
    cards.sort(key=lambda c: (c.priority, c.due_week, c.key))
    return cards


def pick_reference_works(
    rows: list[dict[str, Any]],
    snapshot: DiagnosisSnapshot,
    *,
    limit: int = 3,
) -> list[RefWork]:
    """从已发布爆款池里挑同赛道参考（标题相似度为主、热度保底）.

    ``rows`` 为调用方查好的候选（dict：video_id/title/author/likes）。
    相似度 0 的候选仍保留但排最后：账号标题与爆款池完全无交集时，
    给纯热度榜也有参考价值。
    """

    titles = [v.title for v in snapshot.videos if v.title]
    scored: list[RefWork] = []
    max_likes = max((int(r.get("likes") or 0) for r in rows), default=0) or 1
    for row in rows:
        title = str(row.get("title") or "")
        likes = int(row.get("likes") or 0)
        sim = max((_similarity(title, t) for t in titles), default=0.0)
        heat = likes / max_likes
        score = round(sim * 8 + heat * 2, 1)
        scored.append(
            RefWork(
                video_id=str(row.get("video_id") or ""),
                title=title,
                author=str(row.get("author") or ""),
                likes=likes,
                score=score,
                url=f"https://www.douyin.com/video/{row.get('video_id')}",
            )
        )
    scored = [w for w in scored if w.video_id]
    scored.sort(key=lambda w: w.score, reverse=True)
    return scored[:limit]


def build_calendar(cards: list[dict[str, Any]], collected_at: datetime) -> list[CalendarWeek]:
    """按行动卡 dueWeek 生成 2 周改进日历（每周 3 条 + 周日复盘）.

    排期是节奏建议而非精确保证：粉丝活跃时段数据（精确到小时）公开拿不到，
    固定 18:00 是行业通用稳妥值；周日晚固定"复盘 → 再次诊断"。
    """

    start = collected_at.date() + timedelta(days=1)
    while start.weekday() != 0:  # 对齐到下周一
        start += timedelta(days=1)
    publish_labels: dict[str, str] = {}
    for card in cards:
        if card["dueWeek"] == 1 and "week1" not in publish_labels:
            publish_labels["week1"] = str(card["title"])[:18]
        if card["dueWeek"] == 2 and "week2" not in publish_labels:
            publish_labels["week2"] = str(card["title"])[:18]
    weeks: list[CalendarWeek] = []
    for week_index in (1, 2):
        days: list[dict[str, Any]] = []
        monday = start + timedelta(weeks=week_index - 1)
        published = 0
        for offset in range(7):
            day = monday + timedelta(days=offset)
            if offset in (1, 3, 5):  # 周二/四/六发布
                if published == 0 and (label := publish_labels.get(f"week{week_index}")):
                    task = f"发布：{label}（新结尾钩子）"
                else:
                    task = "发布：选题池轮换 + 新结尾钩子"
                published += 1
            elif offset == 6:
                task = "周复盘 → 再次诊断"
            else:
                task = "回复评论区 / 备素材"
            days.append(
                {
                    "date": day.isoformat(),
                    "weekday": offset + 1,
                    "task": task,
                    "rest": offset in (0, 2, 4),
                }
            )
        weeks.append(CalendarWeek(label=f"第 {week_index} 周", days=tuple(days)))
    return weeks
