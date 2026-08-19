from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import connect_database, initialize_database
from app.internal_accounts import create_user, issue_token
from app.main import app
from app.payment_routes import get_zpay_order_query_client
from app.settings import SettingsRepository
from app.zpay import (
    ZPayDeploymentConfig,
    ZPayMerchantConfig,
    ZPayOrderQueryClient,
    ZPayOrderQueryError,
    ZPayOrderQueryResult,
    sign_zpay_params,
)

ORDER_NO = "20260819000000000000000000000001"
SECOND_ORDER_NO = "20260819000000000000000000000002"
OTHER_ORDER_NO = "20260819000000000000000000000003"
CONTROL_TOKEN = "control-proxy-test-token"


@dataclass(frozen=True)
class PaymentTestContext:
    client: TestClient
    db_path: Path
    user_headers: dict[str, str]
    other_headers: dict[str, str]
    control_headers: dict[str, str]


def insert_pending_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    user_id: str,
    order_no: str,
    amount_fen: int = 10000,
    credits: int = 10,
) -> None:
    conn.execute(
        """
        INSERT INTO recharge_orders (
            id, user_id, merchant_order_no, provider_trade_no, channel,
            base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
            min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits
        ) VALUES (?, ?, ?, NULL, 'alipay', 1000, 1000, 10000, 1000, ?, ?)
        """,
        (order_id, user_id, order_no, amount_fen, credits),
    )


@pytest.fixture()
def payment_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[PaymentTestContext]:
    db_path = tmp_path / "payments.db"
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "internal")
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://video.example")
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    monkeypatch.setenv(
        "CONTROL_PROXY_TOKEN_DIGEST",
        hashlib.sha256(CONTROL_TOKEN.encode()).hexdigest(),
    )
    monkeypatch.setenv("CONTROL_ADMIN_USER_ID", "admin_1")

    with initialize_database(db_path) as conn:
        admin = create_user(
            conn,
            username="internal_admin",
            display_name="Internal Admin",
            role="admin",
            user_id="admin_1",
        )
        create_user(
            conn,
            username="operator_1",
            display_name="Operator One",
            user_id="user_1",
        )
        create_user(
            conn,
            username="operator_2",
            display_name="Operator Two",
            user_id="user_2",
        )
        user_token = issue_token(conn, user_id="user_1", raw_token="user-token")
        other_token = issue_token(conn, user_id="user_2", raw_token="other-token")
        SettingsRepository(conn).save_zpay_config(
            {"pid": "merchant-123", "key": "merchant-secret", "enabled_channels": "alipay"},
            actor_user_id=str(admin["user_id"]),
        )
        insert_pending_order(conn, order_id="order_1", user_id="user_1", order_no=ORDER_NO)
        insert_pending_order(
            conn,
            order_id="order_2",
            user_id="user_1",
            order_no=SECOND_ORDER_NO,
        )
        insert_pending_order(
            conn,
            order_id="order_3",
            user_id="user_2",
            order_no=OTHER_ORDER_NO,
        )
        conn.commit()

    try:
        with TestClient(app) as client:
            yield PaymentTestContext(
                client=client,
                db_path=db_path,
                user_headers={"Authorization": f"Bearer {user_token['token']}"},
                other_headers={"Authorization": f"Bearer {other_token['token']}"},
                control_headers={"X-Control-Proxy-Token": CONTROL_TOKEN},
            )
    finally:
        app.dependency_overrides.clear()


def signed_notify_params(
    *,
    order_no: str = ORDER_NO,
    trade_no: str = "zpay-trade-1",
    money: str = "100.00",
    trade_status: str = "TRADE_SUCCESS",
    pid: str = "merchant-123",
) -> dict[str, str]:
    params = {
        "pid": pid,
        "name": "内部视频生成条数充值 10 条",
        "money": money,
        "out_trade_no": order_no,
        "trade_no": trade_no,
        "trade_status": trade_status,
        "type": "alipay",
    }
    params["sign"] = sign_zpay_params(params, "merchant-secret")
    params["sign_type"] = "MD5"
    return params


def wallet_snapshot(db_path: Path, user_id: str = "user_1") -> tuple[int, int, int]:
    with connect_database(db_path) as conn:
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        charge_count = conn.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE user_id = ? AND type = 'CHARGE'",
            (user_id,),
        ).fetchone()[0]
    return int(wallet["available_credits"]), int(wallet["reserved_credits"]), int(charge_count)


