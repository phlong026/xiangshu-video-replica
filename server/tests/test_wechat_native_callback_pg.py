"""CW-070: WeChat Pay V3 Native settlement — PostgreSQL lane (TEST-PG).

The settlement half of the WeChat Native callback that ``test_wechat_native_callback.py``
deliberately skips: that suite proves the raw-body signature verification and the
route's non-settlement answers on the SQLite lane, asserting ``confirm_recharge_payment``
is never reached. This module proves the part that *does* settle a real
``wechat_native`` recharge_order, which is PostgreSQL-only:

* migration 022 pins ``recharge_orders.provider`` to ``'zpay'`` with a CHECK that also
  fires on SQLite, so a ``wechat_native`` order cannot even be inserted there;
* migration 083 (PostgreSQL-only) adds ``prepay_id`` / ``code_url`` / ``transaction_id``
  and forces a ``wechat_native`` row to keep ``provider_trade_no`` NULL while its
  ``transaction_id`` becomes NOT NULL once PAID.

So the generalized settlement routine — ``confirm_recharge_payment`` under
``WECHAT_NATIVE_SETTLEMENT_SPEC`` (design decision Q1: one shared fund path parameterized
by ``trade_no_column``) — can only be exercised against real PostgreSQL, which is exactly
what design decision Q2 (PG lane + CI verification) mandates. Each test maps one wechat
settlement guarantee:

* S1 settle writes ``transaction_id`` (never ``provider_trade_no``), credits the wallet
  once, and lands one ``CHARGE`` ledger row keyed ``wechat_native:charge:<order>``;
* S2 replaying an already-PAID notification is idempotent (no double credit, one ledger row);
* S3 a ``transaction_id`` bound to another order is refused (``WECHAT_TRADE_ALREADY_BOUND``);
* S3b two concurrent settlements of two different orders carrying the *same*
  ``transaction_id`` credit exactly once — the pre-check is check-then-write and
  READ COMMITTED lets each transaction lock its own order row without seeing the
  other's uncommitted trade reference, so the partial unique index added by
  migration 20260924T0000 is the only backstop (the loser's UPDATE fails with
  23505 on that index and is mapped back to ``WECHAT_TRADE_ALREADY_BOUND``);
* S4 an amount that disagrees with the stored order is refused (``WECHAT_AMOUNT_MISMATCH``);
* S5 a channel outside the wxpay universe is refused (``WECHAT_CHANNEL_MISMATCH``);
* S6 a ``wechat_native`` callback cannot settle a ``zpay`` order (``WECHAT_PROVIDER_MISMATCH``).

Every error code carries the ``WECHAT`` prefix from the spec's ``error_prefix``, proving
the shared routine reports provider-specific codes without forking the fund logic.

Dedicated isolation database ``cw070_wechat_callback_test`` is registered in
``pg_test_kit.RECORDED_TEST_DATABASES``; cases are isolated by TRUNCATE + re-seed. Missing
PostgreSQL hard-fails (``require_pg_or_explicit_skip``) — a silent skip never counts as
acceptance evidence. The ZPay byte-identical regression stays in ``test_payments.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.payment_provider import OrderQueryError
from app.payment_routes import get_wechat_provider, get_zpay_provider, router
from app.recharge_order_sweep import sweep_pending_recharge_orders
from app.wechat_native_client import WeChatNativeError, WeChatOrderQueryResult
from app.zpay import ZPayOrderQueryResult
from app.zpay_payments import (
    WECHAT_NATIVE_SETTLEMENT_SPEC,
    NativeReconciliationOutcome,
    PaymentConfirmationError,
    close_expired_native_orders,
    close_expired_zpay_orders,
    confirm_recharge_payment,
    count_closed_native_long_tail,
    count_closed_zpay_long_tail,
    count_expired_native_orders,
    count_recently_closed_native_orders,
    count_recently_closed_zpay_orders,
    count_stale_pending_native_orders,
    reconcile_closed_native_long_tail,
    reconcile_closed_zpay_long_tail,
    reconcile_recently_closed_native_orders,
    reconcile_recently_closed_zpay_orders,
    reconcile_stale_pending_native_orders,
    reconcile_stale_pending_zpay_orders,
)

CW070_WECHAT_DB_NAME = "cw070_wechat_callback_test"

# Truncated (FK bypassed) then re-seeded per test so every case starts from an
# identical, known-valid billing scene.
_TRUNCATE_TABLES = "wallet_transactions, recharge_orders, wallets, users"

_SEEDED_WALLET_CREDITS = 100
_ORDER_CREDITS = 10
_ORDER_AMOUNT_FEN = 10000

TRANSACTION_ID = "4200001234202609120000000001"
OTHER_TRANSACTION_ID = "4200009999202609120000000009"
SOURCE_DIGEST = "cw070-wechat-source-digest"
OUT_TRADE_NO = "202609120000000000000000000000w1"

# A legal head-schema zpay order (test_cw059_billing_pg_matrix._ORDER_BASELINE parity):
# the neutral shape the wechat overrides below specialize.
_ORDER_BASELINE: dict[str, object] = {
    "provider": "zpay",
    "provider_trade_no": None,
    "channel": "alipay",
    "status": "PENDING",
    "pricing_scope": "INTERNAL",
    "base_unit_price_fen_snapshot": 1000,
    "charged_unit_price_fen_snapshot": 1000,
    "min_recharge_fen_snapshot": 10000,
    "recharge_step_fen_snapshot": 1000,
    "amount_fen": _ORDER_AMOUNT_FEN,
    "credits": _ORDER_CREDITS,
    "paid_at": None,
}

# 083: a wechat_native PENDING order carries prepay_id + code_url (NOT NULL), keeps
# provider_trade_no NULL, and leaves transaction_id NULL until settlement writes it.
_WECHAT_OVERRIDES: dict[str, object] = {
    "provider": "wechat_native",
    "channel": "wxpay",
    "pricing_scope": "CUSTOMER_STANDARD",
    "prepay_id": "wx-prepay-cw070-0001",
    "code_url": "weixin://wxpay/bizpayurl?pr=cw070abc",
}


# ---------------------------------------------------------------------------
# Fixtures — CW-007 kit helpers only (no hand-rolled DSN concatenation).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wechat_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the WeChat Native settlement matrix.

    ``require_pg_or_explicit_skip`` hard-gates (never a silent skip counting as
    evidence); ``create/drop_test_database`` are allowlist-guarded so cleanup can
    only ever touch ``cw070_wechat_callback_test``.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW070_WECHAT_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW070_WECHAT_DB_NAME)


@pytest.fixture()
def wechat_db(wechat_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Reset + seed the dedicated database and point the app PG pool at it.

    Seeding runs over an autocommit connection; the settlement itself runs through
    ``pg_transaction()`` (the app pool), so ``DATABASE_URL_ENV`` must target this
    database for one test — the same wiring ``test_wallet_billing_service.wallet_db``
    uses. ``close_pg_pool`` before and after keeps the pooled DSN from leaking across
    tests.
    """
    _reset_and_seed(wechat_dsn)
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, wechat_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    try:
        yield wechat_dsn
    finally:
        close_pg_pool()


