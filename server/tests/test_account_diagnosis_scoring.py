from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.account_diagnosis_scoring import (
    DiagnosisAccount,
    DiagnosisReport,
    DiagnosisSnapshot,
    DiagnosisVideo,
    DimensionScore,
    _round_half_up,
    diagnose,
    report_to_dict,
)

BASE = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def _account(**overrides: object) -> DiagnosisAccount:
    values: dict[str, object] = {
        "nickname": "测试账号",
        "avatar_url": "https://cdn.example/avatar.jpg",
        "signature": "分享装修干货与户型改造经验，帮你避坑",
        "province": "浙江",
        "ip_location": "浙江",
        "unique_id": "zsj001",
        "follower_count": 50_000,
        "total_favorited": 500_000,
        "aweme_count": 100,
    }
    values.update(overrides)
    return DiagnosisAccount(**values)  # type: ignore[arg-type]


def _video(
    index: int,
    digg: int,
    *,
    days_ago: int,
    comments: int = 0,
    shares: int = 0,
    collects: int = 0,
    play_count: int | None = None,
) -> DiagnosisVideo:
    return DiagnosisVideo(
        aweme_id=f"video-{index}",
        title=f"作品 {index}",
        created_at=BASE - timedelta(days=days_ago),
        digg_count=digg,
        comment_count=comments,
        share_count=shares,
        collect_count=collects,
        play_count=play_count,
    )


def _snapshot(
    account: DiagnosisAccount | None = None,
    videos: tuple[DiagnosisVideo, ...] = (),
) -> DiagnosisSnapshot:
    return DiagnosisSnapshot(
        platform="douyin",
        account=account or _account(),
        videos=videos,
        collected_at=BASE,
    )


def _dimension(report: DiagnosisReport, key: str) -> DimensionScore:
    return next(dimension for dimension in report.dimensions if dimension.key == key)


def _sub(report: DiagnosisReport, dimension_key: str, sub_key: str):
    dimension = _dimension(report, dimension_key)
    return next(sub for sub in dimension.sub_scores if sub.key == sub_key)


def test_tier_boundaries_follow_follower_count() -> None:
    cases = [
        (1_000_000, "S"),
        (999_999, "A"),
        (100_000, "A"),
        (99_999, "B"),
        (10_000, "B"),
        (9_999, "C"),
        (0, "C"),
    ]
    for followers, expected in cases:
        report = diagnose(_snapshot(_account(follower_count=followers)))
        assert report.tier == expected, followers


def test_profile_dimension_full_and_empty() -> None:
    full = diagnose(_snapshot(_account()))
    profile = _dimension(full, "profile")
    assert profile.score == 8
    assert profile.effective_full == 8

    empty = diagnose(
        _snapshot(
            _account(
                avatar_url=None,
                signature="",
                province=None,
                ip_location=None,
                unique_id=None,
            )
        )
    )
    empty_profile = _dimension(empty, "profile")
    assert empty_profile.score == 0
    assert all(sub.score == 0 for sub in empty_profile.sub_scores)


def test_short_signature_gets_partial_score() -> None:
    report = diagnose(_snapshot(_account(signature="做装修")))
    assert _sub(report, "profile", "signature").score == 1


def test_no_works_keeps_dependent_items_in_denominator() -> None:
    report = diagnose(
        _snapshot(
            _account(aweme_count=0, total_favorited=0),
            videos=(),
        )
    )
    for dimension_key in ("productivity", "quality", "trend"):
        dimension = _dimension(report, dimension_key)
        assert dimension.effective_full == dimension.full
        assert all(sub.counted for sub in dimension.sub_scores)
        assert all(sub.score == 0 for sub in dimension.sub_scores)
    assert report.low_confidence is True
    assert report.sample_size == 0


def test_small_sample_excludes_distribution_items() -> None:
    videos = (
        _video(1, 900, days_ago=6),
        _video(2, 1200, days_ago=2),
    )
    report = diagnose(_snapshot(_account(aweme_count=2), videos))

    assert _sub(report, "productivity", "frequency").counted is False
    assert _sub(report, "quality", "median_deviation").counted is False
    assert _sub(report, "quality", "stability").counted is False
    assert _sub(report, "trend", "concentration").counted is False
    assert _sub(report, "trend", "recent_trend").counted is False
    # 爆款率和零互动占比不依赖分布，2 条样本也应计入。
    assert _sub(report, "quality", "viral_rate").counted is True
    assert _sub(report, "quality", "zero_interaction").counted is True