def test_valid_notify_credits_wallet_and_duplicate_notify_is_idempotent(
    payment_context: PaymentTestContext,
) -> None:
    params = signed_notify_params()

    first = payment_context.client.get("/api/payments/zpay/notify", params=params)
    second = payment_context.client.get("/api/payments/zpay/notify", params=params)

    assert first.status_code == 200
    assert first.text == "success"
    assert second.status_code == 200
    assert second.text == "success"
    assert wallet_snapshot(payment_context.db_path) == (10, 0, 1)
    with connect_database(payment_context.db_path) as conn:
        order = conn.execute(
            """
            SELECT status, provider_trade_no, paid_at, notify_digest
            FROM recharge_orders WHERE merchant_order_no = ?
            """,
            (ORDER_NO,),
        ).fetchone()
        transaction = conn.execute(
            """
            SELECT available_delta, reserved_delta, recharge_order_id, idempotency_key
            FROM wallet_transactions WHERE type = 'CHARGE'
            """
        ).fetchone()
    assert order["status"] == "PAID"
    assert order["provider_trade_no"] == "zpay-trade-1"
    assert order["paid_at"] is not None
    assert len(str(order["notify_digest"])) == 64
    assert dict(transaction) == {
        "available_delta": 10,
        "reserved_delta": 0,
        "recharge_order_id": "order_1",
        "idempotency_key": "zpay:charge:order_1",
    }


@pytest.mark.parametrize("invalid_field", ["sign", "pid", "money", "trade_status"])
def test_invalid_notify_never_credits_wallet(
    payment_context: PaymentTestContext,
    invalid_field: str,
) -> None:
    if invalid_field == "pid":
        params = signed_notify_params(pid="other-merchant")
    elif invalid_field == "money":
        params = signed_notify_params(money="99.99")
    elif invalid_field == "trade_status":
        params = signed_notify_params(trade_status="WAIT_BUYER_PAY")
    else:
        params = signed_notify_params()
        params["sign"] = "0" * 32

    response = payment_context.client.get("/api/payments/zpay/notify", params=params)

    assert response.status_code in {400, 409}
    assert response.text != "success"
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)


def test_notify_rejects_duplicate_query_parameters(
    payment_context: PaymentTestContext,
) -> None:
    params = list(signed_notify_params().items())
    params.append(("money", "100.00"))

    response = payment_context.client.get("/api/payments/zpay/notify", params=params)

    assert response.status_code == 400
    assert response.text != "success"
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)


def test_paid_order_rejects_a_different_provider_trade_number(
    payment_context: PaymentTestContext,
) -> None:
    first = payment_context.client.get(
        "/api/payments/zpay/notify",
        params=signed_notify_params(),
    )
    conflict = payment_context.client.get(
        "/api/payments/zpay/notify",
        params=signed_notify_params(trade_no="zpay-trade-conflict"),
    )

    assert first.text == "success"
    assert conflict.status_code == 409
    assert conflict.text != "success"
    assert wallet_snapshot(payment_context.db_path) == (10, 0, 1)


def test_provider_trade_number_cannot_credit_two_orders(
    payment_context: PaymentTestContext,
) -> None:
    first = payment_context.client.get(
        "/api/payments/zpay/notify",
        params=signed_notify_params(),
    )
    conflict = payment_context.client.get(
        "/api/payments/zpay/notify",
        params=signed_notify_params(order_no=SECOND_ORDER_NO),
    )

    assert first.text == "success"
    assert conflict.status_code == 409
    assert wallet_snapshot(payment_context.db_path) == (10, 0, 1)


def test_concurrent_duplicate_notifies_credit_once(payment_context: PaymentTestContext) -> None:
    params = signed_notify_params()

    def send_notify(_: int) -> tuple[int, str]:
        with TestClient(app) as client:
            response = client.get("/api/payments/zpay/notify", params=params)
            return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send_notify, range(8)))

    assert results == [(200, "success")] * 8
    assert wallet_snapshot(payment_context.db_path) == (10, 0, 1)