def _reset_and_seed(dsn: str) -> None:
    """TRUNCATE and re-seed the single-user wallet scene on real PostgreSQL."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_TRUNCATE_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('user_1', 'user_1', 'User One', 'customer')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('user_1', %s, 0)",
            (_SEEDED_WALLET_CREDITS,),
        )


def _seed_order(
    dsn: str,
    *,
    order_id: str,
    merchant_order_no: str,
    wechat: bool = True,
    user_id: str = "user_1",
    **overrides: object,
) -> None:
    """Insert one recharge_order (wechat_native by default) over autocommit."""
    row: dict[str, object] = {
        "id": order_id,
        "user_id": user_id,
        "merchant_order_no": merchant_order_no,
        **_ORDER_BASELINE,
    }
    if wechat:
        row.update(_WECHAT_OVERRIDES)
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            f"INSERT INTO recharge_orders ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )


def _settle(
    *,
    merchant_order_no: str = OUT_TRADE_NO,
    provider_trade_no: str = TRANSACTION_ID,
    amount_fen: int = _ORDER_AMOUNT_FEN,
    channel: str = "wxpay",
    source_digest: str = SOURCE_DIGEST,
) -> sqlite3.Row:
    """Run confirm_recharge_payment(WECHAT_NATIVE_SETTLEMENT_SPEC) on the app pool."""
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return confirm_recharge_payment(
            conn,
            merchant_order_no=merchant_order_no,
            provider_trade_no=provider_trade_no,
            amount_fen=amount_fen,
            channel=channel,
            source_digest=source_digest,
            allowed_channels=("wxpay",),
            provider_spec=WECHAT_NATIVE_SETTLEMENT_SPEC,
        )


def _callback_client(wechat: bool) -> TestClient:
    """Real router and Database dependency; only the provider boundary is faked."""
    channel = "wxpay" if wechat else "alipay"
    result = SimpleNamespace(
        valid=True,
        authenticated=True,
        error_code=None,
        trade_state="SUCCESS",
        merchant_order_no=OUT_TRADE_NO,
        provider_trade_no=TRANSACTION_ID,
        amount_fen=_ORDER_AMOUNT_FEN,
        channel=channel,
        source_digest=SOURCE_DIGEST,
    )
    provider = SimpleNamespace(
        load_merchant_config=lambda conn: SimpleNamespace(allowed_channels={channel}),
        verify_notification=lambda params, merchant: result,
        verify_notification_raw=lambda **kwargs: result,
    )
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_wechat_provider if wechat else get_zpay_provider] = (
        lambda: provider
    )
    return TestClient(application)


def _notify(client: TestClient, wechat: bool) -> int:
    response = (
        client.post("/api/payments/wechat_native/notify", content=b"{}")
        if wechat
        else client.get("/api/payments/zpay/notify")
    )
    return response.status_code


@pytest.mark.parametrize("wechat", [False, True], ids=["zpay", "wechat"])
@pytest.mark.parametrize("failure", ["overflow", "missing_wallet", "ledger_conflict"])
def test_callback_failure_rolls_back_committed_state_and_can_retry(
    wechat_db: str, wechat: bool, failure: str
) -> None:
    """R-02: catching a domain error in a route must not commit half a payment."""
    _seed_order(wechat_db, order_id="order_r02", merchant_order_no=OUT_TRADE_NO, wechat=wechat)
    provider = "wechat_native" if wechat else "zpay"
    if failure == "ledger_conflict":
        _seed_order(wechat_db, order_id="other_order", merchant_order_no="r02_other")
    with psycopg.connect(wechat_db) as raw:
        if failure == "overflow":
            raw.execute("UPDATE wallets SET available_credits = 2147483647")
        elif failure == "missing_wallet":
            raw.execute("DELETE FROM wallets")
        else:
            raw.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, "
                "recharge_order_id, idempotency_key) "
                "VALUES ('conflict', 'user_1', 'CHARGE', 10, 0, 'other_order', %s)",
                (f"{provider}:charge:order_r02",),
            )
    with _callback_client(wechat) as client:
        for _ in range(2):
            assert _notify(client, wechat) == (500 if failure == "missing_wallet" else 409)
            # A fresh connection observes the committed state, after dependency teardown.
            with psycopg.connect(wechat_db) as raw:
                assert raw.execute(
                    "SELECT status, paid_at, notify_digest, transaction_id, provider_trade_no "
                    "FROM recharge_orders WHERE id = 'order_r02'"
                ).fetchone() == ("PENDING", None, None, None, None)
                assert raw.execute(
                    "SELECT count(*) FROM wallet_transactions WHERE recharge_order_id = 'order_r02'"
                ).fetchone() == (0,)
                expected_balance = (
                    None
                    if failure == "missing_wallet"
                    else (2147483647 if failure == "overflow" else _SEEDED_WALLET_CREDITS,)
                )
                assert raw.execute("SELECT available_credits FROM wallets").fetchone() == (
                    expected_balance
                )
        # Repair the fixture's cause; the same notification must now settle exactly once.
        with psycopg.connect(wechat_db) as raw:
            if failure == "missing_wallet":
                raw.execute(
                    "INSERT INTO wallets(user_id, available_credits, reserved_credits) "
                    "VALUES ('user_1', 100, 0)"
                )
            else:
                raw.execute("UPDATE wallets SET available_credits = 100")
                # Current main captures every CHARGE as a billing lot whose
                # primary key references the ledger row. Remove the derived
                # lot before repairing this deliberately injected conflict.
                raw.execute("DELETE FROM billing_credit_lots WHERE id = 'conflict'")
                raw.execute("DELETE FROM wallet_transactions WHERE id = 'conflict'")
        assert _notify(client, wechat) == 200
        assert _notify(client, wechat) == 200
    with psycopg.connect(wechat_db) as raw:
        assert raw.execute("SELECT available_credits FROM wallets").fetchone() == (110,)
        assert raw.execute(
            "SELECT count(*), sum(available_delta) FROM wallet_transactions "
            "WHERE recharge_order_id = 'order_r02'"
        ).fetchone() == (1, 10)


@pytest.mark.parametrize("wechat", [False, True], ids=["zpay", "wechat"])
def test_concurrent_callbacks_are_idempotent_after_order_lock(wechat_db: str, wechat: bool) -> None:
    _seed_order(wechat_db, order_id="order_r02", merchant_order_no=OUT_TRADE_NO, wechat=wechat)
    with _callback_client(wechat) as client, ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: _notify(client, wechat), range(4))) == [200] * 4
    with psycopg.connect(wechat_db) as raw:
        assert raw.execute("SELECT available_credits FROM wallets").fetchone() == (110,)
        assert raw.execute(
            "SELECT count(*), sum(available_delta) FROM wallet_transactions "
            "WHERE recharge_order_id = 'order_r02'"
        ).fetchone() == (1, 10)


def test_settlement_success_does_not_commit_its_outer_transaction(wechat_db: str) -> None:
    _seed_order(wechat_db, order_id="order_r02", merchant_order_no=OUT_TRADE_NO)
    with pytest.raises(RuntimeError, match="outer failure"), pg_transaction() as raw:
        confirm_recharge_payment(
            BusinessConnection.postgres(raw),
            merchant_order_no=OUT_TRADE_NO,
            provider_trade_no=TRANSACTION_ID,
            amount_fen=_ORDER_AMOUNT_FEN,
            channel="wxpay",
            source_digest=SOURCE_DIGEST,
            provider_spec=WECHAT_NATIVE_SETTLEMENT_SPEC,
        )
        raise RuntimeError("outer failure")
    with psycopg.connect(wechat_db) as raw:
        assert raw.execute("SELECT status FROM recharge_orders").fetchone() == ("PENDING",)
        assert raw.execute("SELECT available_credits FROM wallets").fetchone() == (100,)
        assert raw.execute("SELECT count(*) FROM wallet_transactions").fetchone() == (0,)


def _fetch_order(dsn: str, order_id: str) -> tuple[Any, ...]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, transaction_id, provider_trade_no, notify_digest, paid_at "
            "FROM recharge_orders WHERE id = %s",
            (order_id,),
        ).fetchone()
    assert row is not None
    return row


def _wallet_credits(dsn: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = 'user_1'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _charge_ledger(dsn: str, order_id: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT type, available_delta, idempotency_key FROM wallet_transactions "
            "WHERE recharge_order_id = %s ORDER BY ledger_sequence",
            (order_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# S1 — happy path: settle writes transaction_id, credits wallet once
# ---------------------------------------------------------------------------


def test_settle_wechat_order_writes_transaction_id_and_credits_wallet(
    wechat_db: str,
) -> None:
    _seed_order(wechat_db, order_id="order_1", merchant_order_no=OUT_TRADE_NO)

    confirmed = _settle()

    # The routine returns the re-read order: PAID with the trade reference aliased
    # from the transaction_id column (not provider_trade_no).
    assert confirmed["status"] == "PAID"
    assert confirmed["trade_ref"] == TRANSACTION_ID

    status, transaction_id, provider_trade_no, notify_digest, paid_at = _fetch_order(
        wechat_db, "order_1"
    )
    assert status == "PAID"
    # 083: the wechat trade reference lands in transaction_id; provider_trade_no
    # stays NULL (the constraint would reject a wechat row that set it).
    assert transaction_id == TRANSACTION_ID
    assert provider_trade_no is None
    assert notify_digest == SOURCE_DIGEST
    assert paid_at is not None

    # Wallet credited exactly the order's credits, once.
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS + _ORDER_CREDITS

    ledger = _charge_ledger(wechat_db, "order_1")
    assert len(ledger) == 1
    tx_type, available_delta, idempotency_key = ledger[0]
    assert tx_type == "CHARGE"
    assert int(available_delta) == _ORDER_CREDITS
    # The idempotency key carries the provider_name from the spec, not zpay's.
    assert idempotency_key == "wechat_native:charge:order_1"


# ---------------------------------------------------------------------------
# S2 — idempotent replay of an already-PAID notification
# ---------------------------------------------------------------------------


def test_settle_wechat_order_replay_is_idempotent(wechat_db: str) -> None:
    _seed_order(wechat_db, order_id="order_1", merchant_order_no=OUT_TRADE_NO)

    _settle()
    replayed = _settle()

    assert replayed["status"] == "PAID"
    assert replayed["trade_ref"] == TRANSACTION_ID

    # No double credit and no second ledger row: the PAID short-circuit returns
    # before the wallet UPDATE / CHARGE INSERT ever run again.
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS + _ORDER_CREDITS
    assert len(_charge_ledger(wechat_db, "order_1")) == 1


def test_wechat_receipt_funds_consumed_revenue_once(wechat_db: str) -> None:
    from app.usage_billing import accept_operation, finish_operation

    # This scene has no untracked opening balance; every credit has a paid receipt.
    with psycopg.connect(wechat_db) as raw:
        raw.execute("UPDATE wallets SET available_credits=0 WHERE user_id='user_1'")
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES('analysis',true,2)"
        )
    _seed_order(wechat_db, order_id="order_1", merchant_order_no=OUT_TRADE_NO)
    _settle()
    _settle()
    with psycopg.connect(wechat_db) as raw:
        conn = BusinessConnection.postgres(raw)
        op = accept_operation(
            conn, user_id="user_1", service="analysis", source_id="wechat-consumption", units=1
        )
        finish_operation(conn, operation_id=op, units=1, succeeded=True)
        assert tuple(
            raw.execute("SELECT count(*),sum(amount_fen) FROM billing_credit_lots").fetchone()
        ) == (1, _ORDER_AMOUNT_FEN)
        assert (
            raw.execute("SELECT revenue_fen FROM billing_operations WHERE id=%s", (op,)).fetchone()[
                0
            ]
            == 2000
        )


# ---------------------------------------------------------------------------
# S3 — a transaction_id already bound to another order is refused
# ---------------------------------------------------------------------------


def test_settle_wechat_order_rejects_transaction_id_bound_to_another_order(
    wechat_db: str,
) -> None:
    _seed_order(wechat_db, order_id="order_1", merchant_order_no=OUT_TRADE_NO)
    _seed_order(wechat_db, order_id="order_2", merchant_order_no="20260912w2")

    # Bind TRANSACTION_ID to order_1 first.
    _settle(merchant_order_no=OUT_TRADE_NO, provider_trade_no=TRANSACTION_ID)

    # Reusing the same transaction_id for order_2 must be refused.
    with pytest.raises(PaymentConfirmationError) as exc_info:
        _settle(merchant_order_no="20260912w2", provider_trade_no=TRANSACTION_ID)
    assert exc_info.value.code == "WECHAT_TRADE_ALREADY_BOUND"

    # order_2 stays unsettled and its wallet was never credited.
    status, transaction_id, provider_trade_no, _, paid_at = _fetch_order(wechat_db, "order_2")
    assert status == "PENDING"
    assert transaction_id is None
    assert provider_trade_no is None
    assert paid_at is None
    assert len(_charge_ledger(wechat_db, "order_2")) == 0


def test_concurrent_settlements_sharing_one_transaction_id_credit_exactly_once(
    wechat_db: str,
) -> None:
    """S3b — 应用层 bound_order 预检查在并发下互相看不见对方的未提交行。

    两个不同订单的结算并发携带同一 ``transaction_id`` 时，唯一能拦住双份入账的
    是 20260924T0000 的部分唯一索引；败者的 UPDATE 以 23505 落在该索引上，
    ``confirm_recharge_payment`` 按约束名把它映射回预检查同款
    ``WECHAT_TRADE_ALREADY_BOUND``，整个结算事务（含资金流水）原样回滚。
    """
    _seed_order(wechat_db, order_id="order_w1", merchant_order_no="20260912w1")
    _seed_order(wechat_db, order_id="order_w2", merchant_order_no="20260912w2")

    def _settle_order(merchant_order_no: str) -> str:
        try:
            _settle(merchant_order_no=merchant_order_no)
        except PaymentConfirmationError as exc:
            return exc.code
        return "SETTLED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(_settle_order, ["20260912w1", "20260912w2"]))

    assert outcomes == ["SETTLED", "WECHAT_TRADE_ALREADY_BOUND"]
    # 只有一单 PAID、一份 CHARGE 流水；钱包恰好入账一次。
    with psycopg.connect(wechat_db) as raw:
        assert raw.execute(
            "SELECT count(*) FROM recharge_orders WHERE status = 'PAID'"
        ).fetchone() == (1,)
        assert raw.execute(
            "SELECT count(*) FROM wallet_transactions WHERE type = 'CHARGE'"
        ).fetchone() == (1,)
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS + _ORDER_CREDITS


# ---------------------------------------------------------------------------
# S4 — an amount that disagrees with the stored order is refused
# ---------------------------------------------------------------------------


def test_settle_wechat_order_rejects_amount_mismatch(wechat_db: str) -> None:
    _seed_order(wechat_db, order_id="order_1", merchant_order_no=OUT_TRADE_NO)

    with pytest.raises(PaymentConfirmationError) as exc_info:
        _settle(amount_fen=_ORDER_AMOUNT_FEN + 1000)
    assert exc_info.value.code == "WECHAT_AMOUNT_MISMATCH"

    status, transaction_id, _, _, paid_at = _fetch_order(wechat_db, "order_1")
    assert status == "PENDING"
    assert transaction_id is None
    assert paid_at is None
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS
    assert len(_charge_ledger(wechat_db, "order_1")) == 0


# ---------------------------------------------------------------------------
# S5 — a channel outside the wxpay universe is refused
# ---------------------------------------------------------------------------


def test_settle_wechat_order_rejects_channel_outside_wxpay(wechat_db: str) -> None:
    _seed_order(wechat_db, order_id="order_1", merchant_order_no=OUT_TRADE_NO)

    with pytest.raises(PaymentConfirmationError) as exc_info:
        _settle(channel="alipay")
    assert exc_info.value.code == "WECHAT_CHANNEL_MISMATCH"

    status, _, _, _, paid_at = _fetch_order(wechat_db, "order_1")
    assert status == "PENDING"
    assert paid_at is None
    assert len(_charge_ledger(wechat_db, "order_1")) == 0


# ---------------------------------------------------------------------------
# S6 — a wechat callback cannot settle a zpay order
# ---------------------------------------------------------------------------


def test_settle_wechat_spec_rejects_zpay_order(wechat_db: str) -> None:
    # A zpay PENDING order (no prepay_id/code_url); settling it with the wechat
    # spec must fail the provider guard before any fund movement.
    _seed_order(wechat_db, order_id="order_z", merchant_order_no=OUT_TRADE_NO, wechat=False)

    with pytest.raises(PaymentConfirmationError) as exc_info:
        _settle()
    assert exc_info.value.code == "WECHAT_PROVIDER_MISMATCH"

    status, _, provider_trade_no, _, paid_at = _fetch_order(wechat_db, "order_z")
    assert status == "PENDING"
    assert provider_trade_no is None
    assert paid_at is None
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS
    assert len(_charge_ledger(wechat_db, "order_z")) == 0


# ---------------------------------------------------------------------------
# S7 — the expiry sweep: unpaid Native orders must not stay PENDING forever
# ---------------------------------------------------------------------------

_LAPSED_CREATED_AT = "2026-01-01 00:00:00+00"


def _sweep(*, limit: int = 100) -> int:
    with pg_transaction() as raw:
        return close_expired_native_orders(BusinessConnection.postgres(raw), limit=limit)


def _backlog() -> int:
    with pg_transaction() as raw:
        return count_expired_native_orders(BusinessConnection.postgres(raw))


def _status(dsn: str, order_id: str) -> str:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT status FROM recharge_orders WHERE id = %s", (order_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_expiry_sweep_closes_only_lapsed_unpaid_native_orders(wechat_db: str) -> None:
    """Everything the sweep must leave alone is seeded alongside what it must close."""
    _seed_order(wechat_db, order_id="fresh", merchant_order_no="sweep_fresh")
    _seed_order(
        wechat_db,
        order_id="lapsed",
        merchant_order_no="sweep_lapsed",
        created_at=_LAPSED_CREATED_AT,
    )
    _seed_order(
        wechat_db,
        order_id="lapsed_paid",
        merchant_order_no="sweep_paid",
        created_at=_LAPSED_CREATED_AT,
        status="PAID",
        transaction_id=OTHER_TRANSACTION_ID,
        paid_at="2026-01-01 00:30:00+00",
    )
    _seed_order(
        wechat_db,
        order_id="lapsed_zpay",
        merchant_order_no="sweep_zpay",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )

    assert _backlog() == 1
    assert _sweep() == 1

    assert _status(wechat_db, "lapsed") == "CLOSED"
    assert _status(wechat_db, "fresh") == "PENDING"
    # A settled order keeps its money; ZPay orders have their own lifecycle.
    assert _status(wechat_db, "lapsed_paid") == "PAID"
    assert _status(wechat_db, "lapsed_zpay") == "PENDING"
    assert _backlog() == 0


def test_expiry_sweep_honours_its_limit_and_leaves_the_rest_for_the_next_run(
    wechat_db: str,
) -> None:
    for index in range(3):
        _seed_order(
            wechat_db,
            order_id=f"lapsed_{index}",
            merchant_order_no=f"sweep_batch_{index}",
            created_at=_LAPSED_CREATED_AT,
        )

    assert _backlog() == 3
    assert _sweep(limit=2) == 2
    assert _backlog() == 1
    assert _sweep(limit=2) == 1
    assert _backlog() == 0


def test_a_swept_order_still_settles_a_late_notification(wechat_db: str) -> None:
    """Closing is bookkeeping, not a refusal: WeChat retries for hours after expiry."""
    _seed_order(
        wechat_db,
        order_id="order_1",
        merchant_order_no=OUT_TRADE_NO,
        created_at=_LAPSED_CREATED_AT,
    )
    assert _sweep() == 1
    assert _status(wechat_db, "order_1") == "CLOSED"

    _settle()

    status, transaction_id, provider_trade_no, _, paid_at = _fetch_order(wechat_db, "order_1")
    assert status == "PAID"
    assert transaction_id == TRANSACTION_ID
    assert provider_trade_no is None
    assert paid_at is not None
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS + _ORDER_CREDITS
    assert len(_charge_ledger(wechat_db, "order_1")) == 1


# ---------------------------------------------------------------------------
# S8 — the reconciliation sweep: a lost callback is recovered by asking WeChat
# ---------------------------------------------------------------------------

_STALE_CREATED_AT = "2026-01-01 00:00:00+00"
_RECONCILE_GRACE_SECONDS = 60


def _paid_query(order_no: str, *, transaction_id: str = TRANSACTION_ID) -> WeChatOrderQueryResult:
    """What WeChat answers when the customer paid and our callback never landed."""
    return WeChatOrderQueryResult(
        trade_state="SUCCESS",
        paid=True,
        out_trade_no=order_no,
        transaction_id=transaction_id,
        amount_fen=_ORDER_AMOUNT_FEN,
        response_digest=SOURCE_DIGEST,
    )


def _unpaid_query(order_no: str) -> WeChatOrderQueryResult:
    return WeChatOrderQueryResult(
        trade_state="NOTPAY",
        paid=False,
        out_trade_no=order_no,
        transaction_id=None,
        amount_fen=None,
        response_digest=SOURCE_DIGEST,
    )


def _reconcile(
    query: Callable[[str], WeChatOrderQueryResult],
    *,
    grace_seconds: int = _RECONCILE_GRACE_SECONDS,
    limit: int = 100,
) -> NativeReconciliationOutcome:
    with pg_transaction() as raw:
        return reconcile_stale_pending_native_orders(
            BusinessConnection.postgres(raw),
            query_order=query,
            grace_seconds=grace_seconds,
            limit=limit,
        )


def _stale_backlog(grace_seconds: int = _RECONCILE_GRACE_SECONDS) -> int:
    with pg_transaction() as raw:
        return count_stale_pending_native_orders(
            BusinessConnection.postgres(raw), grace_seconds=grace_seconds
        )


def test_reconciliation_queries_only_stale_pending_native_orders(wechat_db: str) -> None:
    """Fresh, settled, closed and ZPay orders are not the sweep's business."""
    _seed_order(
        wechat_db,
        order_id="stale",
        merchant_order_no="recon_stale",
        created_at=_STALE_CREATED_AT,
    )
    _seed_order(wechat_db, order_id="fresh", merchant_order_no="recon_fresh")
    _seed_order(
        wechat_db,
        order_id="stale_paid",
        merchant_order_no="recon_paid",
        created_at=_STALE_CREATED_AT,
        status="PAID",
        transaction_id=OTHER_TRANSACTION_ID,
        paid_at="2026-01-01 00:30:00+00",
    )
    _seed_order(
        wechat_db,
        order_id="stale_closed",
        merchant_order_no="recon_closed",
        created_at=_STALE_CREATED_AT,
        status="CLOSED",
    )
    _seed_order(
        wechat_db,
        order_id="stale_zpay",
        merchant_order_no="recon_zpay",
        wechat=False,
        created_at=_STALE_CREATED_AT,
    )

    assert _stale_backlog() == 1

    queried: list[str] = []

    def spy_query(order_no: str) -> WeChatOrderQueryResult:
        queried.append(order_no)
        return _unpaid_query(order_no)

    outcome = _reconcile(spy_query)

    assert queried == ["recon_stale"]
    assert outcome == NativeReconciliationOutcome(
        queried=1, settled=0, unpaid=1, skipped=0, unpaid_order_nos=("recon_stale",)
    )
    # The unpaid answer stays PENDING: retiring it is the expiry sweep's job.
    assert _status(wechat_db, "stale") == "PENDING"
    assert _stale_backlog() == 1


