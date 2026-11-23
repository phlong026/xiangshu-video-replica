"""账号诊断：首单免费 / 设备限流 / 付费套餐门禁 / 端到端编排（TEST-PG）."""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json
from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from test_customer_center import account, client, route_state  # noqa: F401
from test_customer_registration import (  # noqa: F401
    client as registration_client,
)
from test_customer_registration import (
    registration_dsn,
)

from app.account_diagnosis import (
    _load_prior_actions,
    _load_reference_pool,
    availability,
    create_diagnosis,
    get_report,
)
from app.account_diagnosis_entitlement import (
    claim_free_quota,
    current_device_id,
    free_quota_claimed,
    has_paid_package,
    release_free_quota,
)
from app.account_link import AccountTarget
from app.db_portable import BusinessConnection
from app.viral_tikhub import (
    DOUYIN_USER_POSTS_PATH,
    DOUYIN_USER_PROFILE_PATH,
    DOUYIN_XINGTU_KOL_ID_PATH,
    DouyinUserPost,
    DouyinUserPostPage,
    DouyinUserProfile,
    DouyinXingtuVideoMetrics,
    ViralSourceClient,
)

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _isolate_model_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # 仍执行 LLM 分支；漏桩必须立即失败，不能发出请求后靠降级文案蒙混通过。
    monkeypatch.setenv("ACCOUNT_DIAGNOSIS_LLM_DISABLED", "0")

    def unexpected_request(*_args: object, **_kwargs: object) -> None:
        pytest.fail("诊断 PG 测试禁止模型 HTTP 外呼，请显式 mock LLM 入口。")

    monkeypatch.setattr("app.account_diagnosis_llm._post_chat_completion", unexpected_request)


def _target(sec_uid: str) -> AccountTarget:
    return AccountTarget(platform="douyin", sec_uid=sec_uid, unique_id=None)


def _profile(sec_uid: str) -> DouyinUserProfile:
    return DouyinUserProfile(
        sec_user_id=sec_uid,
        unique_id="zsj001",
        nickname="众墅之家",
        avatar_url="https://cdn.example/avatar.jpg",
        signature="分享装修干货与户型改造经验，帮你避坑",
        province="浙江",
        city="杭州",
        ip_location="浙江",
        follower_count=50_000,
        following_count=20,
        total_favorited=400_000,
        aweme_count=120,
    )


def _posts() -> DouyinUserPostPage:
    now = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp())
    posts = [
        DouyinUserPost(
            aweme_id=f"aweme-{index}",
            title=f"作品 {index}",
            created_at=now - index * 86_400,
            digg_count=3000 + index,
            comment_count=150,
            share_count=120,
            collect_count=80,
            play_count=None,
            duration_ms=18_000,
        )
        for index in range(8)
    ]
    return DouyinUserPostPage(posts=posts, max_cursor=None, has_more=False)


class _FakeClient:
    def douyin_user_profile(self, *, sec_user_id: str) -> DouyinUserProfile:
        return _profile(sec_user_id)

    def douyin_user_profile_by_unique_id(self, *, unique_id: str) -> DouyinUserProfile:
        return _profile("resolved-by-unique-id")

    def douyin_user_posts(self, **_: object) -> DouyinUserPostPage:
        return _posts()


class _L2Client(_FakeClient):
    """在基础桩上补星图链路：8 条作品全部命中播放/完播/互动率。"""

    def douyin_xingtu_kol_id(self, *, sec_user_id: str) -> str | None:
        return "777123"

    def douyin_xingtu_author_videos(
        self, *, o_author_id: str, page: int = 1
    ) -> list[DouyinXingtuVideoMetrics]:
        return [
            DouyinXingtuVideoMetrics(
                item_id=f"aweme-{index}",
                watch_cnt=5_000 + index * 100,
                finish_rate=32.5,
                interact_rate=6.1,
            )
            for index in range(8)
        ]


