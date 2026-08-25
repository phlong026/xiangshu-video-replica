import { useCallback, useEffect, useState } from "react";
import {
  type CreatedRechargeOrder,
  CustomerApiError,
  customerCreateRechargeOrder,
  customerGetRechargeOrder,
  customerGetWallet,
  customerListRechargeOrders,
  customerListWalletTransactions,
  type RechargeOrder,
  type WalletSnapshot,
  type WalletTransaction,
} from "../api";
import type { CustomerCredentialStore } from "./useCustomerSession";

const RECHARGE_PRESETS_YUAN = [100, 200, 500, 1000] as const;
const ORDER_POLL_INTERVAL_MS = 2_000;
const MAX_ORDER_POLL_ATTEMPTS = 30;

/** The customer wallet view (task #7): balance, recharge and orders under the
 * customer session. Mirrors the internal WalletPanel but talks to the
 * customer-lane endpoints (the internal wallet API 401'd for a customer
 * session). Never a second main-code entry — recharge reuses the same wallet. */
export function CustomerWalletPanel({
  store,
  onSessionExpired,
}: {
  store: CustomerCredentialStore;
  onSessionExpired: () => void;
}) {
  const [wallet, setWallet] = useState<WalletSnapshot | null>(null);
  const [transactions, setTransactions] = useState<WalletTransaction[]>([]);
  const [orders, setOrders] = useState<RechargeOrder[]>([]);
  const [customAmount, setCustomAmount] = useState("");
  const [pendingOrderNo, setPendingOrderNo] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);

  const loadSession = useCallback(async (): Promise<{
    kind: "session";
    token: string;
  } | null> => {
    const token = await store.loadSessionToken();
    if (token === null) {
      onSessionExpired();
      return null;
    }
    return { kind: "session", token };
  }, [store, onSessionExpired]);

  const refresh = useCallback(async () => {
    const credential = await loadSession();
    if (credential === null) {
      return;
    }
    const [nextWallet, transactionPage, orderPage] = await Promise.all([
      customerGetWallet(credential),
      customerListWalletTransactions(credential),
      customerListRechargeOrders(credential),
    ]);
    setWallet(nextWallet);
    setTransactions(transactionPage.items);
    setOrders(orderPage.items);
    // Codex P2 (PR #65): the pending order number lives only in component
    // state, so reopening/remounting the wallet (or exhausting the poll
    // window) would never resume tracking an outstanding payment. Derive the
    // most recent still-pending order from the fetched list and restart the
    // status poll — unless one is already being tracked.
    const pending = orderPage.items.find((order) => order.status === "PENDING");
    if (pending) {
      setPendingOrderNo((current) => current ?? pending.order_no);
    }
  }, [loadSession]);

  useEffect(() => {
    let active = true;
    refresh()
      .then(() => {
        if (active) {
          setError("");
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(errorMessage(cause, "钱包暂不可用，请稍后重试。"));
        }
      })
      .finally(() => {
        if (active) {
          setIsLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [refresh]);

  useEffect(() => {
    if (!pendingOrderNo) {
      return;
    }
    let active = true;
    let timer: number | undefined;
    let attempts = 0;

    async function checkOrder() {
      attempts += 1;
      const credential = await loadSession();
      if (!active || credential === null) {
        return;
      }
      try {
        const order = await customerGetRechargeOrder(
          credential,
          pendingOrderNo as string,
        );
        if (!active) {
          return;
        }
        setError("");
        if (order.status === "PAID") {
          setPendingOrderNo(null);
          setNotice("充值已到账，钱包余额已更新。");
          await refresh();
          return;
        }
        if (order.status === "FAILED" || order.status === "CLOSED") {
          setPendingOrderNo(null);
          setNotice("该充值订单已结束，未增加条数。");
          await refresh();
          return;
        }
        setNotice("支付结果确认中，请完成支付后返回本页。");
        if (attempts >= MAX_ORDER_POLL_ATTEMPTS) {
          setNotice("支付结果仍待确认，可稍后刷新页面继续查询。");
          return;
        }
        timer = window.setTimeout(checkOrder, ORDER_POLL_INTERVAL_MS);
      } catch (cause) {
        if (!active) {
          return;
        }
        setError(errorMessage(cause, "暂时无法查询充值状态。"));
        if (attempts >= MAX_ORDER_POLL_ATTEMPTS) {
          return;
        }
        timer = window.setTimeout(checkOrder, ORDER_POLL_INTERVAL_MS);
      }
    }

    void checkOrder();
    return () => {
      active = false;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [pendingOrderNo, refresh, loadSession]);

  async function startRecharge(amountYuan: number) {
    if (!wallet || isCreating) {
      return;
    }
    const amountFen = amountYuan * 100;
    if (
      !Number.isInteger(amountYuan) ||
      amountFen < wallet.min_recharge_fen ||
      amountFen % wallet.recharge_step_fen !== 0
    ) {
      setError(
        `充值金额须为${formatFen(wallet.min_recharge_fen)}起，并按${formatFen(wallet.recharge_step_fen)}递增。`,
      );
      return;
    }
    const credential = await loadSession();
    if (credential === null) {
      return;
    }
    setIsCreating(true);
    setError("");
    setNotice("");
    try {
      const created = await customerCreateRechargeOrder(credential, amountFen, {
        idempotencyKey: crypto.randomUUID(),
      });
      setPendingOrderNo(created.order_no);
      setNotice("支付页已打开，本页会自动确认到账。");
      submitPaymentForm(created);
      setOrders((current) => [createdOrderStatus(created), ...current]);
    } catch (cause) {
      setError(errorMessage(cause, "创建充值订单失败。"));
    } finally {
      setIsCreating(false);
    }
  }

  function submitCustomAmount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void startRecharge(Number(customAmount));
  }

  if (isLoading && !wallet) {
    return <p className="status-note">正在读取钱包</p>;
  }

  if (!wallet) {
    return (
      <section className="settings-error" role="alert">
        {error || "钱包暂不可用。"}
      </section>
    );
  }

  return (
    <section className="wallet-page" aria-label="余额与充值">
      <div className="wallet-summary-grid">
        <article className="wallet-summary-card">
          <span>内部价</span>
          <strong>{formatFen(wallet.internal_unit_price_fen)} / 条</strong>
        </article>
        <article className="wallet-summary-card">
          <span>可用条数</span>
          <strong>{wallet.available_credits}</strong>
          <small>冻结 {wallet.reserved_credits} 条</small>
        </article>
      </div>

      <section className="wallet-section" aria-labelledby="recharge-title">
        <div className="wallet-section__heading">
          <div>
            <h2 id="recharge-title">充值条数</h2>
            <p>
              {formatFen(wallet.min_recharge_fen)}起充，按
              {formatFen(wallet.recharge_step_fen)}递增。
            </p>
          </div>
        </div>
        <div className="recharge-presets">
          {RECHARGE_PRESETS_YUAN.map((amount) => (
            <button
              aria-label={`充值${amount}元`}
              disabled={isCreating}
              key={amount}
              onClick={() => void startRecharge(amount)}
              type="button"
            >
              <strong>{amount} 元</strong>
              <span>
                {Math.floor((amount * 100) / wallet.internal_unit_price_fen)} 条
              </span>
            </button>
          ))}
        </div>
        <form
          className="custom-recharge-form"
          noValidate
          onSubmit={submitCustomAmount}
        >
          <label>
            自定义充值金额（元）
            <input
              inputMode="numeric"
              min={wallet.min_recharge_fen / 100}
              step={wallet.recharge_step_fen / 100}
              type="number"
              value={customAmount}
              onChange={(event) => setCustomAmount(event.target.value)}
            />
          </label>
          <button disabled={isCreating} type="submit">
            {isCreating ? "正在创建订单" : "确认充值"}
          </button>
        </form>
        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        {notice ? (
          <p className="wallet-notice" role="status">
            {notice}
          </p>
        ) : null}
      </section>

      <section className="wallet-section" aria-labelledby="orders-title">
        <h2 id="orders-title">最近充值订单</h2>
        <div className="table-scroll">
          <table className="internal-table">
            <thead>
              <tr>
                <th>订单号</th>
                <th>金额</th>
                <th>条数</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {orders.length ? (
                orders.map((order) => (
                  <tr key={order.order_no}>
                    <td>{order.order_no}</td>
                    <td>{formatFen(order.amount_fen)}</td>
                    <td>{order.credits}</td>
                    <td>{orderStatusLabel(order.status)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={4}>暂无充值订单</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="wallet-section" aria-labelledby="ledger-title">
        <h2 id="ledger-title">条数流水</h2>
        <div className="table-scroll">
          <table className="internal-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>可用变化</th>
              </tr>
            </thead>
            <tbody>
              {transactions.length ? (
                transactions.map((transaction) => (
                  <tr key={transaction.id}>
                    <td>{transaction.created_at}</td>
                    <td>{transactionTypeLabel(transaction.type)}</td>
                    <td>{signedNumber(transaction.available_delta)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={3}>暂无条数流水</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}

function submitPaymentForm(order: CreatedRechargeOrder) {
  const form = document.createElement("form");
  form.method = order.method;
  form.action = order.gateway_url;
  form.target = "_blank";
  form.setAttribute("rel", "noopener");
  for (const [name, value] of Object.entries(order.form_fields)) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
  form.remove();
}

function createdOrderStatus(order: CreatedRechargeOrder): RechargeOrder {
  return {
    order_no: order.order_no,
    status: order.status,
    amount_fen: order.amount_fen,
    credits: order.credits,
    channel: order.form_fields.type ?? "",
    created_at: new Date().toISOString(),
    paid_at: null,
  };
}

function formatFen(amountFen: number): string {
  const yuan = amountFen / 100;
  return `${Number.isInteger(yuan) ? yuan : yuan.toFixed(2)}元`;
}

function signedNumber(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}

function orderStatusLabel(status: RechargeOrder["status"]): string {
  return {
    PENDING: "待支付",
    PAID: "已到账",
    FAILED: "支付失败",
    CLOSED: "已关闭",
  }[status];
}

function transactionTypeLabel(type: WalletTransaction["type"]): string {
  return {
    CHARGE: "充值到账",
    RESERVE: "任务冻结",
    SETTLE: "成功结算",
    RELEASE: "失败返还",
  }[type];
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof CustomerApiError && error.message.trim()) {
    return error.message;
  }
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}