def test_reconciliation_settles_a_wechat_confirmed_payment(wechat_db: str) -> None:
    """The case that motivates the sweep: money arrived, our callback never did."""
    _seed_order(
        wechat_db,
        order_id="order_1",
        merchant_order_no=OUT_TRADE_NO,
        created_at=_STALE_CREATED_AT,
    )

    outcome = _reconcile(_paid_query)

    assert outcome == NativeReconciliationOutcome(queried=1, settled=1, unpaid=0, skipped=0)
    status, transaction_id, provider_trade_no, _, paid_at = _fetch_order(wechat_db, "order_1")
    assert status == "PAID"
    assert transaction_id == TRANSACTION_ID  # 083: settlement writes transaction_id,
    assert provider_trade_no is None  # ...never provider_trade_no
    assert paid_at is not None
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS + _ORDER_CREDITS
    assert len(_charge_ledger(wechat_db, "order_1")) == 1
    # A settled order leaves the candidate set: re-running the sweep is a no-op.
    assert _reconcile(_paid_query) == NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )


def test_reconciliation_survives_a_failing_query_and_finishes_the_rest(wechat_db: str) -> None:
    """One unreachable order must not strand the recoverable ones behind it."""
    _seed_order(
        wechat_db,
        order_id="dead",
        merchant_order_no="recon_dead",
        created_at="2026-01-01 00:00:00+00",
    )
    _seed_order(
        wechat_db,
        order_id="alive",
        merchant_order_no="recon_alive",
        created_at="2026-01-02 00:00:00+00",
    )

    def flaky_query(order_no: str) -> WeChatOrderQueryResult:
        if order_no == "recon_dead":
            raise WeChatNativeError("WeChat API request timed out", status_code=504)
        return _paid_query(order_no)

    outcome = _reconcile(flaky_query)

    assert outcome == NativeReconciliationOutcome(queried=1, settled=1, unpaid=0, skipped=1)
    assert _status(wechat_db, "dead") == "PENDING"  # the next run asks again
    assert _status(wechat_db, "alive") == "PAID"