def _seed_package_order(
    raw: psycopg.Connection,
    user_id: str,
    *,
    credits: int = 100,
    amount_fen: int = 10_000,
) -> str:
    package_id = f"diag-pkg-{uuid4().hex}"
    raw.execute(
        "INSERT INTO recharge_packages (id, name, amount_fen, credits, discount_rate, "
        "discount_interfaces, is_active, version) VALUES (%s, %s, %s, %s, NULL, %s, true, 0)",
        (package_id, "诊断测试套餐", amount_fen, credits, json.dumps([])),
    )
    snapshot = json.dumps(
        {
            "id": package_id,
            "name": "诊断测试套餐",
            "amount_fen": amount_fen,
            "credits": credits,
        }
    )
    raw.execute(
        "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, provider_trade_no, "
        "status, pricing_scope, base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, "
        "package_id, package_snapshot_json, paid_at) "
        "VALUES (%s, %s, %s, 'zpay', %s, 'PAID', 'CUSTOMER_STANDARD', "
        "10000, 10000, 10000, 1000, %s, %s, %s, %s, %s)",
        (
            f"diag-order-{uuid4().hex}",
            user_id,
            f"DIAG-{uuid4().hex}",
            f"DIAG-TRADE-{uuid4().hex}",
            amount_fen,
            credits,
            package_id,
            snapshot,
            datetime.now(UTC),
        ),
    )
    return package_id


def _seed_wallet(raw: psycopg.Connection, user_id: str, credits: int) -> None:
    raw.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits, updated_at) "
        "VALUES (%s, %s, 0, %s) ON CONFLICT (user_id) DO UPDATE SET "
        "available_credits=excluded.available_credits, reserved_credits=0",
        (user_id, credits, datetime.now(UTC).isoformat()),
    )


def _seed_tariff(raw: psycopg.Connection) -> None:
    """route_state 的 TRUNCATE users CASCADE 会连坐 billing_tariffs（updated_by_user_id
    引用 users），迁移种子被清掉——按迁移同款默认值重建。"""

    raw.execute(
        "INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_cost_fen, "
        "unit_rounding) VALUES ('account_diagnosis', true, 8, 5, 'exact') "
        "ON CONFLICT (service) DO NOTHING"
    )


def _seed_reference(
    raw: psycopg.Connection,
    video_id: str,
    *,
    likes: int = 100,
    platform: str = "douyin",
) -> None:
    raw.execute(
        "INSERT INTO viral_videos "
        "(platform, video_id, title, author, likes, collection_published, pushed_at) "
        "VALUES (%s, %s, '装修作品', '参考作者', %s, 1, now())",
        (platform, video_id, likes),
    )
    raw.execute(
        "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,keyword) "
        "VALUES('diagnosis-reference','admin_link_import',%s,%s,'') ON CONFLICT DO NOTHING",
        (platform, video_id),
    )
    raw.execute(
        "INSERT INTO viral_media_preparations "
        "(id, platform, video_id, media_kind, status, storage_uri) "
        "VALUES (%s, %s, %s, 'video', 'SUCCEEDED', 'local://diagnosis/reference.mp4')",
        (f"reference-media-{platform}-{video_id}", platform, video_id),
    )


@pytest.mark.parametrize(
    "state,visible",
    [
        ("published", True),
        ("hidden", False),
        ("unpublished", False),
        ("unpushed", False),
        ("unready", False),
        ("future", False),
        ("expired", False),
    ],
)
def test_reference_pool_obeys_public_visibility(
    route_state: str, state: str, visible: bool
) -> None:
    with psycopg.connect(route_state) as raw, raw.transaction(force_rollback=True):
        conn = BusinessConnection.postgres(raw)
        _seed_reference(raw, "reference")
        video_changes = {
            "unpublished": "collection_published=0",
            "unpushed": "pushed_at=NULL",
            "future": "homepage_starts_at=now()+interval '1 day'",
            "expired": "homepage_ends_at=now()-interval '1 day'",
        }
        if state in video_changes:
            raw.execute(
                f"UPDATE viral_videos SET {video_changes[state]} WHERE video_id='reference'"
            )
        if state == "hidden":
            raw.execute(
                "INSERT INTO viral_video_visibility(platform, video_id, status) "
                "VALUES ('douyin', 'reference', 'HIDDEN')"
            )
        if state == "unready":
            raw.execute(
                "UPDATE viral_media_preparations SET status='PENDING' WHERE video_id='reference'"
            )

        assert _load_reference_pool(conn) == (
            [{"video_id": "reference", "title": "装修作品", "author": "参考作者", "likes": 100}]
            if visible
            else []
        )


