from __future__ import annotations

import hashlib
import re

from app.zpay import generate_merchant_order_no, sign_zpay_params, zpay_signing_string


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