def test_reconciliation_refuses_a_gateway_answer_for_a_different_order(wechat_db: str) -> None:
    """The sweep settles only what it asked about, whatever the gateway claims."""
    _seed_order(
        wechat_db,
        order_id="order_1",
        merchant_order_no=OUT_TRADE_NO,
        created_at=_STALE_CREATED_AT,
    )
    hijacked = WeChatOrderQueryResult(
        trade_state="SUCCESS",
        paid=True,
        out_trade_no="someone_elses_order",
        transaction_id=TRANSACTION_ID,
        amount_fen=_ORDER_AMOUNT_FEN,
        response_digest=SOURCE_DIGEST,
    )

    outcome = _reconcile(lambda order_no: hijacked)

    assert outcome == NativeReconciliationOutcome(queried=1, settled=0, unpaid=0, skipped=1)
    assert _status(wechat_db, "order_1") == "PENDING"
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS


def test_reconciliation_refuses_an_amount_that_disagrees_with_the_order(wechat_db: str) -> None:
    """confirm_recharge_payment re-validates: a disputed answer settles nothing."""
    _seed_order(
        wechat_db,
        order_id="order_1",
        merchant_order_no=OUT_TRADE_NO,
        created_at=_STALE_CREATED_AT,
    )
    underpaid = WeChatOrderQueryResult(
        trade_state="SUCCESS",
        paid=True,
        out_trade_no=OUT_TRADE_NO,
        transaction_id=TRANSACTION_ID,
        amount_fen=1,
        response_digest=SOURCE_DIGEST,
    )

    outcome = _reconcile(lambda order_no: underpaid)

    assert outcome == NativeReconciliationOutcome(queried=1, settled=0, unpaid=0, skipped=1)
    assert _status(wechat_db, "order_1") == "PENDING"
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS
    assert len(_charge_ledger(wechat_db, "order_1")) == 0