def test_reference_pool_filters_before_its_hotness_limit(route_state: str) -> None:
    with psycopg.connect(route_state) as raw, raw.transaction(force_rollback=True):
        conn = BusinessConnection.postgres(raw)
        for likes in range(1, 62):
            _seed_reference(raw, f"reference-{likes}", likes=likes)
        _seed_reference(raw, "hidden-hot", likes=1_000_000)
        raw.execute(
            "INSERT INTO viral_video_visibility(platform, video_id, status) "
            "VALUES ('douyin', 'hidden-hot', 'HIDDEN')"
        )
        _seed_reference(raw, "other-platform", likes=2_000_000, platform="wechat_channels")

        # 隐藏高热度项不能挤占 60 个公开候选，也不能扩大到其他平台。
        pool = _load_reference_pool(conn)
        assert [row["video_id"] for row in pool] == [
            f"reference-{likes}" for likes in range(61, 1, -1)
        ]
        assert [row["likes"] for row in pool] == list(range(61, 1, -1))


def test_report_references_exclude_hidden_videos(client, route_state, monkeypatch) -> None:
    _, user_id = account(client, "diag_references")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings", lambda conn: _FakeClient()
    )
    with psycopg.connect(route_state) as raw, raw.transaction(force_rollback=True):
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        _seed_reference(raw, "public-reference")
        _seed_reference(raw, "hidden-reference", likes=1_000_000)
        raw.execute(
            "INSERT INTO viral_video_visibility(platform, video_id, status) "
            "VALUES ('douyin', 'hidden-reference', 'HIDDEN')"
        )

        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-references"),
            input_url="https://www.douyin.com/user/sec-references",
        )
        assert result["status"] == "SUCCEEDED"
        assert [row["videoId"] for row in result["report"]["referenceWorks"]] == [
            "public-reference"
        ]


def _seed_prior_report(
    raw: psycopg.Connection,
    owner_id: str,
    *,
    report_id: str = "prior-report",
    sec_user_id: str = "resolved-by-unique-id",
    unique_id: str = "old_handle",
    platform: str = "douyin",
    status: str = "SUCCEEDED",
    age_days: int = 2,
) -> None:
    raw.execute(
        "INSERT INTO account_diagnosis_reports "
        "(id, user_id, wallet_owner_id, platform, sec_user_id, unique_id, input_url, "
        "status, report_json, narrative_json, created_at, completed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'prior-input', %s, %s, %s, "
        "now()-%s::interval, now()-%s::interval)",
        (
            report_id,
            owner_id,
            owner_id,
            platform,
            sec_user_id,
            unique_id,
            status,
            json.dumps(
                {
                    "actionCards": [
                        {"title": "上次粉丝目标", "metric": "followers", "targetValue": 60000}
                    ],
                    "metrics": {"followers": 70000},
                }
            ),
            # 真实报告的机器目标保存在 report_json，解读文案没有 actionCards。
            json.dumps({"summary": "上次解读", "actions": [{"title": "文案中的简短建议"}]}),
            f"{age_days} days",
            f"{age_days} days",
        ),
    )


@pytest.mark.parametrize("lookup", ["link", "unique_id"])
def test_prior_review_reads_report_snapshot_for_the_resolved_account(
    client, route_state, monkeypatch, lookup: str
) -> None:
    _, user_id = account(client, "diag_prior_review")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings", lambda conn: _FakeClient()
    )
    target = (
        _target("resolved-by-unique-id")
        if lookup == "link"
        else AccountTarget(platform="douyin", sec_uid=None, unique_id="zsj001")
    )
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        _seed_prior_report(raw, user_id)
        result = create_diagnosis(conn, user_id=user_id, target=target, input_url=target.identity)
        assert result["status"] == "SUCCEEDED"
        assert result["cached"] is False

    expected = [
        {
            "title": "上次粉丝目标",
            "status": "worse",
            "note": "当前 50000 较上次 70000 进一步下滑（目标 60000）",
        }
    ]
    assert result["report"]["priorReview"] == expected
    # 从新连接读取落库报告，覆盖 _run → report_json → get_report 的完整链路。
    with psycopg.connect(route_state) as raw:
        stored = get_report(
            BusinessConnection.postgres(raw), user_id=user_id, report_id=result["id"]
        )
    assert stored["report"]["priorReview"] == expected