def test_zero_followers_excludes_ratio_items() -> None:
    videos = (_video(1, 100, days_ago=5),)
    report = diagnose(_snapshot(_account(follower_count=0), videos))

    assert _sub(report, "engagement", "likes_per_follower").counted is False
    assert _sub(report, "engagement", "fan_engagement").counted is False
    assert _sub(report, "audience", "fan_interaction").counted is False
    assert _sub(report, "audience", "like_divergence").counted is False
    assert report.metrics["engagement_rate"] is None


def test_engagement_structure_boundaries() -> None:
    cases = [
        # (评论, 分享, 期望分)
        (4, 4, 5),
        (3, 3, 4),
        (2, 0, 3),
        (1, 1, 2),
    ]
    for comments, shares, expected in cases:
        report = diagnose(
            _snapshot(
                videos=(
                    _video(
                        1,
                        100,
                        days_ago=5,
                        comments=comments,
                        shares=shares,
                    ),
                )
            )
        )
        assert _sub(report, "engagement", "structure").score == expected, (comments, shares)


def test_viral_rate_and_throttle_risk() -> None:
    videos = tuple(
        _video(index, 600 if index < 2 else 0, days_ago=10 + index) for index in range(10)
    )
    report = diagnose(_snapshot(_account(follower_count=10_000), videos))

    # 腰部爆款线 5% × 1 万粉 = 500；2/10 命中即 20% 爆款率。
    assert _sub(report, "quality", "viral_rate").score == 6
    # 8/10 零互动 = 80%，触发限流预警。
    assert _sub(report, "quality", "zero_interaction").score == 1
    assert any(risk.kind == "throttled" for risk in report.risks)


def test_decline_and_single_post_dependency() -> None:
    videos = tuple(
        _video(index, 1000 if index == 0 else 1, days_ago=30 - index) for index in range(11)
    )
    report = diagnose(_snapshot(videos=videos))

    kinds = {risk.kind for risk in report.risks}
    assert "decline" in kinds
    assert "single_post_dependency" in kinds
    assert _sub(report, "trend", "recent_trend").score == 1


def test_inactive_risk() -> None:
    videos = (_video(1, 500, days_ago=20),)
    report = diagnose(_snapshot(videos=videos))

    assert any(risk.kind == "inactive" for risk in report.risks)
    assert _sub(report, "trend", "recency").score == 1


def test_zombie_followers_and_fake_engagement() -> None:
    # 6 万粉、互动极低 + 获赞/粉丝 >100 且近期骤降。
    videos = (
        _video(1, 2000, days_ago=12),
        _video(2, 2000, days_ago=11),
        _video(3, 100, days_ago=10),
        _video(4, 100, days_ago=9),
        _video(5, 1, days_ago=8),
        _video(6, 1, days_ago=7),
    )
    report = diagnose(
        _snapshot(
            _account(follower_count=60_000, total_favorited=7_000_000),
            videos,
        )
    )
    levels = {risk.kind: risk.level for risk in report.risks}
    assert levels.get("zombie_followers") == "high"
    assert levels.get("fake_engagement") == "high"
    assert levels.get("decline") == "medium"


def test_healthy_account_scores_well_without_risks() -> None:
    videos = tuple(
        _video(
            index,
            3000 + index * 20,
            days_ago=10 - index,
            comments=150,
            shares=150,
            collects=100,
        )
        for index in range(10)
    )
    report = diagnose(_snapshot(videos=videos))

    assert report.score >= 70
    assert report.rating in ("优质", "正常")
    assert report.risks == ()
    assert report.low_confidence is False


def test_report_raises_data_level_when_play_count_present() -> None:
    videos = (_video(1, 100, days_ago=5, play_count=12_000),)
    report = diagnose(_snapshot(videos=videos))
    assert report.data_level == "L2"
    assert report.metrics["avg_play_count"] == 12_000

    without_play = diagnose(_snapshot(videos=(_video(1, 100, days_ago=5),)))
    assert without_play.data_level == "L1"


def test_round_half_up_is_not_bankers_rounding() -> None:
    assert _round_half_up(2.5) == 3
    assert _round_half_up(0.5) == 1
    assert _round_half_up(84.49) == 84


def test_diagnose_is_deterministic_and_serializable() -> None:
    snapshot = _snapshot(
        videos=tuple(_video(index, 100 + index, days_ago=6 - index) for index in range(6))
    )
    first = diagnose(snapshot)
    second = diagnose(snapshot)

    assert report_to_dict(first) == report_to_dict(second)
    assert json.loads(json.dumps(report_to_dict(first), ensure_ascii=False))["score"] == first.score