def test_reconciliation_honours_its_limit_and_leaves_the_rest(wechat_db: str) -> None:
    """A capped run settles its share; the next run picks up exactly the rest.

    Paid answers shrink the candidate set (unlike unpaid ones, which stay
    PENDING by design), so successive runs walk the backlog to zero.
    """
    for index in range(3):
        _seed_order(
            wechat_db,
            order_id=f"stale_{index}",
            merchant_order_no=f"recon_batch_{index}",
            created_at=_STALE_CREATED_AT,
        )

    def unique_paid_query(order_no: str) -> WeChatOrderQueryResult:
        # One distinct transaction id per order: a trade number bound to another
        # order would (rightly) be refused by the settlement guards.
        suffix = order_no.rsplit("_", 1)[1]
        return _paid_query(order_no, transaction_id=f"{TRANSACTION_ID[:-1]}{suffix}")

    assert _reconcile(unique_paid_query, limit=2) == NativeReconciliationOutcome(
        queried=2, settled=2, unpaid=0, skipped=0
    )
    assert _reconcile(unique_paid_query, limit=2) == NativeReconciliationOutcome(
        queried=1, settled=1, unpaid=0, skipped=0
    )
    assert _reconcile(unique_paid_query, limit=2) == NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# M3（2026-11-15 审计修复）—— ZPay 侧兜底与双 provider 聚合 sweep。
# 生产是 compose 部署，systemd maintenance timer 不存在：CLI 版 sweep 从未
# 在生产跑过，34 笔待支付（含 ¥2980）挂 8 天。worker 常驻 sweep 必须把两个
# provider 一并覆盖。
# ---------------------------------------------------------------------------

_ZPAY_TRADE_NO = "zp-20261115-0001"
_ZPAY_DIGEST = "cw070-zpay-source-digest"


def _zpay_sweep(*, limit: int = 100, validity_seconds: int = 2 * 60 * 60) -> int:
    with pg_transaction() as raw:
        return close_expired_zpay_orders(
            BusinessConnection.postgres(raw), limit=limit, validity_seconds=validity_seconds
        )