def test_prior_review_excludes_the_current_report(client, route_state) -> None:
    _, user_id = account(client, "diag_prior_exclude")
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_prior_report(raw, user_id)
        _seed_prior_report(raw, user_id, report_id="current-report", age_days=0)
        raw.execute(
            "UPDATE account_diagnosis_reports SET report_json=%s WHERE id='current-report'",
            (
                json.dumps(
                    {"actionCards": [{"title": "当前报告"}], "metrics": {"followers": 50000}}
                ),
            ),
        )
        prior = _load_prior_actions(
            conn,
            owner_id=user_id,
            sec_user_id="resolved-by-unique-id",
            exclude_report_id="current-report",
        )
        assert prior is not None
        assert prior["cards"][0]["title"] == "上次粉丝目标"
        assert prior["priorMetrics"] == {"followers": 70000}


@pytest.mark.parametrize(
    "history", ["other-account", "other-wallet", "other-platform", "failed", "expired"]
)
def test_prior_review_does_not_cross_history_boundaries(
    client, route_state, monkeypatch, history: str
) -> None:
    _, user_id = account(client, "diag_prior_boundary")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings", lambda conn: _FakeClient()
    )
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        owner_id = user_id
        if history == "other-wallet":
            owner_id = "other-prior-owner"
            raw.execute(
                "INSERT INTO users(id, username, display_name) VALUES (%s, %s, '其他客户')",
                (owner_id, owner_id),
            )
        _seed_prior_report(
            raw,
            owner_id,
            sec_user_id="another-account"
            if history == "other-account"
            else "resolved-by-unique-id",
            unique_id="zsj001",
            platform="wechat_channels" if history == "other-platform" else "douyin",
            status="FAILED" if history == "failed" else "SUCCEEDED",
            age_days=61 if history == "expired" else 2,
        )
        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=AccountTarget(platform="douyin", sec_uid=None, unique_id="zsj001"),
            input_url="zsj001",
        )
        assert result["status"] == "SUCCEEDED"
        assert result["report"]["priorReview"] is None


def test_free_quota_is_claimed_once_per_user_and_device(client, route_state) -> None:
    _, first_user = account(client, "diag_free_first")
    _, second_user = account(client, "diag_free_second")

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        device = current_device_id(conn, first_user)
        assert device

        assert claim_free_quota(conn, owner_id=first_user, device_id=device, report_id="r1") is True
        assert (
            claim_free_quota(conn, owner_id=first_user, device_id=device, report_id="r2") is False
        )
        # 同一设备换账号也拿不到第二次免费。
        assert (
            claim_free_quota(conn, owner_id=second_user, device_id=device, report_id="r3") is False
        )

        assert free_quota_claimed(conn, first_user) is True
        release_free_quota(conn, owner_id=first_user, report_id="r1")
        assert free_quota_claimed(conn, first_user) is False


def test_paid_package_requires_a_package_order_with_credits(client, route_state) -> None:
    _, member = account(client, "diag_member")
    _, plain = account(client, "diag_plain")

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        assert has_paid_package(conn, member) is False

        raw.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, provider, "
            "provider_trade_no, status, pricing_scope, base_unit_price_fen_snapshot, "
            "charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
            "recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            "VALUES (%s, %s, %s, 'zpay', %s, 'PAID', 'CUSTOMER_STANDARD', "
            "10000, 10000, 10000, 1000, 10000, 1, %s)",
            (
                f"diag-plain-{uuid4().hex}",
                plain,
                f"PLAIN-{uuid4().hex}",
                f"PLAIN-TRADE-{uuid4().hex}",
                datetime.now(UTC),
            ),
        )
        # 自定义金额订单没有 package_id，不算会员。
        assert has_paid_package(conn, plain) is False

        _seed_package_order(raw, member)
        assert has_paid_package(conn, member) is True


def test_diagnosis_gate_and_billing(client, route_state, monkeypatch) -> None:
    _, user_id = account(client, "diag_flow")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings",
        lambda conn: _FakeClient(),
    )

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)

        first = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-first"),
            input_url="https://www.douyin.com/user/sec-first",
        )
        assert first["status"] == "SUCCEEDED"
        assert first["isFree"] is True
        assert first["report"]["score"] >= 0
        assert first["narrative"]["generatedBy"] == "rules"

        state = availability(conn, user_id=user_id)
        assert state["freeQuota"] is False
        assert state["member"] is False

        # 第二次：免费额度已用且没有付费套餐 → 402 会员门禁。
        with pytest.raises(Exception) as excinfo:
            create_diagnosis(
                conn,
                user_id=user_id,
                target=_target("sec-second"),
                input_url="https://www.douyin.com/user/sec-second",
            )
        detail = getattr(excinfo.value, "detail", None)
        assert isinstance(detail, dict)
        assert detail["code"] == "ACCOUNT_DIAGNOSIS_MEMBER_REQUIRED"