def test_notify_returns_non_success_before_zpay_timeout_when_database_is_locked(
    payment_context: PaymentTestContext,
) -> None:
    with connect_database(payment_context.db_path) as locker:
        locker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        response = payment_context.client.get(
            "/api/payments/zpay/notify",
            params=signed_notify_params(),
        )
        elapsed = time.monotonic() - started
        locker.rollback()

    assert response.status_code == 503
    assert response.text != "success"
    assert elapsed < 5
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)


def test_return_page_is_display_only_even_with_valid_payment_fields(
    payment_context: PaymentTestContext,
) -> None:
    response = payment_context.client.get(
        "/api/payments/zpay/return",
        params=signed_notify_params(),
    )

    assert response.status_code == 200
    assert "正在确认支付" in response.text
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)


def test_user_can_read_only_their_own_recharge_order(
    payment_context: PaymentTestContext,
) -> None:
    own = payment_context.client.get(
        f"/api/recharge-orders/{ORDER_NO}",
        headers=payment_context.user_headers,
    )
    other = payment_context.client.get(
        f"/api/recharge-orders/{OTHER_ORDER_NO}",
        headers=payment_context.user_headers,
    )

    assert own.status_code == 200
    assert own.json() == {
        "order_no": ORDER_NO,
        "status": "PENDING",
        "amount_fen": 10000,
        "credits": 10,
        "channel": "alipay",
        "created_at": own.json()["created_at"],
        "paid_at": None,
    }
    assert other.status_code == 404