def _zpay_paid_query(order_no: str) -> ZPayOrderQueryResult:
    return ZPayOrderQueryResult(
        paid=True,
        merchant_order_no=order_no,
        provider_trade_no=_ZPAY_TRADE_NO,
        amount_fen=_ORDER_AMOUNT_FEN,
        channel="alipay",
        response_digest=_ZPAY_DIGEST,
    )


def _zpay_unpaid_query(order_no: str) -> ZPayOrderQueryResult:
    return ZPayOrderQueryResult(
        paid=False,
        merchant_order_no=order_no,
        provider_trade_no=None,
        amount_fen=None,
        channel=None,
        response_digest=_ZPAY_DIGEST,
    )


def _zpay_reconcile(
    query, *, grace_seconds: int = _RECONCILE_GRACE_SECONDS, limit: int = 100
) -> NativeReconciliationOutcome:
    with pg_transaction() as raw:
        return reconcile_stale_pending_zpay_orders(
            BusinessConnection.postgres(raw),
            query_order=query,
            grace_seconds=grace_seconds,
            limit=limit,
        )


def test_zpay_expiry_sweep_closes_only_lapsed_unpaid_zpay_orders(wechat_db: str) -> None:
    _seed_order(wechat_db, order_id="zfresh", merchant_order_no="zsweep_fresh", wechat=False)
    _seed_order(
        wechat_db,
        order_id="zlapsed",
        merchant_order_no="zsweep_lapsed",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )
    _seed_order(
        wechat_db,
        order_id="zlapsed_paid",
        merchant_order_no="zsweep_paid",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="PAID",
        provider_trade_no=OTHER_TRANSACTION_ID,
        paid_at="2026-01-01 00:30:00+00",
    )
    _seed_order(
        wechat_db,
        order_id="wlapsed",
        merchant_order_no="wsweep_lapsed",
        wechat=True,
        created_at=_LAPSED_CREATED_AT,
    )

    assert _zpay_sweep() == 1
    assert _status(wechat_db, "zlapsed") == "CLOSED"
    assert _status(wechat_db, "zfresh") == "PENDING"
    assert _status(wechat_db, "zlapsed_paid") == "PAID"
    # 微信单不属于 ZPay sweep 的管辖。
    assert _status(wechat_db, "wlapsed") == "PENDING"


def test_zpay_reconciliation_settles_a_gateway_confirmed_payment(wechat_db: str) -> None:
    _seed_order(
        wechat_db,
        order_id="zorder_1",
        merchant_order_no="zrecon_paid",
        wechat=False,
        created_at=_STALE_CREATED_AT,
    )

    outcome = _zpay_reconcile(_zpay_paid_query)

    assert outcome == NativeReconciliationOutcome(queried=1, settled=1, unpaid=0, skipped=0)
    with psycopg.connect(wechat_db) as raw:
        row = raw.execute(
            "SELECT status, provider_trade_no, paid_at FROM recharge_orders WHERE id='zorder_1'"
        ).fetchone()
    assert row is not None
    assert row[0] == "PAID"
    # ZPAY_SETTLEMENT_SPEC：凭证落 provider_trade_no 列（与微信的 transaction_id 相对）。
    assert row[1] == _ZPAY_TRADE_NO
    assert row[2] is not None
    assert _wallet_credits(wechat_db) == _SEEDED_WALLET_CREDITS + _ORDER_CREDITS
    # 幂等：已结算的订单退出候选集。
    assert _zpay_reconcile(_zpay_paid_query) == NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )


def test_zpay_reconciliation_unpaid_answer_stays_pending(wechat_db: str) -> None:
    _seed_order(
        wechat_db,
        order_id="zorder_2",
        merchant_order_no="zrecon_unpaid",
        wechat=False,
        created_at=_STALE_CREATED_AT,
    )

    outcome = _zpay_reconcile(_zpay_unpaid_query)

    assert outcome == NativeReconciliationOutcome(
        queried=1, settled=0, unpaid=1, skipped=0, unpaid_order_nos=("zrecon_unpaid",)
    )
    assert _status(wechat_db, "zorder_2") == "PENDING"


def test_aggregate_sweep_covers_both_providers_in_one_pass(wechat_db: str) -> None:
    """一次 sweep：微信掉单补账 + ZPay 过期关闭，聚合计数正确；未配置通道跳过。"""
    _seed_order(
        wechat_db,
        order_id="m_native",
        merchant_order_no=OUT_TRADE_NO,
        wechat=True,
        created_at=_STALE_CREATED_AT,
    )
    _seed_order(
        wechat_db,
        order_id="m_zpay",
        merchant_order_no="m_zpay_lapsed",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )

    outcome = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=_paid_query,
        zpay_query_order=_zpay_unpaid_query,
    )

    assert outcome.queried == 2
    assert outcome.settled == 1
    assert outcome.unpaid == 1
    assert outcome.closed == 1
    assert _status(wechat_db, "m_native") == "PAID"
    assert _status(wechat_db, "m_zpay") == "CLOSED"

    # 两个通道都未配置时：reconcile 段整段跳过（queried/settled 归零），关闭段照跑。
    outcome_none = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=None,
    )
    assert outcome_none.queried == 0
    assert outcome_none.settled == 0
    assert outcome_none.closed == 0


