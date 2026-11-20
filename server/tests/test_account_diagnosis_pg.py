"""账号诊断：首单免费 / 设备限流 / 付费套餐门禁 / 端到端编排（TEST-PG）."""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from test_customer_center import account, client, route_state  # noqa: F401
from test_customer_registration import (  # noqa: F401
    client as registration_client,
)
from test_customer_registration import (
    registration_dsn,
)

from app.account_diagnosis import availability, create_diagnosis
from app.account_diagnosis_entitlement import (
    claim_free_quota,
    current_device_id,
    free_quota_claimed,
    has_paid_package,
    release_free_quota,
)
from app.account_link import AccountTarget
from app.db_portable import BusinessConnection
from app.viral_tikhub import DouyinUserPost, DouyinUserPostPage, DouyinUserProfile

pytestmark = pytest.mark.pg


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
