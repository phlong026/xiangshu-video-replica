from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
ZPAY_GATEWAY_URL_ENV = "ZPAY_GATEWAY_URL"
ALLOWED_ZPAY_GATEWAYS = frozenset(
    {
        "https://zpayz.cn/submit.php",
        "https://z-pay.cn/submit.php",
    }
)
ALLOWED_ZPAY_CHANNELS = frozenset({"alipay", "wxpay"})


@dataclass(frozen=True)
class ZPayMerchantConfig:
    pid: str
    key: str
    channel: str


@dataclass(frozen=True)
class ZPayDeploymentConfig:
    gateway_url: str
    notify_url: str
    return_url: str


def zpay_signing_string(params: Mapping[str, object]) -> str:
    pairs = (
        (name, str(value))
        for name, value in params.items()
        if name not in {"sign", "sign_type"} and value is not None and str(value) != ""
    )
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def sign_zpay_params(params: Mapping[str, object], key: str) -> str:
    payload = f"{zpay_signing_string(params)}{key}".encode()
    # ZPay requires MD5 for protocol compatibility; it is not used for password storage.
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def generate_merchant_order_no() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    random_suffix = f"{secrets.randbelow(10**12):012d}"
    return f"{timestamp}{random_suffix}"


def merchant_config_from_settings(settings: Mapping[str, str]) -> ZPayMerchantConfig:
    pid = settings.get("pid", "").strip()
    key = settings.get("key", "").strip()
    channels = parse_enabled_channels(settings.get("enabled_channels", ""))
    if not pid or not key or not channels:
        raise ValueError("ZPay merchant settings are incomplete")
    return ZPayMerchantConfig(pid=pid, key=key, channel=channels[0])


def parse_enabled_channels(value: str) -> tuple[str, ...]:
    channels = tuple(channel.strip().lower() for channel in value.split(",") if channel.strip())
    if not channels:
        return ()
    if len(set(channels)) != len(channels) or any(
        channel not in ALLOWED_ZPAY_CHANNELS for channel in channels
    ):
        raise ValueError("ZPay enabled_channels must contain unique alipay or wxpay values")
    return channels


def deployment_config_from_environment() -> ZPayDeploymentConfig:
    gateway_url = os.environ.get(ZPAY_GATEWAY_URL_ENV, "").strip()
    if gateway_url not in ALLOWED_ZPAY_GATEWAYS:
        raise ValueError("ZPay gateway URL is missing or not allowlisted")

    public_base_url = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip()
    parsed = urlsplit(public_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin without path, query, or fragment")

    origin = f"https://{parsed.netloc}"
    return ZPayDeploymentConfig(
        gateway_url=gateway_url,
        notify_url=f"{origin}/api/payments/zpay/notify",
        return_url=f"{origin}/api/payments/zpay/return",
    )


def format_yuan(amount_fen: int) -> str:
    yuan, fen = divmod(amount_fen, 100)
    return f"{yuan}.{fen:02d}"


def build_zpay_payment_form(
    *,
    merchant_order_no: str,
    amount_fen: int,
    credits: int,
    merchant: ZPayMerchantConfig,
    deployment: ZPayDeploymentConfig,
) -> dict[str, str]:
    fields = {
        "pid": merchant.pid,
        "type": merchant.channel,
        "out_trade_no": merchant_order_no,
        "notify_url": deployment.notify_url,
        "return_url": deployment.return_url,
        "name": f"内部视频生成条数充值 {credits} 条",
        "money": format_yuan(amount_fen),
    }
    fields["sign"] = sign_zpay_params(fields, merchant.key)
    fields["sign_type"] = "MD5"
    return fields