def test_sweep_never_closes_an_order_it_did_not_confirm_unpaid(wechat_db: str) -> None:
    """评审 P1 回归：查单失败/没轮到查的单绝不被关闭，已确认 unpaid 的才关。

    一旦 CLOSED，reconcile 与管理端补单都不再受理——把可能的掉单关在门外
    等于把钱弄丢。
    """
    # paid 且查单会失败的单（挂单时间均已过各自窗口）。
    _seed_order(
        wechat_db,
        order_id="q_fail",
        merchant_order_no="q_fail_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )
    _seed_order(
        wechat_db,
        order_id="q_unpaid",
        merchant_order_no="q_unpaid_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )
    # 超出 limit、本轮根本不会被查询的单。
    _seed_order(
        wechat_db,
        order_id="q_beyond",
        merchant_order_no="q_beyond_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )

    def flaky_query(order_no: str) -> ZPayOrderQueryResult:
        if order_no == "q_fail_no":
            raise RuntimeError("gateway timeout")
        return _zpay_unpaid_query(order_no)

    outcome = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=flaky_query,
        limit=2,
        rotation_index=0,
    )

    # 确认 unpaid 且过窗 → 关闭；查单失败与未查询 → 原样留在 PENDING。
    assert outcome.closed == 1
    assert _status(wechat_db, "q_unpaid") == "CLOSED"
    assert _status(wechat_db, "q_fail") == "PENDING"
    assert _status(wechat_db, "q_beyond") == "PENDING"


@pytest.mark.parametrize("rate_limited", [False, True])
def test_zpay_sweep_limits_requests_across_pending_and_closed(
    wechat_db: str, rate_limited: bool
) -> None:
    for index in range(6):
        order_no = f"sweep-limit-{index}"
        _seed_order(
            wechat_db,
            order_id=order_no,
            merchant_order_no=order_no,
            wechat=False,
            created_at=f"2026-01-01 0{index}:00:00+00",
        )
    _mark_closed(wechat_db, "sweep-limit-4")
    _mark_closed(wechat_db, "sweep-limit-5")
    queried: list[str] = []

    def query(order_no: str) -> ZPayOrderQueryResult:
        queried.append(order_no)
        if rate_limited and len(queried) == 2:
            raise OrderQueryError("rate limited", status_code=429)
        return _zpay_unpaid_query(order_no)

    with _autocommit(wechat_db) as raw:
        outcome = sweep_pending_recharge_orders(
            BusinessConnection.postgres(raw),
            wechat_query_order=None,
            zpay_query_order=query,
            long_tail=True,
            rotation_index=0,
        )

    assert _status(wechat_db, "sweep-limit-0") == "CLOSED"
    assert _status(wechat_db, "sweep-limit-3") == "PENDING"
    if rate_limited:
        assert queried == ["sweep-limit-0", "sweep-limit-1"]
        assert outcome.zpay.rate_limited
        assert outcome.zpay.skipped == 1
        assert outcome.closed == 1
        assert _status(wechat_db, "sweep-limit-1") == "PENDING"
    else:
        assert len(queried) == 5
        assert outcome.zpay.queried == 3
        assert outcome.closed_zpay.queried == outcome.tail_zpay.queried == 1


def test_zpay_reconciliation_settles_cross_channel_within_merchant_whitelist(
    wechat_db: str,
) -> None:
    """评审 P2 回归：预选 alipay、实际 wxpay，商户同时启用两通道 → 合法结算。"""
    _seed_order(
        wechat_db,
        order_id="zcross",
        merchant_order_no="zcross_no",
        wechat=False,
        created_at=_STALE_CREATED_AT,
    )
    # 网关说这笔是用 wxpay 付的（订单预选 alipay）。
    answer = ZPayOrderQueryResult(
        paid=True,
        merchant_order_no="zcross_no",
        provider_trade_no=_ZPAY_TRADE_NO,
        amount_fen=_ORDER_AMOUNT_FEN,
        channel="wxpay",
        response_digest=_ZPAY_DIGEST,
    )

    with pg_transaction() as raw:
        outcome = reconcile_stale_pending_zpay_orders(
            BusinessConnection.postgres(raw),
            query_order=lambda _no: answer,
            grace_seconds=_RECONCILE_GRACE_SECONDS,
            allowed_channels=("alipay", "wxpay"),
        )

    assert outcome == NativeReconciliationOutcome(queried=1, settled=1, unpaid=0, skipped=0)
    assert _status(wechat_db, "zcross") == "PAID"


def _autocommit(dsn: str):
    """sweep 契约要求 autocommit 连接（外呼期间不持事务），测试同生产同构。"""
    return psycopg.connect(dsn, autocommit=True)


def test_sweep_rotation_reaches_orders_behind_failing_queries(wechat_db: str) -> None:
    """评审 P1 回归：最老一批持续查单失败时，轮转游标必须让后面的掉单被问到。

    旧实现固定取最老 limit 条——最老的单查单失败就永远占着坑，新掉进来的
    已付款单永远轮不到补账。
    """
    _seed_order(
        wechat_db,
        order_id="rot_fail",
        merchant_order_no="rot_fail_no",
        wechat=False,
        created_at="2026-01-01 00:00:00+00",
    )
    _seed_order(
        wechat_db,
        order_id="rot_paid",
        merchant_order_no="rot_paid_no",
        wechat=False,
        created_at="2026-01-01 01:00:00+00",
    )

    def flaky_query(order_no: str) -> ZPayOrderQueryResult:
        if order_no == "rot_fail_no":
            raise RuntimeError("gateway keeps timing out")
        return _zpay_paid_query(order_no)

    # 第 0 页：最老的 rot_fail 被问到（失败跳过），rot_paid 还轮不到。
    outcome_a = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=flaky_query,
        limit=1,
        rotation_index=0,
    )
    # 查单失败计 skipped（不算 queried），轮转到下一页前 rot_paid 保持 PENDING。
    assert outcome_a.zpay.skipped == 1
    assert outcome_a.settled == 0
    assert _status(wechat_db, "rot_paid") == "PENDING"

    # 第 1 页：轮转让出位置，rot_paid 被问到并补账。
    outcome_b = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=flaky_query,
        limit=1,
        rotation_index=1,
    )
    assert outcome_b.settled == 1
    assert _status(wechat_db, "rot_paid") == "PAID"


# ---------------------------------------------------------------------------
# F3（2026-11-16 漏账缺口修复）：CLOSED 扫描段——关单不是「不可支付」的证明。
# zpay 无网关过期 → 7 天全窗；微信窗=关单后 validity+grace（竞态掉单在窗内）。
# ---------------------------------------------------------------------------


