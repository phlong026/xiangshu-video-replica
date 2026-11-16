from __future__ import annotations

import hashlib
import re

import pytest

from app.payment_provider import MerchantConfig, OrderCloseUnsupported
from app.zpay import (
    generate_merchant_order_no,
    parse_zpay_money_to_fen,
    sign_zpay_params,
    zpay_signing_string,
)
from app.zpay_provider import ZPayProvider


def test_zpay_signature_matches_official_sorting_and_unencoded_values() -> None:
    params = {
        "type": "alipay",
        "sign_type": "MD5",
        "return_url": "https://video.example/api/payments/zpay/return",
        "name": "内部视频生成条数充值 10 条",
        "money": "100.00",
        "sign": "must-be-ignored",
        "notify_url": "https://video.example/api/payments/zpay/notify",
        "empty": "",
        "out_trade_no": "20260819123456000000000000000001",
        "pid": "merchant-123",
    }
    expected = (
        "money=100.00&name=内部视频生成条数充值 10 条&"
        "notify_url=https://video.example/api/payments/zpay/notify&"
        "out_trade_no=20260819123456000000000000000001&pid=merchant-123&"
        "return_url=https://video.example/api/payments/zpay/return&type=alipay"
    )

    signing_string = zpay_signing_string(params)
    signature = sign_zpay_params(params, "merchant-secret")

    assert signing_string == expected
    assert "%" not in signing_string
    assert (
        signature
        == hashlib.md5(f"{expected}merchant-secret".encode(), usedforsecurity=False).hexdigest()
    )
    assert re.fullmatch(r"[0-9a-f]{32}", signature)


def test_merchant_order_numbers_are_unique_numeric_and_within_zpay_limit() -> None:
    order_numbers = {generate_merchant_order_no() for _ in range(100)}

    assert len(order_numbers) == 100
    assert all(order_no.isdigit() for order_no in order_numbers)
    assert all(len(order_no) <= 32 for order_no in order_numbers)


@pytest.mark.parametrize(
    ("money", "amount_fen"),
    [("100", 10000), ("100.0", 10000), ("100.00", 10000), ("000100.01", 10001)],
)
def test_zpay_money_is_converted_to_integer_fen(money: str, amount_fen: int) -> None:
    assert parse_zpay_money_to_fen(money) == amount_fen


@pytest.mark.parametrize("money", ["", "0", "-1", ".50", "100.", "100.001", "1e2", "NaN"])
def test_zpay_money_rejects_non_positive_or_non_decimal_values(money: str) -> None:
    with pytest.raises(ValueError):
        parse_zpay_money_to_fen(money)


def test_zpay_provider_close_order_is_explicitly_unsupported() -> None:
    """zpay submit API 无网关关单端点：必须报错暴露，绝不混同成功（F2）.

    这正是 zpay 敞口无上界的根源——本地关单后网关二维码寿命不受控，只能靠
    查单补账兜底；调用方按 provider 分支不应走到这里，走到即说明分支缺失。
    """
    merchant = MerchantConfig(provider="zpay", raw={}, allowed_channels=("alipay",))
    with pytest.raises(OrderCloseUnsupported):
        ZPayProvider().close_order(merchant=merchant, merchant_order_no="ZP-CLOSE-1")