def test_paid_member_is_charged_the_configured_price(client, route_state, monkeypatch) -> None:
    _, user_id = account(client, "diag_paid")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings",
        lambda conn: _FakeClient(),
    )

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        _seed_package_order(raw, user_id)
        _seed_wallet(raw, user_id, 100)

        # 先手动占掉首单免费，确保这次走计费路。
        device = current_device_id(conn, user_id)
        assert claim_free_quota(conn, owner_id=user_id, device_id=device, report_id="consumed")

        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-paid"),
            input_url="https://www.douyin.com/user/sec-paid",
        )
        assert result["status"] == "SUCCEEDED"
        assert result["isFree"] is False

        balance = raw.execute(
            "SELECT available_credits FROM wallets WHERE user_id=%s", (user_id,)
        ).fetchone()
        assert balance is not None and int(balance[0]) == 92

        operations = raw.execute(
            "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
            (user_id, "account_diagnosis"),
        ).fetchone()
        assert operations is not None and int(operations[0]) == 1


@pytest.mark.parametrize("paid", [False, True], ids=["free", "paid"])
@pytest.mark.parametrize("businesses", [[], ["viral_data"]], ids=["disabled", "viral-only"])
def test_sub_account_diagnosis_requires_its_own_permission(
    client, route_state, monkeypatch, paid: bool, businesses: list[str]
) -> None:
    _, owner_id = account(client, "diag_permission_owner")
    user_id = "diag-permission-sub"
    provider = Mock(return_value=_FakeClient())
    monkeypatch.setattr("app.account_diagnosis.account_diagnosis_client_from_settings", provider)

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        raw.execute(
            "INSERT INTO users (id, username, display_name, parent_user_id, account_type) "
            "VALUES (%s, %s, '诊断子账号', %s, 'SUB')",
            (user_id, user_id, owner_id),
        )
        raw.execute(
            "INSERT INTO sub_account_permissions (user_id, allowed_businesses) VALUES (%s, %s)",
            (user_id, json.dumps(businesses)),
        )
        _seed_tariff(raw)
        _seed_wallet(raw, owner_id, 100)
        if paid:
            _seed_package_order(raw, owner_id)
            assert claim_free_quota(
                conn, owner_id=owner_id, device_id=None, report_id="already-consumed"
            )

        def snapshot():
            return {
                table: [
                    tuple(row)
                    for row in raw.execute(
                        f"SELECT * FROM {table} WHERE user_id IN (%s, %s) ORDER BY 1",
                        (owner_id, user_id),
                    ).fetchall()
                ]
                for table in (
                    "account_diagnosis_free_grants",
                    "account_diagnosis_reports",
                    "wallets",
                    "wallet_transactions",
                    "billing_operations",
                )
            }

        before = snapshot()
        # 在同一事务内捕获拒绝，避免自动回滚掩盖「先占额度/建报告，再拒绝」的错误。
        with pytest.raises(HTTPException) as blocked:
            create_diagnosis(
                conn,
                user_id=user_id,
                target=_target("sec-permission"),
                input_url="https://www.douyin.com/user/sec-permission",
            )
        assert blocked.value.status_code == 403
        assert blocked.value.detail["code"] == "SUB_ACCOUNT_FEATURE_DISABLED"
        assert "账号诊断" in blocked.value.detail["message"]
        assert snapshot() == before
        provider.assert_not_called()

        # 单独开放诊断即可使用，不依赖爆款数据权限；费用归母账号钱包。
        raw.execute(
            "UPDATE sub_account_permissions SET allowed_businesses=%s WHERE user_id=%s",
            (json.dumps(["account_diagnosis"]), user_id),
        )
        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-permission"),
            input_url="https://www.douyin.com/user/sec-permission",
        )
        assert result["status"] == "SUCCEEDED"
        assert result["isFree"] is not paid
        assert free_quota_claimed(conn, owner_id) is True
        balance = raw.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s",
            (owner_id,),
        ).fetchone()
        assert balance is not None and tuple(balance) == (92 if paid else 100, 0)
        provider.assert_called_once()