def _mark_closed(dsn: str, order_id: str, *, closed_at_sql: str | None = "now()") -> None:
    """置为 CLOSED 并按测试指定的 SQL 表达式落 ``closed_at``.

    ``closed_at_sql`` 是测试内受控的 SQL 字面量（``now()`` / ``now() - interval
    '8 days'``），必须内插进 SET 让数据库**求值**——走 %s 参数绑定会把表达式
    原样存成文本，扫描谓词的 ``::timestamptz`` 转换直接报错（评审 P2 实锤）。
    """
    assert closed_at_sql is None or closed_at_sql.startswith("now()"), (
        "只允许 now() 系表达式，防误绑字面量"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        if closed_at_sql is None:
            conn.execute(
                "UPDATE recharge_orders SET status = 'CLOSED', closed_at = NULL WHERE id = %s",
                (order_id,),
            )
        else:
            conn.execute(
                f"UPDATE recharge_orders SET status = 'CLOSED', "
                f"closed_at = {closed_at_sql} WHERE id = %s",
                (order_id,),
            )


def _wallet_credits(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = 'user_1'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_closed_scan_settles_a_zpay_order_paid_after_close(wechat_db: str) -> None:
    """zpay 关单后（网关无过期）用户拿旧码付款且回调丢失：扫描段当场补账。"""
    _seed_order(
        wechat_db,
        order_id="cs_zpaid",
        merchant_order_no="cs_zpaid_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    _mark_closed(wechat_db, "cs_zpaid")
    before = _wallet_credits(wechat_db)

    with pg_transaction() as raw:
        outcome = reconcile_recently_closed_zpay_orders(
            BusinessConnection.postgres(raw),
            query_order=_zpay_paid_query,
        )

    assert outcome == NativeReconciliationOutcome(queried=1, settled=1, unpaid=0, skipped=0)
    assert _status(wechat_db, "cs_zpaid") == "PAID"
    assert _wallet_credits(wechat_db) == before + _ORDER_CREDITS


def test_closed_scan_leaves_unpaid_orders_closed_and_untouched(wechat_db: str) -> None:
    """未付应答：原样留在 CLOSED（下一轮继续问），零资金副作用。"""
    _seed_order(
        wechat_db,
        order_id="cs_zunpaid",
        merchant_order_no="cs_zunpaid_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    _mark_closed(wechat_db, "cs_zunpaid")
    before = _wallet_credits(wechat_db)

    with pg_transaction() as raw:
        outcome = reconcile_recently_closed_zpay_orders(
            BusinessConnection.postgres(raw),
            query_order=_zpay_unpaid_query,
        )

    assert outcome == NativeReconciliationOutcome(
        queried=1, settled=0, unpaid=1, skipped=0, unpaid_order_nos=("cs_zunpaid_no",)
    )
    assert _status(wechat_db, "cs_zunpaid") == "CLOSED"
    assert _wallet_credits(wechat_db) == before


def test_closed_scan_windows_exclude_out_of_window_and_null_history(wechat_db: str) -> None:
    """窗口边界（评审 T5）：过期窗/无 closed_at 的历史存量都不进候选。"""
    _seed_order(
        wechat_db,
        order_id="cs_z_old",
        merchant_order_no="cs_z_old_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    # zpay：关单已 8 天 > 7 天窗 → 出窗。
    _mark_closed(wechat_db, "cs_z_old", closed_at_sql="now() - interval '8 days'")
    _seed_order(
        wechat_db,
        order_id="cs_w_old",
        merchant_order_no="cs_w_old_no",
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    # 微信：关单已 3 小时 > validity(2h)+grace(10min) → 出窗（time_expire 已退休码）。
    _mark_closed(wechat_db, "cs_w_old", closed_at_sql="now() - interval '3 hours'")
    _seed_order(
        wechat_db,
        order_id="cs_null",
        merchant_order_no="cs_null_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    # 历史 NULL（迁移前的 sweep/CLI 关单存量）→ 永不进候选，走独立追查批次。
    _mark_closed(wechat_db, "cs_null", closed_at_sql=None)
    _seed_order(
        wechat_db,
        order_id="cs_w_in",
        merchant_order_no="cs_w_in_no",
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    # 微信：关单 1 小时（< 2h+10min）→ 在窗内，竞态窗口未关死。
    _mark_closed(wechat_db, "cs_w_in", closed_at_sql="now() - interval '1 hour'")

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        assert count_recently_closed_zpay_orders(conn) == 0
        assert count_recently_closed_native_orders(conn) == 1

        # 出窗单与 NULL 历史都不产生任何网关查询。
        outcome_z = reconcile_recently_closed_zpay_orders(conn, query_order=_zpay_unpaid_query)
        assert outcome_z.queried == 0
        outcome_w = reconcile_recently_closed_native_orders(conn, query_order=_unpaid_query)
        assert outcome_w.queried == 1
        assert outcome_w.unpaid_order_nos == ("cs_w_in_no",)
    assert _status(wechat_db, "cs_w_in") == "CLOSED"


def test_sweep_race_notpay_then_paid_is_recovered_in_the_next_pass(wechat_db: str) -> None:
    """确定性竞态（评审 T2）：查单 NOTPAY → 到期前付款 → 关单 → 下一段捞回.

    同一轮 sweep 内：PENDING 段查到 unpaid 并关单；CLOSED 段随即再问同一单，
    此时网关已答 paid——当场补账。这正是「本地关单后支付成功且回调丢失」的
    最短闭环，也是微信通道必须纳入 F3 的原因。
    """
    _seed_order(
        wechat_db,
        order_id="race_z",
        merchant_order_no="race_z_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
    )
    calls: list[str] = []

    def racing_query(order_no: str) -> ZPayOrderQueryResult:
        calls.append(order_no)
        if len(calls) == 1:
            return _zpay_unpaid_query(order_no)  # 第一段：查单说没付
        return _zpay_paid_query(order_no)  # 关单后：其实已付

    outcome = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=racing_query,
    )
    assert calls == ["race_z_no", "race_z_no"], "同一单被 PENDING 段与 CLOSED 段各问一次"
    assert outcome.closed == 1, "PENDING 段按当时应答关单"
    assert outcome.closed_zpay.settled == 1, "CLOSED 段把竞态掉单当场补回"
    assert _status(wechat_db, "race_z") == "PAID"
    assert _wallet_credits(wechat_db) == 100 + _ORDER_CREDITS


def test_long_tail_recovers_orders_past_the_high_freq_window(wechat_db: str) -> None:
    """长尾层（评审 P1 落地，二轮收敛为无时间上界）：高频窗出窗的单仍被覆盖.

    覆盖三类候选：zpay 关单 8 天（> 7 天高频窗）、微信关单 3 小时（>
    validity+grace，worker 停机跨窗场景）、迁移前 closed_at IS NULL 存量。
    长尾候选=通道全部 CLOSED 单——到期前的历史支付查单永远答 SUCCESS，
    任何时间上界都会制造永久盲区（二轮评审 P1 的 181 天反例），因此这里
    **不设**排除用例。
    """
    _seed_order(
        wechat_db,
        order_id="tail_z_8d",
        merchant_order_no="tail_z_8d_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    _mark_closed(wechat_db, "tail_z_8d", closed_at_sql="now() - interval '8 days'")
    _seed_order(
        wechat_db,
        order_id="tail_w_3h",
        merchant_order_no="tail_w_3h_no",
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    _mark_closed(wechat_db, "tail_w_3h", closed_at_sql="now() - interval '3 hours'")
    _seed_order(
        wechat_db,
        order_id="tail_z_null",
        merchant_order_no="tail_z_null_no",
        wechat=False,
        created_at="2024-01-01 00:00:00+00",
        status="CLOSED",
    )
    # 迁移前存量：NULL 关单时间，created_at 远古 → 仍是候选（NULL 只影响
    # 排序锚点，不产生盲区）。
    _mark_closed(wechat_db, "tail_z_null", closed_at_sql=None)
    before = _wallet_credits(wechat_db)

    def tail_zpay_query(order_no: str) -> ZPayOrderQueryResult:
        # 8 天前关的单其实已付（每单独立交易号——共享桩的固定交易号会让第二单
        # 撞 TRADE_ALREADY_BOUND，测不出 unpaid 分支）；NULL 存量未付。
        if order_no == "tail_z_8d_no":
            return _zpay_paid_query(order_no)
        return _zpay_unpaid_query(order_no)

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        assert count_closed_zpay_long_tail(conn) == 2
        assert count_closed_native_long_tail(conn) == 1

        outcome_z = reconcile_closed_zpay_long_tail(conn, query_order=tail_zpay_query, limit=20)
        outcome_w = reconcile_closed_native_long_tail(conn, query_order=_unpaid_query, limit=20)

    assert outcome_z.queried == 2 and outcome_z.settled == 1
    assert outcome_z.unpaid_order_nos == ("tail_z_null_no",), "NULL 存量未付 → 留在 CLOSED"
    assert outcome_w.queried == 1 and outcome_w.unpaid == 1
    assert _status(wechat_db, "tail_z_8d") == "PAID", "第 8 天才付的 zpay 单被长尾捞回"
    assert _wallet_credits(wechat_db) == before + _ORDER_CREDITS


def test_sweep_long_tail_flag_runs_the_tail_segment(wechat_db: str) -> None:
    """sweep 的 long_tail 慢频档：默认轮次不跑，long_tail=True 才跑长尾段."""
    _seed_order(
        wechat_db,
        order_id="tailflag_z",
        merchant_order_no="tailflag_z_no",
        wechat=False,
        created_at=_LAPSED_CREATED_AT,
        status="CLOSED",
    )
    _mark_closed(wechat_db, "tailflag_z", closed_at_sql="now() - interval '8 days'")

    default_outcome = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=_zpay_unpaid_query,
    )
    assert default_outcome.tail_zpay.queried == 0, "默认轮次不跑长尾段"

    tail_outcome = sweep_pending_recharge_orders(
        BusinessConnection.postgres(_autocommit(wechat_db)),
        wechat_query_order=None,
        zpay_query_order=_zpay_unpaid_query,
        long_tail=True,
    )
    assert tail_outcome.tail_zpay.queried == 1
    assert _status(wechat_db, "tailflag_z") == "CLOSED", "未付应答原样保留"
