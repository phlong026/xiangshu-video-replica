"""抖音账号诊断的确定性打分引擎.

设计约束（为什么不写成一次性的报告生成器）：

1. **纯函数**：不读库、不调上游、不取当前时间。所有"现在"都来自 ``collected_at``，
   同一份快照必然得到同一个分数——否则回归测试和"历史分数曲线"都不可信。
2. **量级自适应**：同一个互动率对 100 万粉和 5000 粉含义完全不同，所有比率类
   指标都按 S/A/B/C 四档基准判定，而不是用一套全局阈值。
3. **有效满分**：样本退化（如 2 条作品时中位数恒等于均值）或分母为 0 的子项
   从分母剔除，不奖不罚；只有"账号确实没有作品"才按最低档计 0 分并保留分母，
   因为"没内容"本身就是负面信号。
4. **口径诚实**：公开数据拿不到播放量和完播率，因此所有比率都以粉丝数或互动量
   为分母，并在结果里标注 ``data_level``；拿到播放数据（星图）时才升级为 L2。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Rating = Literal["优质", "正常", "待优化", "风险"]
RiskLevel = Literal["high", "medium", "low"]
DataLevel = Literal["L1", "L2"]

# 作品样本少于该值时提示置信度不足（对齐行业惯例：5 条以下不做分布类判断）。
MIN_RELIABLE_SAMPLE = 5
# 趋势类只比较发布满 3 天的作品：新作品的数据尚未稳定，提前比较会误判"衰退"。
TREND_MATURE_DAYS = 3
# 零互动定义：点赞数低于该值。抖音公开数据里 0 赞作品常伴随极低推荐量。
ZERO_DIGG_THRESHOLD = 10


@dataclass(frozen=True)
class DiagnosisAccount:
    nickname: str
    avatar_url: str | None
    signature: str | None
    province: str | None
    ip_location: str | None
    unique_id: str | None
    follower_count: int
    total_favorited: int
    aweme_count: int


@dataclass(frozen=True)
class DiagnosisVideo:
    aweme_id: str
    title: str
    created_at: datetime | None
    digg_count: int
    comment_count: int
    share_count: int
    collect_count: int
    play_count: int | None = None

    @property
    def interaction(self) -> int:
        return self.digg_count + self.comment_count + self.share_count + self.collect_count


@dataclass(frozen=True)
class DiagnosisSnapshot:
    platform: str
    account: DiagnosisAccount
    videos: tuple[DiagnosisVideo, ...]
    collected_at: datetime


@dataclass(frozen=True)
class SubScore:
    key: str
    label: str
    score: float
    full: float
    counted: bool
    detail: str


@dataclass(frozen=True)
class DimensionScore:
    key: str
    label: str
    weight: float
    score: float
    effective_full: float
    full: float
    sub_scores: tuple[SubScore, ...]
    comment: str


@dataclass(frozen=True)
class RiskWarning:
    kind: str
    label: str
    level: RiskLevel
    evidence: str


@dataclass(frozen=True)
class DiagnosisReport:
    score: int
    tier: str
    tier_label: str
    rating: Rating
    dimensions: tuple[DimensionScore, ...]
    risks: tuple[RiskWarning, ...]
    metrics: dict[str, float | int | None]
    sample_size: int
    low_confidence: bool
    data_level: DataLevel


@dataclass(frozen=True)
class _Baseline:
    tier: str
    tier_label: str
    min_followers: int
    engagement_excellent: float
    engagement_good: float
    engagement_low: float
    viral_line: float
    avg_interaction_lines: tuple[int, int, int, int]
    works_abnormal_high: int
    works_high: int
    works_normal: int
    likes_per_work_high: int
    likes_per_work_good: int
    likes_per_work_normal: int


# 顺序必须从大到小：取第一个满足 ``followers >= min_followers`` 的档位。
_BASELINES: tuple[_Baseline, ...] = (
    _Baseline(
        tier="S",
        tier_label="超头部",
        min_followers=1_000_000,
        engagement_excellent=2.0,
        engagement_good=1.2,
        engagement_low=0.6,
        viral_line=1.0,
        avg_interaction_lines=(1_000_000, 100_000, 10_000, 500),
        works_abnormal_high=3_000,
        works_high=150,
        works_normal=60,
        likes_per_work_high=200_000,
        likes_per_work_good=80_000,
        likes_per_work_normal=15_000,
    ),
    _Baseline(
        tier="A",
        tier_label="头部",
        min_followers=100_000,
        engagement_excellent=4.0,
        engagement_good=2.5,
        engagement_low=1.2,
        viral_line=2.0,
        avg_interaction_lines=(200_000, 20_000, 2_000, 200),
        works_abnormal_high=6_000,
        works_high=600,
        works_normal=200,
        likes_per_work_high=15_000,
        likes_per_work_good=4_000,
        likes_per_work_normal=1_200,
    ),
    _Baseline(
        tier="B",
        tier_label="腰部",
        min_followers=10_000,
        engagement_excellent=6.0,
        engagement_good=4.0,
        engagement_low=2.0,
        viral_line=5.0,
        avg_interaction_lines=(50_000, 5_000, 500, 50),
        works_abnormal_high=5_000,
        works_high=300,
        works_normal=100,
        likes_per_work_high=3_000,
        likes_per_work_good=1_000,
        likes_per_work_normal=250,
    ),
    _Baseline(
        tier="C",
        tier_label="尾部",
        min_followers=0,
        engagement_excellent=8.0,
        engagement_good=5.0,
        engagement_low=2.5,
        viral_line=10.0,
        avg_interaction_lines=(10_000, 1_000, 200, 20),
        works_abnormal_high=3_000,
        works_high=100,
        works_normal=30,
        likes_per_work_high=1_000,
        likes_per_work_good=400,
        likes_per_work_normal=100,
    ),
)

_DIMENSION_WEIGHTS: dict[str, float] = {
    "profile": 0.10,
    "productivity": 0.15,
    "engagement": 0.30,
    "quality": 0.20,
    "trend": 0.15,
    "audience": 0.10,
}


def _baseline_for(followers: int) -> _Baseline:
    for baseline in _BASELINES:
        if followers >= baseline.min_followers:
            return baseline
    return _BASELINES[-1]


def _round_half_up(value: float) -> int:
    # 内置 round 是银行家舍入（round(0.5)=0），评分必须按四舍五入，否则边界分档不可复现。
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.fmean(values)


def _percent(value: float) -> str:
    return f"{value:.2f}%"


@dataclass
class _DimensionBuilder:
    key: str
    label: str
    weight: float
    subs: list[SubScore] = field(default_factory=list)

    def add(
        self,
        key: str,
        label: str,
        score: float,
        full: float,
        detail: str,
        *,
        counted: bool = True,
    ) -> None:
        self.subs.append(
            SubScore(
                key=key,
                label=label,
                score=score if counted else 0.0,
                full=full,
                counted=counted,
                detail=detail,
            )
        )

    def build(self) -> DimensionScore:
        counted = [sub for sub in self.subs if sub.counted]
        score = sum(sub.score for sub in counted)
        effective_full = sum(sub.full for sub in counted)
        full = sum(sub.full for sub in self.subs)
        short = [sub for sub in counted if sub.score < sub.full * 0.6]
        comment = "、".join(sub.label for sub in short[:2]) if short else "本维度无明显短板"
        return DimensionScore(
            key=self.key,
            label=self.label,
            weight=self.weight,
            score=score,
            effective_full=effective_full,
            full=full,
            sub_scores=tuple(self.subs),
            comment=comment,
        )


def _score_profile(
    account: DiagnosisAccount,
) -> DimensionScore:
    builder = _DimensionBuilder("profile", "账号基础画像", _DIMENSION_WEIGHTS["profile"])
    builder.add(
        "avatar",
        "头像",
        2 if (account.avatar_url or "").strip() else 0,
        2,
        "已设置头像" if (account.avatar_url or "").strip() else "未设置头像，主页辨识度低",
    )
    signature = (account.signature or "").strip()
    signature_score = 3 if len(signature) >= 15 else 1 if signature else 0
    builder.add(
        "signature",
        "简介",
        signature_score,
        3,
        f"简介 {len(signature)} 字"
        + ("，信息完整" if signature_score == 3 else "，建议补足定位与价值主张"),
    )
    province = (account.province or "").strip()
    ip_location = (account.ip_location or "").strip()
    region_score = 2 if province and ip_location else 1 if province or ip_location else 0
    builder.add(
        "region",
        "地域完整度",
        region_score,
        2,
        "地域信息完整" if region_score == 2 else "地域信息不完整",
    )
    unique_id = (account.unique_id or "").strip()
    builder.add(
        "unique_id",
        "抖音号",
        1 if unique_id else 0,
        1,
        "已设置抖音号" if unique_id else "未设置抖音号，不便被搜索",
    )
    return builder.build()


def _published_at(videos: tuple[DiagnosisVideo, ...]) -> list[datetime]:
    """提取发布时间；缺失的条目直接跳过，调用方按剩余样本判断是否够用."""

    values: list[datetime] = []
    for video in videos:
        created_at = video.created_at
        if created_at is not None:
            values.append(created_at)
    return values


def _publish_frequency(videos: tuple[DiagnosisVideo, ...]) -> float | None:
    timestamps = _published_at(videos)
    if len(timestamps) < MIN_RELIABLE_SAMPLE:
        return None
    span_days = (max(timestamps) - min(timestamps)).total_seconds() / 86_400
    if span_days <= 0:
        return None
    return len(timestamps) / span_days


def _score_productivity(
    account: DiagnosisAccount,
    videos: tuple[DiagnosisVideo, ...],
    baseline: _Baseline,
) -> DimensionScore:
    builder = _DimensionBuilder("productivity", "内容生产力", _DIMENSION_WEIGHTS["productivity"])
    no_works = not videos and account.aweme_count <= 0
    works = max(account.aweme_count, len(videos))
    if no_works:
        works_score, works_detail = 0, "账号暂无作品"
    elif works >= baseline.works_abnormal_high:
        works_score, works_detail = 4, f"{works} 条，产量异常高，需关注质量稀释"
    elif works >= baseline.works_high:
        works_score, works_detail = 5, f"{works} 条，产量健康"
    elif works >= baseline.works_normal:
        works_score, works_detail = 4, f"{works} 条，产量正常"
    else:
        works_score, works_detail = (
            2,
            f"{works} 条，低于{baseline.tier_label}正常线 {baseline.works_normal} 条",
        )
    builder.add("works", "作品总量", works_score, 5, works_detail)

    per_work = _ratio(account.total_favorited, works)
    if no_works:
        builder.add("likes_per_work", "人均获赞", 0, 5, "账号暂无作品")
    elif per_work is None:
        builder.add("likes_per_work", "人均获赞", 0, 5, "作品数为 0，无法计算", counted=False)
    else:
        if per_work >= baseline.likes_per_work_high:
            score, detail = 5, f"{per_work:.0f}，高效线以上"
        elif per_work >= baseline.likes_per_work_good:
            score, detail = 4, f"{per_work:.0f}，良好"
        elif per_work >= baseline.likes_per_work_normal:
            score, detail = 3, f"{per_work:.0f}，正常"
        else:
            score, detail = 1, f"{per_work:.0f}，低于正常线 {baseline.likes_per_work_normal}"
        builder.add("likes_per_work", "人均获赞", score, 5, detail)

    frequency = _publish_frequency(videos)
    if no_works:
        builder.add("frequency", "发布频率", 0, 5, "账号暂无作品")
    elif frequency is None:
        builder.add(
            "frequency",
            "发布频率",
            0,
            5,
            f"有效作品不足 {MIN_RELIABLE_SAMPLE} 条或时间跨度不足，暂不判定",
            counted=False,
        )
    else:
        if frequency < 0.5:
            score, detail = 2, f"{frequency:.2f} 条/天，低于 0.5，接近断更"
        elif frequency <= 2:
            score, detail = 5, f"{frequency:.2f} 条/天，节奏健康"
        elif frequency <= 3:
            score, detail = 4, f"{frequency:.2f} 条/天，偏高"
        else:
            score, detail = 3, f"{frequency:.2f} 条/天，过高，可能影响单条质量"
        builder.add("frequency", "发布频率", score, 5, detail)
    return builder.build()


def _score_engagement(
    account: DiagnosisAccount,
    videos: tuple[DiagnosisVideo, ...],
    baseline: _Baseline,
) -> DimensionScore:
    builder = _DimensionBuilder("engagement", "互动健康度", _DIMENSION_WEIGHTS["engagement"])
    followers = account.follower_count

    likes_per_follower = _ratio(account.total_favorited, followers)
    if likes_per_follower is None:
        builder.add(
            "likes_per_follower", "粉丝获赞比", 0, 10, "粉丝数为 0，无法计算", counted=False
        )
    else:
        if likes_per_follower >= 25:
            score = 10
        elif likes_per_follower >= 15:
            score = 9
        elif likes_per_follower >= 8:
            score = 8
        elif likes_per_follower >= 3:
            score = 6
        elif likes_per_follower >= 1:
            score = 3
        else:
            score = 1
        builder.add(
            "likes_per_follower",
            "粉丝获赞比",
            score,
            10,
            f"{likes_per_follower:.2f}（获赞总数/粉丝数）",
        )

    avg_interaction = _mean([float(video.interaction) for video in videos])
    engagement_rate = (
        avg_interaction / followers * 100 if avg_interaction is not None and followers > 0 else None
    )
    if not videos:
        builder.add("fan_engagement", "粉丝互动率", 0, 10, "无作品数据")
    elif engagement_rate is None:
        builder.add("fan_engagement", "粉丝互动率", 0, 10, "粉丝数为 0，无法计算", counted=False)
    else:
        if engagement_rate >= baseline.engagement_excellent:
            score = 10
        elif engagement_rate >= baseline.engagement_good:
            score = 7
        elif engagement_rate >= baseline.engagement_low:
            score = 4
        else:
            score = 1
        builder.add(
            "fan_engagement",
            "粉丝互动率",
            score,
            10,
            f"{_percent(engagement_rate)}（{baseline.tier_label}基准 "
            f"{baseline.engagement_excellent:.1f}/{baseline.engagement_good:.1f}/"
            f"{baseline.engagement_low:.1f}%）",
        )

    total_digg = sum(video.digg_count for video in videos)
    if not videos:
        builder.add("structure", "互动结构比", 0, 5, "无作品数据")
    elif total_digg <= 0:
        builder.add("structure", "互动结构比", 0, 5, "点赞总数为 0，无法计算", counted=False)
    else:
        comment_share = sum(video.comment_count for video in videos) / total_digg * 100
        share_share = sum(video.share_count for video in videos) / total_digg * 100
        if comment_share >= 4 and share_share >= 4:
            score = 5
        elif comment_share >= 3 and share_share >= 3:
            score = 4
        elif comment_share >= 2 or share_share >= 2:
            score = 3
        else:
            score = 2
        builder.add(
            "structure",
            "互动结构比",
            score,
            5,
            f"评论占赞 {comment_share:.2f}%、分享占赞 {share_share:.2f}%",
        )

    if not videos:
        builder.add("avg_interaction", "作品均互动", 0, 5, "无作品数据")
    elif avg_interaction is None:
        builder.add("avg_interaction", "作品均互动", 0, 5, "无法计算", counted=False)
    else:
        lines = baseline.avg_interaction_lines
        if avg_interaction >= lines[0]:
            score, level = 5, "现象级"
        elif avg_interaction >= lines[1]:
            score, level = 4, "强传播"
        elif avg_interaction >= lines[2]:
            score, level = 3, "中等"
        elif avg_interaction >= lines[3]:
            score, level = 2, "偏弱"
        else:
            score, level = 1, "微弱"
        builder.add(
            "avg_interaction",
            "作品均互动",
            score,
            5,
            f"{avg_interaction:.0f}，{level}（{baseline.tier_label}强传播线 {lines[1]}）",
        )
    return builder.build()


def _score_quality(
    account: DiagnosisAccount,
    videos: tuple[DiagnosisVideo, ...],
    baseline: _Baseline,
) -> DimensionScore:
    builder = _DimensionBuilder("quality", "内容质量", _DIMENSION_WEIGHTS["quality"])
    diggs = [float(video.digg_count) for video in videos]
    followers = account.follower_count

    if not videos or followers <= 0:
        builder.add("viral_rate", "爆款率", 0, 6, "无作品或粉丝数据")
    else:
        threshold = followers * baseline.viral_line / 100
        viral_count = sum(1 for value in diggs if value > threshold)
        viral_rate = viral_count / len(videos) * 100
        if viral_rate > 10:
            score = 6
        elif viral_rate >= 5:
            score = 4
        elif viral_rate > 0:
            score = 3
        else:
            score = 1
        builder.add(
            "viral_rate",
            "爆款率",
            score,
            6,
            f"{viral_rate:.2f}%（爆款线：单条点赞>{threshold:.0f}，共 {viral_count} 条）",
        )

    mean_digg = _mean(diggs)
    if not videos:
        builder.add("median_deviation", "中位/均值偏离", 0, 4, "账号暂无作品")
    elif len(diggs) < 3:
        builder.add(
            "median_deviation",
            "中位/均值偏离",
            0,
            4,
            "作品不足 3 条，分布指标退化，不计入",
            counted=False,
        )
    elif mean_digg is None or mean_digg <= 0:
        builder.add(
            "median_deviation", "中位/均值偏离", 0, 4, "点赞总数为 0，无法计算", counted=False
        )
    else:
        deviation = abs(statistics.median(diggs) - mean_digg) / mean_digg
        if deviation < 0.2:
            score = 4
        elif deviation < 0.5:
            score = 3
        else:
            score = 1
        builder.add(
            "median_deviation",
            "中位/均值偏离",
            score,
            4,
            f"偏离度 {deviation:.2f}",
        )

    if not videos:
        builder.add("stability", "互动稳定性", 0, 5, "账号暂无作品")
    elif len(diggs) < MIN_RELIABLE_SAMPLE:
        builder.add(
            "stability",
            "互动稳定性",
            0,
            5,
            f"作品不足 {MIN_RELIABLE_SAMPLE} 条，不计入",
            counted=False,
        )
    elif mean_digg is None or mean_digg <= 0:
        builder.add("stability", "互动稳定性", 0, 5, "均值不为正，无法计算", counted=False)
    else:
        cv = statistics.pstdev(diggs) / mean_digg
        if cv < 0.5:
            score = 5
        elif cv <= 1.0:
            score = 3
        else:
            score = 1
        builder.add("stability", "互动稳定性", score, 5, f"变异系数 CV={cv:.2f}")

    if not videos:
        builder.add("zero_interaction", "零互动占比", 0, 5, "无作品数据")
    else:
        zero_count = sum(1 for value in diggs if value < ZERO_DIGG_THRESHOLD)
        zero_rate = zero_count / len(diggs) * 100
        if zero_rate < 5:
            score = 5
        elif zero_rate < 20:
            score = 3
        else:
            score = 1
        builder.add(
            "zero_interaction",
            "零互动占比",
            score,
            5,
            f"{zero_rate:.1f}%（点赞<{ZERO_DIGG_THRESHOLD} 视为零互动，共 {zero_count} 条）",
        )
    return builder.build()


@dataclass(frozen=True)
class _TrendMetrics:
    recent_mean: float | None
    early_mean: float | None
    change_pct: float | None
    mature_count: int
    days_since_latest: int | None


def _trend_metrics(videos: tuple[DiagnosisVideo, ...], collected_at: datetime) -> _TrendMetrics:
    dated: list[tuple[datetime, DiagnosisVideo]] = []
    for video in videos:
        created_at = video.created_at
        if created_at is not None:
            dated.append((created_at, video))

    days_since_latest: int | None = None
    if dated:
        latest = max(timestamp for timestamp, _ in dated)
        delta = collected_at - latest
        # 采集时间早于作品时间说明数据异常（时区或爬虫时间滞后），此时不做断更判断。
        days_since_latest = max(0, int(delta.total_seconds() // 86_400))

    mature = [
        (timestamp, video)
        for timestamp, video in dated
        if (collected_at - timestamp).days >= TREND_MATURE_DAYS
    ]
    mature.sort(key=lambda pair: pair[0])
    if len(mature) < 4:
        return _TrendMetrics(None, None, None, len(mature), days_since_latest)

    window = max(1, len(mature) // 3)
    early = mature[:window]
    recent = mature[-window:]
    early_mean = _mean([float(video.interaction) for _, video in early])
    recent_mean = _mean([float(video.interaction) for _, video in recent])
    if early_mean is None or recent_mean is None or early_mean <= 0:
        return _TrendMetrics(recent_mean, early_mean, None, len(mature), days_since_latest)
    return _TrendMetrics(
        recent_mean,
        early_mean,
        (recent_mean - early_mean) / early_mean * 100,
        len(mature),
        days_since_latest,
    )


def _score_trend(
    videos: tuple[DiagnosisVideo, ...],
    trend: _TrendMetrics,
) -> DimensionScore:
    builder = _DimensionBuilder("trend", "内容趋势", _DIMENSION_WEIGHTS["trend"])
    no_works = not videos

    if no_works:
        builder.add("recent_trend", "近期趋势", 0, 5, "账号暂无作品")
    elif trend.change_pct is None:
        builder.add(
            "recent_trend",
            "近期趋势",
            0,
            5,
            f"发布满 {TREND_MATURE_DAYS} 天的作品不足 4 条或早期互动为 0，不计入",
            counted=False,
        )
    else:
        change = trend.change_pct
        if change > 20:
            score, level = 5, "上升"
        elif change >= -20:
            score, level = 4, "平稳"
        elif change >= -50:
            score, level = 2, "走弱"
        else:
            score, level = 1, "明显下滑"
        builder.add(
            "recent_trend",
            "近期趋势",
            score,
            5,
            f"近期互动较早期 {change:+.1f}%，{level}",
        )

    interactions = [float(video.interaction) for video in videos]
    avg = _mean(interactions)
    if no_works:
        builder.add("concentration", "爆款集中度", 0, 5, "账号暂无作品")
    elif len(interactions) < MIN_RELIABLE_SAMPLE:
        builder.add(
            "concentration",
            "爆款集中度",
            0,
            5,
            f"作品不足 {MIN_RELIABLE_SAMPLE} 条，不计入",
            counted=False,
        )
    elif avg is None or avg <= 0:
        builder.add("concentration", "爆款集中度", 0, 5, "互动总数为 0，无法计算", counted=False)
    else:
        ratio = max(interactions) / avg
        if ratio < 3:
            score = 5
        elif ratio < 5:
            score = 4
        elif ratio < 10:
            score = 2
        else:
            score = 1
        builder.add(
            "concentration",
            "爆款集中度",
            score,
            5,
            f"最高互动是均值的 {ratio:.2f} 倍",
        )

    if no_works:
        builder.add("recency", "最新活跃度", 0, 5, "账号暂无作品")
    elif trend.days_since_latest is None:
        builder.add(
            "recency",
            "最新活跃度",
            0,
            5,
            "作品缺少发布时间，不计入",
            counted=False,
        )
    else:
        days = trend.days_since_latest
        if days <= 3:
            score = 5
        elif days <= 7:
            score = 4
        elif days <= 14:
            score = 2
        else:
            score = 1
        builder.add("recency", "最新活跃度", score, 5, f"最近一条作品距今 {days} 天")
    return builder.build()


def _score_audience(
    account: DiagnosisAccount,
    videos: tuple[DiagnosisVideo, ...],
    baseline: _Baseline,
) -> DimensionScore:
    builder = _DimensionBuilder("audience", "粉丝质量", _DIMENSION_WEIGHTS["audience"])
    followers = account.follower_count

    if followers < 10_000:
        size_score, size_detail = 1, "<1 万，尾部"
    elif followers < 100_000:
        size_score, size_detail = 2, "1 万–10 万，腰部"
    elif followers < 1_000_000:
        size_score, size_detail = 3, "10 万–100 万，头部"
    else:
        size_score, size_detail = 4, "≥100 万，超头部"
    builder.add("fan_size", "粉丝规模", size_score, 4, size_detail)

    avg_interaction = _mean([float(video.interaction) for video in videos])
    engagement_rate = (
        avg_interaction / followers * 100 if avg_interaction is not None and followers > 0 else None
    )
    if not videos:
        builder.add("fan_interaction", "粉丝互动比", 0, 3, "无作品数据")
    elif engagement_rate is None:
        builder.add("fan_interaction", "粉丝互动比", 0, 3, "粉丝数为 0，无法计算", counted=False)
    else:
        if engagement_rate >= baseline.engagement_good:
            score = 3
        elif engagement_rate >= baseline.engagement_low:
            score = 2
        else:
            score = 1
        builder.add(
            "fan_interaction",
            "粉丝互动比",
            score,
            3,
            f"{_percent(engagement_rate)}",
        )

    likes_per_follower = _ratio(account.total_favorited, followers)
    if likes_per_follower is None:
        builder.add("like_divergence", "获赞/粉丝背离", 0, 3, "粉丝数为 0，无法计算", counted=False)
    else:
        if likes_per_follower <= 50:
            score, level = 3, "正常"
        elif likes_per_follower <= 100:
            score, level = 2, "偏高"
        else:
            score, level = 1, "异常偏高，需排查刷量"
        builder.add(
            "like_divergence",
            "获赞/粉丝背离",
            score,
            3,
            f"获赞/粉丝={likes_per_follower:.1f}，{level}",
        )
    return builder.build()


def _build_risks(
    account: DiagnosisAccount,
    videos: tuple[DiagnosisVideo, ...],
    baseline: _Baseline,
    trend: _TrendMetrics,
    engagement_rate: float | None,
) -> tuple[RiskWarning, ...]:
    risks: list[RiskWarning] = []
    followers = account.follower_count

    if (
        engagement_rate is not None
        and followers > 50_000
        and engagement_rate < baseline.engagement_low
    ):
        risks.append(
            RiskWarning(
                kind="zombie_followers",
                label="僵尸粉",
                level="high",
                evidence=f"粉丝互动率 {_percent(engagement_rate)} 低于{baseline.tier_label}偏低线 "
                f"{baseline.engagement_low:.1f}%，且粉丝数 {followers} 超过 5 万",
            )
        )

    likes_per_follower = _ratio(account.total_favorited, followers)
    declining = trend.change_pct is not None and trend.change_pct < -50
    if likes_per_follower is not None and likes_per_follower > 100 and declining:
        risks.append(
            RiskWarning(
                kind="fake_engagement",
                label="刷量",
                level="high",
                evidence=f"获赞/粉丝={likes_per_follower:.1f} 超过 100，且近期互动较早期 "
                f"{trend.change_pct:+.1f}%，存在虚假互动嫌疑",
            )
        )

    if trend.change_pct is not None and trend.change_pct < -50:
        risks.append(
            RiskWarning(
                kind="decline",
                label="衰退",
                level="medium",
                evidence=f"近期作品均互动较早期 {trend.change_pct:+.1f}%，低于早期 50%",
            )
        )

    if videos:
        zero_rate = sum(1 for v in videos if v.digg_count < ZERO_DIGG_THRESHOLD) / len(videos) * 100
        if zero_rate > 30:
            risks.append(
                RiskWarning(
                    kind="throttled",
                    label="限流",
                    level="medium",
                    evidence=f"零互动作品占比 {zero_rate:.1f}%，超过 30%，可能被限流",
                )
            )

    if trend.days_since_latest is not None and trend.days_since_latest > 14:
        risks.append(
            RiskWarning(
                kind="inactive",
                label="断更",
                level="medium",
                evidence=f"最近一条作品距今 {trend.days_since_latest} 天，超过 14 天",
            )
        )

    interactions = [float(video.interaction) for video in videos]
    avg = _mean(interactions)
    if len(interactions) >= MIN_RELIABLE_SAMPLE and avg is not None and avg > 0:
        ratio = max(interactions) / avg
        if ratio >= 10:
            risks.append(
                RiskWarning(
                    kind="single_post_dependency",
                    label="单条依赖",
                    level="low",
                    evidence=f"最高互动是均值的 {ratio:.2f} 倍，流量过度依赖单条作品",
                )
            )
    return tuple(risks)


def diagnose(snapshot: DiagnosisSnapshot) -> DiagnosisReport:
    """对一份账号快照做六维诊断，返回可直接渲染与持久化的报告结构."""

    account = snapshot.account
    videos = snapshot.videos
    baseline = _baseline_for(account.follower_count)

    trend = _trend_metrics(videos, snapshot.collected_at)
    avg_interaction = _mean([float(video.interaction) for video in videos])
    engagement_rate = (
        avg_interaction / account.follower_count * 100
        if avg_interaction is not None and account.follower_count > 0
        else None
    )

    dimensions = (
        _score_profile(account),
        _score_productivity(account, videos, baseline),
        _score_engagement(account, videos, baseline),
        _score_quality(account, videos, baseline),
        _score_trend(videos, trend),
        _score_audience(account, videos, baseline),
    )

    # 维度层面同样做有效归一化：某维度所有子项都不可计算时整维剔除，
    # 否则会把"没数据"当成"0 分"而误伤。
    active = [dimension for dimension in dimensions if dimension.effective_full > 0]
    total_weight = sum(dimension.weight for dimension in active)
    weighted = sum(
        dimension.score / dimension.effective_full * 100 * dimension.weight for dimension in active
    )
    score = _round_half_up(weighted / total_weight) if total_weight > 0 else 0

    if score >= 85:
        rating: Rating = "优质"
    elif score >= 70:
        rating = "正常"
    elif score >= 50:
        rating = "待优化"
    else:
        rating = "风险"

    play_values = [video.play_count for video in videos if video.play_count is not None]
    interactions = [float(video.interaction) for video in videos]
    diggs = [float(video.digg_count) for video in videos]
    zero_rate = (
        sum(1 for video in videos if video.digg_count < ZERO_DIGG_THRESHOLD) / len(videos) * 100
        if videos
        else None
    )
    metrics: dict[str, float | int | None] = {
        "followers": account.follower_count,
        "total_favorited": account.total_favorited,
        "aweme_count": account.aweme_count,
        "sample_size": len(videos),
        "engagement_rate": None if engagement_rate is None else round(engagement_rate, 4),
        "likes_per_follower": (
            None
            if (value := _ratio(account.total_favorited, account.follower_count)) is None
            else round(value, 4)
        ),
        "avg_interaction": None if avg_interaction is None else round(avg_interaction, 2),
        "publish_frequency": (
            None if (frequency := _publish_frequency(videos)) is None else round(frequency, 4)
        ),
        "viral_rate": (
            None
            if not videos or account.follower_count <= 0
            else round(
                sum(
                    1
                    for value in diggs
                    if value > account.follower_count * baseline.viral_line / 100
                )
                / len(videos)
                * 100,
                4,
            )
        ),
        "zero_interaction_rate": None if zero_rate is None else round(zero_rate, 4),
        "digg_cv": (
            None
            if len(diggs) < MIN_RELIABLE_SAMPLE
            or (mean_digg := _mean(diggs)) is None
            or mean_digg <= 0
            else round(statistics.pstdev(diggs) / mean_digg, 4)
        ),
        "recent_trend_pct": None if trend.change_pct is None else round(trend.change_pct, 2),
        "days_since_latest": trend.days_since_latest,
        "avg_play_count": (
            None if not play_values else round(sum(play_values) / len(play_values), 2)
        ),
        "interaction_total": round(sum(interactions), 2) if interactions else 0,
    }

    return DiagnosisReport(
        score=score,
        tier=baseline.tier,
        tier_label=baseline.tier_label,
        rating=rating,
        dimensions=dimensions,
        risks=_build_risks(account, videos, baseline, trend, engagement_rate),
        metrics=metrics,
        sample_size=len(videos),
        low_confidence=len(videos) < MIN_RELIABLE_SAMPLE,
        data_level="L2" if play_values else "L1",
    )


def report_to_dict(report: DiagnosisReport) -> dict[str, object]:
    """序列化为可入库的普通结构（不含任何上游原始报文）."""

    return {
        "score": report.score,
        "tier": report.tier,
        "tier_label": report.tier_label,
        "rating": report.rating,
        "sample_size": report.sample_size,
        "low_confidence": report.low_confidence,
        "data_level": report.data_level,
        "metrics": report.metrics,
        "risks": [
            {"kind": risk.kind, "label": risk.label, "level": risk.level, "evidence": risk.evidence}
            for risk in report.risks
        ],
        "dimensions": [
            {
                "key": dimension.key,
                "label": dimension.label,
                "weight": dimension.weight,
                "score": dimension.score,
                "effective_full": dimension.effective_full,
                "full": dimension.full,
                "comment": dimension.comment,
                "sub_scores": [
                    {
                        "key": sub.key,
                        "label": sub.label,
                        "score": sub.score,
                        "full": sub.full,
                        "counted": sub.counted,
                        "detail": sub.detail,
                    }
                    for sub in dimension.sub_scores
                ],
            }
            for dimension in report.dimensions
        ],
    }