@pytest.mark.parametrize("paid", [False, True], ids=["free", "paid"])
@pytest.mark.parametrize("valid_profile", [False, True], ids=["empty-profile", "zero-works"])
def test_profile_response_controls_transactional_diagnosis_billing(
    client, route_state, monkeypatch, paid: bool, valid_profile: bool
) -> None:
    from app.account_diagnosis_routes import router

    client.app.include_router(router)
    headers, user_id = account(client, "diag_profile_validation")
    profile_data = (
        {
            "nickname": "合法新账号",
            "follower_count": 0,
            "following_count": 0,
            "total_favorited": 0,
            "aweme_count": 0,
        }
        if valid_profile
        else {}
    )
    requested_paths: list[str] = []

    def request(method: str, url: str, *, headers, body=None) -> bytes:
        path = urlsplit(url).path
        requested_paths.append(path)
        data_by_path = {
            DOUYIN_USER_PROFILE_PATH: {"user": profile_data},
            DOUYIN_USER_POSTS_PATH: {"aweme_list": [], "has_more": 0},
            DOUYIN_XINGTU_KOL_ID_PATH: {"id": 0},
        }
        assert path in data_by_path, "诊断测试不允许未声明的数据源请求"
        return json.dumps({"code": 200, "data": data_by_path[path]}).encode("utf-8")

    transport = Mock()
    transport.request.side_effect = request
    source = ViralSourceClient(
        api_key="test-key",
        base_url="https://source.example",
        transport=transport,
        detail_transport=transport,
    )
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings", lambda conn: source
    )
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        _seed_wallet(raw, user_id, 100)
        if paid:
            _seed_package_order(raw, user_id)
            assert claim_free_quota(
                conn,
                owner_id=user_id,
                device_id=current_device_id(conn, user_id),
                report_id="already-consumed",
            )
        grants_before = [
            tuple(row)
            for row in raw.execute(
                "SELECT * FROM account_diagnosis_free_grants WHERE user_id=%s", (user_id,)
            ).fetchall()
        ]

    # 真实路由负责让异常越过事务边界，响应返回后才从新连接核对持久化结果。
    response = client.post(
        "/api/account-diagnosis",
        headers=headers,
        json={"input": "https://www.douyin.com/user/MS4wLjABAAAA_diagnosis_test_profile"},
    )
    if valid_profile:
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "SUCCEEDED"
        assert result["isFree"] is not paid
        assert result["report"]["account"]["nickname"] == "合法新账号"
        assert result["report"]["works"] == []
    else:
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "ACCOUNT_DIAGNOSIS_SOURCE_FAILED"
        assert requested_paths == [DOUYIN_USER_PROFILE_PATH]

    with psycopg.connect(route_state) as raw:
        balance = raw.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (user_id,)
        ).fetchone()
        assert balance == (92 if paid and valid_profile else 100, 0)
        reports = raw.execute(
            "SELECT status FROM account_diagnosis_reports WHERE user_id=%s", (user_id,)
        ).fetchall()
        assert reports == ([("SUCCEEDED",)] if valid_profile else [])
        grants_after = raw.execute(
            "SELECT * FROM account_diagnosis_free_grants WHERE user_id=%s", (user_id,)
        ).fetchall()
        if valid_profile:
            assert len(grants_after) == 1
        else:
            assert grants_after == grants_before
        # 数据源平台成本独立计量；这里只核对客户诊断扣费，不把平台请求当作扣分。
        operations = raw.execute(
            "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
            (user_id, "account_diagnosis"),
        ).fetchone()[0]
        assert operations == int(paid and valid_profile)
        ledger_delta = raw.execute(
            "SELECT COALESCE(SUM(available_delta), 0) FROM wallet_transactions WHERE user_id=%s",
            (user_id,),
        ).fetchone()[0]
        assert ledger_delta == (-8 if paid and valid_profile else 0)


