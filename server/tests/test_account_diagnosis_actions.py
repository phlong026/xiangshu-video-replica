from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.account_diagnosis_actions import (
    ActionCard,
    CalendarWeek,
    _avg_pairwise_similarity,
    build_action_cards,
    build_calendar,
    pick_reference_works,
)
from app.account_diagnosis_scoring import DiagnosisAccount, DiagnosisSnapshot, DiagnosisVideo

BASE = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)


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


def _video(index: int, digg: int, *, days_ago: int, title: str | None = None) -> DiagnosisVideo:
    return DiagnosisVideo(
        aweme_id=f"v{index}",
        title=title or f"工地实拍 第{index}期",
        created_at=BASE - timedelta(days=days_ago),
        digg_count=digg,
        comment_count=20,
        share_count=10,
        collect_count=15,
    )


def _report_stub(tier: str = "B") -> dict[str, Any]:
    return {
        "tier": tier,
        "metrics": {
            "engagement_rate": 2.1,
            "viral_rate": 0.0,
            "publish_frequency": 0.36,
            "days_since_latest": 9,
        },
        "risks": [{"kind": "decline", "label": "衰退", "level": "medium", "evidence": "-34%"}],
        "dimensions": [
            {"key": "engagement", "score": 16, "effective_full": 30},
            {"key": "quality", "score": 10, "effective_full": 20},
            {"key": "trend", "score": 8, "effective_full": 15},
            {"key": "profile", "score": 6, "effective_full": 8},
        ],
    }


def _snapshot(**account_overrides: object) -> DiagnosisSnapshot:
    renovation = bool(account_overrides.pop("_renovation", True))
    videos = tuple(
        _video(
            i,
            3000 + i * 10,
            days_ago=6 - i + 8,
            title=(f"工地实拍 第{i}期" if renovation else f"日常分享 第{i}期"),
        )
        for i in range(8)
    )
    return DiagnosisSnapshot(
        platform="douyin",
        account=_account(**account_overrides),
        videos=videos,
        collected_at=BASE,
    )


def test_low_engagement_generates_p0_interact_card() -> None:
    cards = build_action_cards(_snapshot(), _report_stub())
    keys = [card.key for card in cards]
    assert "interact-hook" in keys
    card = next(c for c in cards if c.key == "interact-hook")
    assert card.priority == "P0"
    assert card.metric == "engagement_rate"
    assert card.target_value == 4.0
    # 待自查项必须明确标注"自查"，不得写成断言的事实
    assert all("自查" in item for item in card.self_checks)


def test_topic_card_and_cadence_card_generated() -> None:
    cards = build_action_cards(_snapshot(), _report_stub())
    keys = [card.key for card in cards]
    assert "topic-pool" in keys
    assert "cadence" in keys
    cadence = next(c for c in cards if c.key == "cadence")
    assert cadence.metric == "publish_frequency"
    assert cadence.target_value == 0.5


def test_profile_card_is_p2_and_skipped_when_full() -> None:
    cards = build_action_cards(_snapshot(), _report_stub())
    profile = next(c for c in cards if c.key == "profile")
    assert profile.priority == "P2"

    # 画像拿满时不生成（dimensions stub 改为满分）
    report = _report_stub()
    report["dimensions"] = [{"key": "profile", "score": 8, "effective_full": 8}] + report[
        "dimensions"
    ][:3]
    cards2 = build_action_cards(_snapshot(), report)
    assert all(c.key != "profile" for c in cards2)


def test_renovation_keyword_switches_templates() -> None:
    cards = build_action_cards(_snapshot(), _report_stub())
    hook = next(c for c in cards if c.key == "interact-hook")
    assert "厨房" in hook.steps[0]

    generic = build_action_cards(
        _snapshot(signature="分享日常", _renovation=False),
        _report_stub(),
    )
    hook2 = next(c for c in generic if c.key == "interact-hook")
    assert "厨房" not in hook2.steps[0]


def test_similarity_helper_detects_similar_titles() -> None:
    same = _avg_pairwise_similarity(["装修避坑指南", "装修避坑指南续"])
    different = _avg_pairwise_similarity(["装修避坑指南", "今天吃了火锅"])
    assert same > different


def test_reference_works_ranked_by_similarity_and_heat() -> None:
    snap = _snapshot()
    rows = [
        {"video_id": "1", "title": "工地实拍 第9期", "author": "a", "likes": 1000},
        {"video_id": "2", "title": "完全无关的内容", "author": "b", "likes": 900_000},
        {"video_id": "3", "title": "工地实拍 第10期", "author": "c", "likes": 500},
    ]
    refs = pick_reference_works(rows, snap, limit=2)
    # 相似度为主：两条同赛道（工地实拍）都应排在纯高热无关内容之前；
    # 具体先后由 相似度×8 + 热度×2 决定，不硬编码顺序。
    ids = [ref.video_id for ref in refs]
    assert "2" not in ids
    assert set(ids) == {"1", "3"}
    assert all(ref.url.startswith("https://www.douyin.com/video/") for ref in refs)
    assert all(isinstance(ref.score, float) for ref in refs)


def test_calendar_builds_two_weeks_starting_monday() -> None:
    cards = [
        ActionCard(
            key="interact-hook",
            priority="P0",
            dimension="互动健康度",
            title="互动钩子改造",
            problem="p",
            evidence=(),
            self_checks=(),
            steps=(),
            before_after=(),
            target="t",
            metric=None,
            target_value=None,
            due_week=1,
        ),
        ActionCard(
            key="profile",
            priority="P2",
            dimension="基础画像",
            title="补简介",
            problem="p",
            evidence=(),
            self_checks=(),
            steps=(),
            before_after=(),
            target="t",
            metric=None,
            target_value=None,
            due_week=2,
        ),
    ]
    weeks = build_calendar([c.to_dict() for c in cards], BASE)
    assert len(weeks) == 2
    week1_days = weeks[0].days
    assert len(week1_days) == 7
    # BASE 是周日 → 下周一 = 11-23
    assert week1_days[0]["date"] == "2026-11-23"
    # 第一条发布任务引用 P0 卡标题
    publish_tasks = [d["task"] for d in week1_days if "发布：" in d["task"]]
    assert any("互动钩子改造" in t for t in publish_tasks)
    assert isinstance(weeks[0], CalendarWeek)
