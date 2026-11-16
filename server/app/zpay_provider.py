"""ZPay payment provider implementation.

Wraps the existing ZPay HTTP clients and configuration loading into the
PaymentProvider Protocol, enabling multi-provider support.

CW-066: ZPay-specific logic is encapsulated here; routes use the generic
PaymentProvider interface via the registry.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime
from typing import NoReturn

from app.db_portable import BusinessConnection
from app.payment_provider import (
    DeploymentConfig,
    MerchantConfig,
    NotificationVerification,
    OrderCloseUnsupported,
    OrderQueryError,
    OrderQueryResult,
    PaymentCodeError,
    PaymentCodeResult,
    PaymentFormResult,
    PaymentProvider,
    register_provider,
)
from app.settings import SettingsRepository
from app.zpay import (
    ZPayDeploymentConfig,
    ZPayMerchantConfig,
    ZPayOrderQueryClient,
    ZPayOrderQueryError,
    ZPayPaymentCodeClient,
    ZPayPaymentCodeError,
    build_zpay_payment_form,
    deployment_config_from_environment,
    merchant_config_from_settings,
    parse_zpay_money_to_fen,
    sign_zpay_params,
    zpay_signing_string,
)


class ZPayProvider(PaymentProvider):
    """ZPay payment gateway provider.

    Implements the PaymentProvider Protocol by delegating to the existing
    ZPay HTTP clients (ZPayOrderQueryClient, ZPayPaymentCodeClient) and
    configuration loaders.
    """

    @property
    def name(self) -> str:
        return "zpay"

    def load_merchant_config(self, conn: BusinessConnection) -> MerchantConfig:
        """Load ZPay merchant configuration from provider_settings."""
        raw = SettingsRepository(conn).load_zpay_config()
        # Validate by constructing the ZPay-specific config
        zpay_config = merchant_config_from_settings(raw)
        return MerchantConfig(
            provider="zpay",
            raw=raw,
            allowed_channels=zpay_config.allowed_channels,
        )

    def load_deployment_config(self) -> DeploymentConfig:
        """Load ZPay deployment configuration from PUBLIC_BASE_URL."""
        zpay_deployment = deployment_config_from_environment()
        return DeploymentConfig(
            notify_url=zpay_deployment.notify_url,
            return_url=zpay_deployment.return_url,
            gateway_url=zpay_deployment.gateway_url,
        )

    def create_payment_form(
        self,
        *,
        merchant_order_no: str,
        amount_fen: int,
        credits: int,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
    ) -> PaymentFormResult:
        """Create ZPay payment form fields."""
        zpay_merchant = self._to_zpay_merchant(merchant)
        zpay_deployment = self._to_zpay_deployment(deployment)
        form_fields = build_zpay_payment_form(
            merchant_order_no=merchant_order_no,
            amount_fen=amount_fen,
            credits=credits,
            merchant=zpay_merchant,
            deployment=zpay_deployment,
        )
        return PaymentFormResult(
            gateway_url=deployment.gateway_url,
            method="POST",
            form_fields=form_fields,
        )

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
        """Create a ZPay payment QR code.

        ``expires_at`` is accepted for the Protocol and ignored: the ZPay submit
        API has no order-expiry field, so its orders keep the gateway's lifetime.
        """
        zpay_merchant = self._to_zpay_merchant(merchant)
        zpay_deployment = self._to_zpay_deployment(deployment)
        client = ZPayPaymentCodeClient()
        try:
            result = client.create_payment_code(
                merchant=zpay_merchant,
                deployment=zpay_deployment,
                merchant_order_no=merchant_order_no,
                amount_fen=amount_fen,
                credits=credits,
                client_ip=client_ip,
            )
        except ZPayPaymentCodeError as exc:
            raise PaymentCodeError(str(exc), status_code=exc.status_code) from exc
        return PaymentCodeResult(
            qr_image_url=result.qr_image_url,
            payment_url=result.payment_url,
            provider_order_no=result.provider_order_no,
        )

    def query_order(
        self,
        *,
        merchant: MerchantConfig,
        deployment: DeploymentConfig,
        merchant_order_no: str,
    ) -> OrderQueryResult:
        """Query ZPay order status."""
        zpay_merchant = self._to_zpay_merchant(merchant)
        zpay_deployment = self._to_zpay_deployment(deployment)
        client = ZPayOrderQueryClient()
        try:
            result = client.query_order(
                merchant=zpay_merchant,
                deployment=zpay_deployment,
                merchant_order_no=merchant_order_no,
            )
        except ZPayOrderQueryError as exc:
            raise OrderQueryError(str(exc), status_code=exc.status_code) from exc
        return OrderQueryResult(
            paid=result.paid,
            merchant_order_no=result.merchant_order_no,
            provider_trade_no=result.provider_trade_no,
            amount_fen=result.amount_fen,
            channel=result.channel,
            response_digest=result.response_digest,
        )

    def close_order(
        self,
        *,
        merchant: MerchantConfig,
        merchant_order_no: str,
    ) -> NoReturn:
        """ZPay submit API has no order-close endpoint — unsupported (F2).

        这正是 zpay 敞口无上界的根源之一：本地关单后网关侧二维码的寿命不受
        我们控制，只能靠查单补账兜底（人工查单已放开 CLOSED + sweep 的
        CLOSED 扫描段）。调用方按 provider 分支不应走到这里；走到即报错暴露
        分支缺失，绝不静默吞掉或混同成功。
        """
        raise OrderCloseUnsupported(
            "ZPay has no gateway close API; rely on query-based reconciliation"
        )

    def verify_notification(
        self,
        params: Mapping[str, str],
        merchant: MerchantConfig,
    ) -> NotificationVerification:
        """Verify a ZPay payment notification.

        Checks:
        1. sign_type is MD5
        2. Signature matches (HMAC comparison)
        3. pid matches merchant
        4. trade_status is TRADE_SUCCESS
        5. Required fields present
        6. Amount parses correctly
        """
        zpay_merchant = self._to_zpay_merchant(merchant)

        # Check sign_type
        if params.get("sign_type", "").upper() != "MD5":
            return NotificationVerification(
                valid=False,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                source_digest=None,
                error_code="ZPAY_INVALID_SIGN_TYPE",
            )

        # Verify signature
        signature = params.get("sign", "")
        expected = sign_zpay_params(params, zpay_merchant.key)
        if not hmac.compare_digest(expected, signature):
            return NotificationVerification(
                valid=False,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                source_digest=None,
                error_code="ZPAY_SIGNATURE_MISMATCH",
            )

        # Check pid
        if params.get("pid") != zpay_merchant.pid:
            return NotificationVerification(
                valid=False,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                source_digest=None,
                error_code="ZPAY_PID_MISMATCH",
            )

        # Check trade_status
        if params.get("trade_status") != "TRADE_SUCCESS":
            return NotificationVerification(
                valid=False,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                source_digest=None,
                error_code="ZPAY_TRADE_NOT_SUCCESS",
            )

        # Extract required fields
        merchant_order_no = params.get("out_trade_no", "")
        provider_trade_no = params.get("trade_no", "")
        channel = params.get("type", "")
        if not merchant_order_no or not provider_trade_no or not channel:
            return NotificationVerification(
                valid=False,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                source_digest=None,
                error_code="ZPAY_MISSING_FIELDS",
            )

        # Parse amount
        try:
            amount_fen = parse_zpay_money_to_fen(params.get("money", ""))
        except ValueError:
            return NotificationVerification(
                valid=False,
                merchant_order_no=None,
                provider_trade_no=None,
                amount_fen=None,
                channel=None,
                source_digest=None,
                error_code="ZPAY_INVALID_AMOUNT",
            )

        # Compute source digest
        source_digest = hashlib.sha256(zpay_signing_string(params).encode("utf-8")).hexdigest()

        return NotificationVerification(
            valid=True,
            merchant_order_no=merchant_order_no,
            provider_trade_no=provider_trade_no,
            amount_fen=amount_fen,
            channel=channel,
            source_digest=source_digest,
        )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _to_zpay_merchant(self, merchant: MerchantConfig) -> ZPayMerchantConfig:
        """Convert generic MerchantConfig to ZPay-specific config."""
        return merchant_config_from_settings(merchant.raw)

    def _to_zpay_deployment(self, deployment: DeploymentConfig) -> ZPayDeploymentConfig:
        """Convert generic DeploymentConfig to ZPay-specific config."""
        # Re-derive from environment to get gateway_url and query_url
        zpay_deployment = deployment_config_from_environment()
        return ZPayDeploymentConfig(
            gateway_url=zpay_deployment.gateway_url,
            query_url=zpay_deployment.query_url,
            notify_url=deployment.notify_url,
            return_url=deployment.return_url,
        )


# Register ZPayProvider at import time
register_provider("zpay", ZPayProvider)