def test_xingtu_hit_upgrades_report_to_l2(client, route_state, monkeypatch) -> None:
    """星图命中：播放量写回作品、data_level 升 L2、l2 汇总落报告。"""
    _, user_id = account(client, "diag_l2")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings",
        lambda conn: _L2Client(),
    )

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        _seed_package_order(raw, user_id)
        _seed_wallet(raw, user_id, 50)
        device = current_device_id(conn, user_id)
        assert claim_free_quota(conn, owner_id=user_id, device_id=device, report_id="consumed-l2")

        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-l2"),
            input_url="https://www.douyin.com/user/sec-l2",
        )

    assert result["status"] == "SUCCEEDED"
    assert result["dataLevel"] == "L2"
    report = result["report"]
    assert report is not None and report["l2"] is not None
    assert report["l2"]["source"] == "xingtu"
    assert report["l2"]["avgFinishRate"] == 32.5
    assert report["metrics"]["avg_play_count"] is not None
    works = {work["awemeId"]: work for work in report["works"]}
    assert works["aweme-0"]["playCount"] == 5000


def test_daily_limit_blocks_fourth_diagnosis_same_day(client, route_state, monkeypatch) -> None:
    _, user_id = account(client, "diag_daily")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings",
        lambda conn: _FakeClient(),
    )

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)
        _seed_package_order(raw, user_id)
        _seed_wallet(raw, user_id, 100)
        for _ in range(3):
            raw.execute(
                "INSERT INTO account_diagnosis_reports "
                "(id, user_id, wallet_owner_id, platform, sec_user_id, unique_id, input_url, "
                "status, is_free, created_at, completed_at) "
                "VALUES (%s, %s, %s, 'douyin', 'sec-daily', NULL, 'x', 'SUCCEEDED', 0, "
                "now(), now() - interval '48 hours')",
                (f"diag-seed-{uuid4().hex}", user_id, user_id),
            )
        # completed_at 回拨 48h 让 24h 缓存不命中，但 created_at 在今天——
        # 精确隔离「缓存命中」与「每日限额」两条规则。

        with pytest.raises(Exception) as excinfo:
            create_diagnosis(
                conn,
                user_id=user_id,
                target=_target("sec-daily"),
                input_url="https://www.douyin.com/user/sec-daily",
            )
        detail = getattr(excinfo.value, "detail", None)
        assert isinstance(detail, dict)
        assert detail["code"] == "ACCOUNT_DIAGNOSIS_DAILY_LIMIT"


def test_llm_narrative_replaces_rules_when_available(client, route_state, monkeypatch) -> None:
    _, user_id = account(client, "diag_llm")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings",
        lambda conn: _FakeClient(),
    )
    monkeypatch.setattr(
        "app.account_diagnosis.load_narrative_llm_config",
        lambda conn: ("https://api.deepseek.com", "key", "deepseek-chat"),
    )
    narrative = Mock(
        return_value={
            "summary": "LLM 解读：账号基本盘稳定。",
            "strengths": [],
            "weaknesses": [],
            "actions": [
                {
                    "title": "粉丝互动率",
                    "evidence": "低于腰部基准",
                    "suggestion": "结尾抛问题引导评论。",
                }
            ],
            "generatedBy": "llm",
        },
    )
    enrichment = Mock(return_value=None)
    monkeypatch.setattr("app.account_diagnosis.build_llm_narrative", narrative)
    monkeypatch.setattr("app.account_diagnosis.enrich_action_cards", enrichment)

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)

        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-llm"),
            input_url="https://www.douyin.com/user/sec-llm",
        )

    assert result["narrative"]["generatedBy"] == "llm"
    assert result["narrative"]["summary"].startswith("LLM 解读")
    narrative.assert_called_once()
    enrichment.assert_called_once()


def test_llm_failure_falls_back_to_rules(client, route_state, monkeypatch) -> None:
    _, user_id = account(client, "diag_llm_fallback")
    monkeypatch.setattr(
        "app.account_diagnosis.account_diagnosis_client_from_settings",
        lambda conn: _FakeClient(),
    )
    monkeypatch.setattr(
        "app.account_diagnosis.load_narrative_llm_config",
        lambda conn: ("https://api.deepseek.com", "key", "deepseek-chat"),
    )
    narrative = Mock(return_value=None)
    enrichment = Mock(return_value=None)
    monkeypatch.setattr("app.account_diagnosis.build_llm_narrative", narrative)
    monkeypatch.setattr("app.account_diagnosis.enrich_action_cards", enrichment)

    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        _seed_tariff(raw)

        result = create_diagnosis(
            conn,
            user_id=user_id,
            target=_target("sec-fallback"),
            input_url="https://www.douyin.com/user/sec-fallback",
        )

    assert result["narrative"]["generatedBy"] == "rules"
    narrative.assert_called_once()
    enrichment.assert_called_once()