class FakeZPayQueryClient:
    def __init__(self, result: ZPayOrderQueryResult | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    def query_order(
        self,
        *,
        merchant: ZPayMerchantConfig,
        deployment: ZPayDeploymentConfig,
        merchant_order_no: str,
    ) -> ZPayOrderQueryResult:
        del merchant, deployment
        self.calls.append(merchant_order_no)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_control_sync_keeps_unpaid_order_pending(payment_context: PaymentTestContext) -> None:
    fake = FakeZPayQueryClient(
        ZPayOrderQueryResult(
            paid=False,
            merchant_order_no=ORDER_NO,
            provider_trade_no=None,
            amount_fen=None,
            channel=None,
            response_digest="a" * 64,
        )
    )
    app.dependency_overrides[get_zpay_order_query_client] = lambda: fake

    response = payment_context.client.post(
        f"/api/control/recharge-orders/{ORDER_NO}/sync",
        headers=payment_context.control_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
    assert fake.calls == [ORDER_NO]
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)
    with connect_database(payment_context.db_path) as conn:
        trade_no = conn.execute(
            "SELECT provider_trade_no FROM recharge_orders WHERE merchant_order_no = ?",
            (ORDER_NO,),
        ).fetchone()[0]
    assert trade_no is None


def test_control_sync_paid_result_uses_the_same_credit_service(
    payment_context: PaymentTestContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeZPayQueryClient(
        ZPayOrderQueryResult(
            paid=True,
            merchant_order_no=ORDER_NO,
            provider_trade_no="zpay-query-trade-1",
            amount_fen=10000,
            channel="alipay",
            response_digest="b" * 64,
        )
    )
    app.dependency_overrides[get_zpay_order_query_client] = lambda: fake
    from app import payment_routes

    original = payment_routes.confirm_recharge_payment
    calls: list[str] = []

    def recording_credit(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(kwargs["merchant_order_no"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(payment_routes, "confirm_recharge_payment", recording_credit)

    response = payment_context.client.post(
        f"/api/control/recharge-orders/{ORDER_NO}/sync",
        headers=payment_context.control_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PAID"
    assert calls == [ORDER_NO]
    assert wallet_snapshot(payment_context.db_path) == (10, 0, 1)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Control-Proxy-Token": "wrong"},
        {"Authorization": "Bearer user-token"},
    ],
)
def test_control_sync_rejects_missing_forged_or_business_identity(
    payment_context: PaymentTestContext,
    headers: dict[str, str],
) -> None:
    fake = FakeZPayQueryClient(ZPayOrderQueryResult(False, ORDER_NO, None, None, None, "c" * 64))
    app.dependency_overrides[get_zpay_order_query_client] = lambda: fake

    response = payment_context.client.post(
        f"/api/control/recharge-orders/{ORDER_NO}/sync",
        headers=headers,
    )

    assert response.status_code == 401
    assert fake.calls == []
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)


def test_control_sync_surfaces_redacted_query_failure_without_crediting(
    payment_context: PaymentTestContext,
) -> None:
    fake = FakeZPayQueryClient(ZPayOrderQueryError("query failed", status_code=504))
    app.dependency_overrides[get_zpay_order_query_client] = lambda: fake

    response = payment_context.client.post(
        f"/api/control/recharge-orders/{ORDER_NO}/sync",
        headers=payment_context.control_headers,
    )

    assert response.status_code == 504
    assert "merchant-secret" not in response.text
    assert wallet_snapshot(payment_context.db_path) == (0, 0, 0)


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body[:limit]


def test_zpay_query_client_uses_timeout_and_returns_normalized_paid_result() -> None:
    captured: dict[str, object] = {}

    def opener(request: Any, *, timeout: float) -> FakeHTTPResponse:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeHTTPResponse(
            {
                "code": 1,
                "status": 1,
                "pid": "merchant-123",
                "out_trade_no": ORDER_NO,
                "trade_no": "zpay-query-trade-1",
                "money": "100.00",
                "type": "alipay",
            }
        )

    client = ZPayOrderQueryClient(opener=opener, timeout_seconds=3.0)
    result = client.query_order(
        merchant=ZPayMerchantConfig("merchant-123", "merchant-secret", "alipay"),
        deployment=ZPayDeploymentConfig(
            gateway_url="https://zpayz.cn/submit.php",
            query_url="https://zpayz.cn/api.php",
            notify_url="https://video.example/api/payments/zpay/notify",
            return_url="https://video.example/api/payments/zpay/return",
        ),
        merchant_order_no=ORDER_NO,
    )

    query = parse_qs(urlsplit(str(captured["url"])).query)
    assert captured["timeout"] == 3.0
    assert query == {
        "act": ["order"],
        "pid": ["merchant-123"],
        "key": ["merchant-secret"],
        "out_trade_no": [ORDER_NO],
    }
    assert result == ZPayOrderQueryResult(
        paid=True,
        merchant_order_no=ORDER_NO,
        provider_trade_no="zpay-query-trade-1",
        amount_fen=10000,
        channel="alipay",
        response_digest=result.response_digest,
    )
    assert len(result.response_digest) == 64


def test_zpay_query_client_keeps_unpaid_order_unsettled() -> None:
    def opener(request: Any, *, timeout: float) -> FakeHTTPResponse:
        del request, timeout
        return FakeHTTPResponse(
            {
                "code": 1,
                "status": 0,
                "pid": "merchant-123",
                "out_trade_no": ORDER_NO,
            }
        )

    result = ZPayOrderQueryClient(opener=opener).query_order(
        merchant=ZPayMerchantConfig("merchant-123", "merchant-secret", "alipay"),
        deployment=ZPayDeploymentConfig(
            gateway_url="https://zpayz.cn/submit.php",
            query_url="https://zpayz.cn/api.php",
            notify_url="https://video.example/api/payments/zpay/notify",
            return_url="https://video.example/api/payments/zpay/return",
        ),
        merchant_order_no=ORDER_NO,
    )

    assert result == ZPayOrderQueryResult(
        paid=False,
        merchant_order_no=ORDER_NO,
        provider_trade_no=None,
        amount_fen=None,
        channel=None,
        response_digest=result.response_digest,
    )


def test_zpay_query_client_never_logs_secret_or_request_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing_opener(request: Any, *, timeout: float) -> FakeHTTPResponse:
        del timeout
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    client = ZPayOrderQueryClient(opener=failing_opener)
    with caplog.at_level("WARNING"), pytest.raises(ZPayOrderQueryError):
        client.query_order(
            merchant=ZPayMerchantConfig("merchant-123", "merchant-secret", "alipay"),
            deployment=ZPayDeploymentConfig(
                gateway_url="https://zpayz.cn/submit.php",
                query_url="https://zpayz.cn/api.php",
                notify_url="https://video.example/api/payments/zpay/notify",
                return_url="https://video.example/api/payments/zpay/return",
            ),
            merchant_order_no=ORDER_NO,
        )

    assert "merchant-secret" not in caplog.text
    assert "api.php" not in caplog.text
