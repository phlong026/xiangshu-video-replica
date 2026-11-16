"""Payment provider abstraction layer.

Defines the PaymentProvider Protocol that all payment gateways must implement,
along with generic data types and a registry for provider lookup.

CW-066: This module extracts the payment gateway interface from ZPay-specific
code, enabling multi-provider support (WeChat Pay, etc.) without changing
route logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.db_portable import BusinessConnection

# ---------------------------------------------------------------------------
# Generic data types (provider-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MerchantConfig:
    """Generic merchant configuration loaded from provider_settings."""

    provider: str
    raw: dict[str, str]
    allowed_channels: tuple[str, ...] = ()

    @property
    def primary_channel(self) -> str:
        """First allowed channel (for form-based providers)."""
        return self.allowed_channels[0] if self.allowed_channels else ""


@dataclass(frozen=True)
class DeploymentConfig:
    """Generic deployment configuration (URLs derived from environment)."""

    notify_url: str
    return_url: str
    gateway_url: str = ""  # For form-based providers (e.g., ZPay)


@dataclass(frozen=True)
class PaymentFormResult:
    """Result of creating a payment form for gateway submission."""

    gateway_url: str
    method: str
    form_fields: dict[str, str]


@dataclass(frozen=True)
class PaymentCodeResult:
    """Result of creating a payment code/QR for display."""

    qr_image_url: str
    payment_url: str
    provider_order_no: str | None


@dataclass(frozen=True)
class OrderQueryResult:
    """Result of querying order status from the payment provider."""

    paid: bool
    merchant_order_no: str
    provider_trade_no: str | None
    amount_fen: int | None
    channel: str | None
    response_digest: str


@dataclass(frozen=True)
class OrderCloseResult:
    """Outcome of asking the gateway to retire an order's payment code.

    ``closed`` 只有在网关侧给出确定答复时才可为 True：关单成功、幂等重复关单、
    或订单在网关根本不存在（该商户名下无从支付）。“网关说已支付”是
    ``closed=False`` 的确定答复——钱可能到了，但入账只能走已验证的查单，
    错误文本永不作为结算证据（2026-11-16 评审 P1）。

    ``known=False`` 表示结果未知：请求可能已经到达网关（超时/网络错误/未建模
    业务码），此时既不能断定码已作废也不能断定仍可支付，唯一可信的后续动作
    是 query_order（或交给 sweep/人工查单兜底）。
    """

    closed: bool
    known: bool = True


@dataclass(frozen=True)
class NotificationVerification:
    """Result of verifying a payment notification/callback."""

    valid: bool
    merchant_order_no: str | None
    provider_trade_no: str | None
    amount_fen: int | None
    channel: str | None
    source_digest: str | None
    error_code: str | None = None


# ---------------------------------------------------------------------------
# PaymentProvider Protocol
# ---------------------------------------------------------------------------


class PaymentProvider(Protocol):
    """Protocol that all payment providers must implement.

    Each provider encapsulates:
    - Merchant configuration loading (from provider_settings)
    - Deployment configuration (URLs from environment)
    - Payment form/code creation
    - Order status querying
    - Notification/callback verification
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g., 'zpay', 'wechat_native')."""
        ...

    def load_merchant_config(self, conn: BusinessConnection) -> MerchantConfig:
        """Load merchant configuration from the database.

        Raises ValueError if configuration is incomplete or invalid.
        """
        ...

    def load_deployment_config(self) -> DeploymentConfig:
        """Load deployment configuration from environment variables.

        Raises ValueError if PUBLIC_BASE_URL is not properly configured.
        """
        ...

    def create_payment_form(
        self,
        *,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
    ) -> PaymentFormResult:
        """Create payment form for gateway submission.

        Returns PaymentFormResult with gateway URL, HTTP method, and form fields.
        """
        ...

    def create_payment_code(
        self,
        *,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        client_ip: str,
        expires_at: datetime | None = None,
    ) -> PaymentCodeResult:
        """Create a payment code/QR for display to the customer.

        ``expires_at`` is the instant the caller considers the order dead. A
        provider that can tell its gateway (WeChat Native's ``time_expire``)
        should pass it on so both sides retire the order together; a provider
        with no such field ignores it.

        Raises PaymentProviderError on failure.
        """
        ...

    def query_order(
        self,
        *,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
        merchant_order_no: str,
    ) -> OrderQueryResult:
        """Query order status from the payment provider.

        Raises PaymentProviderError on failure.
        """
        ...

    def close_order(
        self,
        *,
        merchant: MerchantConfig,
        merchant_order_no: str,
    ) -> OrderCloseResult:
        """Retire the gateway-side order so its payment code can no longer be paid.

        F2（2026-11-16 漏账缺口修复）：客户删除订单只改本地状态，网关侧二维码
        若仍可支付，「关单后支付且回调丢失」就成了漏账缺口——本地关单的下一个
        动作就是把网关侧也关掉。有网关关单 API 的渠道（微信 v3 close）实现它；
        没有的渠道（zpay submit API 无关单接口）抛 :class:`OrderCloseUnsupported`
        ——调用方按渠道分支，不会对 unsupported 渠道发起调用。

        Raises OrderCloseError when the outcome could not be determined (the
        request may still have reached the gateway — callers must treat the
        order's gateway state as unknown, never assume either way).
        """
        ...

    def verify_notification(
        self,
        params: Mapping[str, str],
        merchant: MerchantConfig,
    ) -> NotificationVerification:
        """Verify a payment notification/callback from the provider.

        Returns NotificationVerification with valid=True if the notification
        is authentic and contains valid payment data.
        """
        ...


# ---------------------------------------------------------------------------
# Provider errors
# ---------------------------------------------------------------------------


class PaymentProviderError(RuntimeError):
    """Base error for payment provider operations."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class PaymentCodeError(PaymentProviderError):
    """Error creating a payment code."""

    pass


class OrderQueryError(PaymentProviderError):
    """Error querying order status."""

    pass


class OrderCloseError(PaymentProviderError):
    """Gateway close outcome could not be determined.

    与查询失败的本质区别：关单请求可能**已经到达网关并生效**。调用方不得
    据此断定二维码仍可支付或已作废——订单的网关侧状态只能靠后续 query_order
    澄清（删除流程告警后交给 sweep/人工查单兜底）。
    """

    pass


class OrderCloseUnsupported(OrderCloseError):
    """The provider has no gateway-side close API (e.g. ZPay submit).

    语义是「渠道无此能力」而非「结果未知」：调用方按渠道分支不应走到这里，
    走到即说明分支缺失，报错暴露而不是静默吞掉。
    """

    pass


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: dict[str, type[PaymentProvider]] = {}


def register_provider(name: str, provider_class: type[PaymentProvider]) -> None:
    """Register a payment provider class by name.

    Called at module import time by provider implementations.
    """
    _PROVIDER_REGISTRY[name] = provider_class


def get_payment_provider(name: str) -> PaymentProvider:
    """Get a payment provider instance by name.

    Raises KeyError if the provider is not registered.
    """
    provider_class = _PROVIDER_REGISTRY.get(name)
    if provider_class is None:
        raise KeyError(f"Unknown payment provider: {name}")
    return provider_class()


def list_registered_providers() -> tuple[str, ...]:
    """List all registered provider names."""
    return tuple(sorted(_PROVIDER_REGISTRY.keys()))
