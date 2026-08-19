import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  type BillingSettings,
  type ControlAccount,
  type ControlRechargeOrder,
  type ControlReconciliation,
  type ControlSettings,
  type ControlWalletTransaction,
  downloadControlRechargeOrdersCsv,
  downloadControlWalletTransactionsCsv,
  getControlAccounts,
  getControlRechargeOrders,
  getControlReconciliation,
  getControlSettings,
  getControlWalletTransactions,
  syncControlRechargeOrder,
  updateControlBillingSettings,
  updateControlZPaySettings,
} from "./api";

type AdminTab = "accounts" | "orders" | "settings";

const tabs: Array<{ id: AdminTab; label: string }> = [
  { id: "accounts", label: "账号与钱包" },
  { id: "orders", label: "充值订单" },
  { id: "settings", label: "支付与价格" },
];

export function AdminApp() {
  const [activeTab, setActiveTab] = useState<AdminTab>("accounts");
  const [accounts, setAccounts] = useState<ControlAccount[]>([]);
  const [orders, setOrders] = useState<ControlRechargeOrder[]>([]);
  const [transactions, setTransactions] = useState<ControlWalletTransaction[]>(
    [],
  );
  const [reconciliation, setReconciliation] =
    useState<ControlReconciliation | null>(null);
  const [settings, setSettings] = useState<ControlSettings | null>(null);
  const [zpayPid, setZpayPid] = useState("");
  const [zpayKey, setZpayKey] = useState("");
  const [channels, setChannels] = useState<Array<"alipay" | "wxpay">>([
    "alipay",
    "wxpay",
  ]);
  const [billing, setBilling] = useState<BillingSettings | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const loadAccounts = useCallback(async () => {
    setError("");
    try {
      const [accountPage, transactionPage] = await Promise.all([
        getControlAccounts(),
        getControlWalletTransactions(),
      ]);
      setAccounts(accountPage.items);
      setTransactions(transactionPage.items);
    } catch (cause) {
      setError(errorMessage(cause, "读取账号与钱包失败。"));
    }
  }, []);

  const loadOrders = useCallback(async () => {
    setError("");
    try {
      const [orderPage, nextReconciliation] = await Promise.all([
        getControlRechargeOrders(),
        getControlReconciliation(),
      ]);
      setOrders(orderPage.items);
      setReconciliation(nextReconciliation);
    } catch (cause) {
      setError(errorMessage(cause, "读取充值订单失败。"));
    }
  }, []);

  const loadSettings = useCallback(async () => {
    setError("");
    try {
      const nextSettings = await getControlSettings();
      setSettings(nextSettings);
      setBilling(nextSettings.billing);
      setZpayPid(nextSettings.zpay.config.pid ?? "");
      setZpayKey("");
      setChannels(parseChannels(nextSettings.zpay.config.enabled_channels));
    } catch (cause) {
      setError(errorMessage(cause, "读取支付与价格失败。"));
    }
  }, []);

  useEffect(() => {
    if (activeTab !== "accounts") {
      return;
    }
    void loadAccounts();
  }, [activeTab, loadAccounts]);

  useEffect(() => {
    if (activeTab !== "orders") {
      return;
    }
    void loadOrders();
  }, [activeTab, loadOrders]);

  useEffect(() => {
    if (activeTab !== "settings") {
      return;
    }
    void loadSettings();
  }, [activeTab, loadSettings]);

  async function syncOrder(orderNo: string) {
    setNotice("");
    setError("");
    try {
      await syncControlRechargeOrder(orderNo);
      setNotice("订单状态已同步。");
      await loadOrders();
    } catch (cause) {
      setError(errorMessage(cause, "同步订单失败。"));
    }
  }

  async function exportRechargeOrders() {
    setError("");
    try {
      await downloadControlRechargeOrdersCsv();
    } catch (cause) {
      setError(errorMessage(cause, "导出充值订单失败。"));
    }
  }

  async function exportWalletTransactions() {
    setError("");
    try {
      await downloadControlWalletTransactionsCsv();
    } catch (cause) {
      setError(errorMessage(cause, "导出账务流水失败。"));
    }
  }

  async function saveZPay(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice("");
    setError("");
    try {
      await updateControlZPaySettings({
        pid: zpayPid,
        key: zpayKey,
        enabled_channels: channels,
      });
      setNotice("ZPay 设置已保存。");
      await loadSettings();
    } catch (cause) {
      setError(errorMessage(cause, "保存 ZPay 设置失败。"));
    }
  }

  async function saveBilling(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!billing) {
      return;
    }
    setNotice("");
    setError("");
    try {
      const nextBilling = await updateControlBillingSettings({
        internal_base_unit_price_fen: billing.internal_base_unit_price_fen,
        min_recharge_fen: billing.min_recharge_fen,
        recharge_step_fen: billing.recharge_step_fen,
      });
      setBilling(nextBilling);
      setNotice("内部价格已保存。");
    } catch (cause) {
      setError(errorMessage(cause, "保存内部价格失败。"));
    }
  }

  function toggleChannel(channel: "alipay" | "wxpay") {
    setChannels((current) =>
      current.includes(channel)
        ? current.filter((item) => item !== channel)
        : [...current, channel],
    );
  }

  return (
    <main className="admin-shell">
      <header className="admin-header">
        <div>
          <span className="eyebrow">INTERNAL CONTROL</span>
          <h1>内部运营管理</h1>
        </div>
      </header>

      <nav className="admin-tabs" aria-label="管理端导航">
        {tabs.map((tab) => (
          <button
            aria-current={activeTab === tab.id ? "page" : undefined}
            className={
              activeTab === tab.id ? "admin-tab is-active" : "admin-tab"
            }
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

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

      {activeTab === "accounts" ? (
        <section className="admin-panel" aria-label="账号与钱包">
          <h2>账号钱包</h2>
          <div className="table-scroll">
            <table className="internal-table">
              <thead>
                <tr>
                  <th>账号</th>
                  <th>姓名</th>
                  <th>角色</th>
                  <th>可用</th>
                  <th>冻结</th>
                  <th>令牌</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => (
                  <tr key={account.id}>
                    <td>{account.username}</td>
                    <td>{account.display_name}</td>
                    <td>{roleLabel(account.role)}</td>
                    <td>{account.available_credits}</td>
                    <td>{account.reserved_credits}</td>
                    <td>{account.active_token_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h2>最近账务流水</h2>
          <div className="table-scroll">
            <table className="internal-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>账号</th>
                  <th>类型</th>
                  <th>可用变动</th>
                  <th>冻结变动</th>
                </tr>
              </thead>
              <tbody>
                {transactions.map((tx) => (
                  <tr key={tx.id}>
                    <td>{tx.id}</td>
                    <td>{tx.username}</td>
                    <td>{transactionLabel(tx.type)}</td>
                    <td>{tx.available_delta}</td>
                    <td>{tx.reserved_delta}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {activeTab === "orders" ? (
        <section className="admin-panel" aria-label="充值订单">
          <div className="admin-actions">
            <button type="button" onClick={() => void exportRechargeOrders()}>
              导出充值订单 CSV
            </button>
            <button
              type="button"
              onClick={() => void exportWalletTransactions()}
            >
              导出账务流水 CSV
            </button>
          </div>
          {reconciliation ? (
            <div className="admin-metrics">
              <span>钱包数 {reconciliation.wallet_count}</span>
              <span>待支付订单 {reconciliation.pending_order_count}</span>
              <span>钱包不一致 {reconciliation.wallet_mismatch_count}</span>
              <span>
                已支付未入账 {reconciliation.paid_order_without_charge_count}
              </span>
              <span>
                入账但订单未支付{" "}
                {reconciliation.charge_without_paid_order_count}
              </span>
            </div>
          ) : null}
          <div className="table-scroll">
            <table className="internal-table">
              <thead>
                <tr>
                  <th>订单号</th>
                  <th>账号</th>
                  <th>金额</th>
                  <th>条数</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((order) => (
                  <tr key={order.id}>
                    <td>{order.order_no}</td>
                    <td>{order.username}</td>
                    <td>{formatFen(order.amount_fen)}</td>
                    <td>{order.credits}</td>
                    <td>{orderStatusLabel(order.status)}</td>
                    <td>
                      {order.status === "PENDING" ? (
                        <button
                          type="button"
                          onClick={() => void syncOrder(order.order_no)}
                        >
                          同步 {order.order_no}
                        </button>
                      ) : (
                        "只读"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {activeTab === "settings" ? (
        <section className="admin-panel" aria-label="支付与价格">
          <form className="admin-form" onSubmit={saveZPay}>
            <h2>ZPay</h2>
            <label>
              ZPay 商户 PID
              <input
                value={zpayPid}
                onChange={(event) => setZpayPid(event.target.value)}
              />
            </label>
            <div className="admin-readonly-field">
              已保存密钥
              <span className="readonly-value">
                {settings?.zpay.config.key || "未配置"}
              </span>
            </div>
            <label>
              新商户密钥
              <input
                autoComplete="new-password"
                placeholder="留空则保留当前密钥"
                type="password"
                value={zpayKey}
                onChange={(event) => setZpayKey(event.target.value)}
              />
            </label>
            <fieldset className="admin-checks">
              <legend>支付渠道</legend>
              <label>
                <input
                  checked={channels.includes("alipay")}
                  type="checkbox"
                  onChange={() => toggleChannel("alipay")}
                />
                支付宝
              </label>
              <label>
                <input
                  checked={channels.includes("wxpay")}
                  type="checkbox"
                  onChange={() => toggleChannel("wxpay")}
                />
                微信
              </label>
            </fieldset>
            <label>
              网关地址
              <input readOnly value={settings?.deployment.gateway_url ?? ""} />
            </label>
            <label>
              异步回调地址
              <input readOnly value={settings?.deployment.notify_url ?? ""} />
            </label>
            <label>
              同步返回地址
              <input readOnly value={settings?.deployment.return_url ?? ""} />
            </label>
            <button type="submit">保存 ZPay 设置</button>
          </form>

          <form className="admin-form" onSubmit={saveBilling}>
            <h2>内部价格</h2>
            <label>
              内部单价（分/条）
              <input
                inputMode="numeric"
                type="number"
                value={billing?.internal_base_unit_price_fen ?? 0}
                onChange={(event) =>
                  updateBilling(
                    "internal_base_unit_price_fen",
                    event.target.value,
                  )
                }
              />
            </label>
            <label>
              最低充值（分）
              <input
                inputMode="numeric"
                type="number"
                value={billing?.min_recharge_fen ?? 0}
                onChange={(event) =>
                  updateBilling("min_recharge_fen", event.target.value)
                }
              />
            </label>
            <label>
              递增步长（分）
              <input
                inputMode="numeric"
                type="number"
                value={billing?.recharge_step_fen ?? 0}
                onChange={(event) =>
                  updateBilling("recharge_step_fen", event.target.value)
                }
              />
            </label>
            <button type="submit">保存内部价格</button>
          </form>
        </section>
      ) : null}
    </main>
  );

  function updateBilling(field: keyof BillingSettings, value: string) {
    setBilling((current) =>
      current ? { ...current, [field]: Number(value) } : current,
    );
  }
}

function parseChannels(value: unknown): Array<"alipay" | "wxpay"> {
  if (typeof value !== "string") {
    return ["alipay"];
  }
  const next = value
    .split(",")
    .filter(
      (item): item is "alipay" | "wxpay" =>
        item === "alipay" || item === "wxpay",
    );
  return next.length ? next : ["alipay"];
}

function formatFen(value: number): string {
  return `${Math.floor(value / 100)}元`;
}

function roleLabel(role: string): string {
  return role === "admin"
    ? "管理员"
    : role === "auditor"
      ? "审计员"
      : "普通员工";
}

function transactionLabel(type: string): string {
  return (
    {
      CHARGE: "充值到账",
      RESERVE: "冻结",
      SETTLE: "结算",
      RELEASE: "释放",
    }[type] ?? type
  );
}

function orderStatusLabel(status: string): string {
  return (
    {
      PENDING: "待支付",
      PAID: "已支付",
      FAILED: "失败",
      CLOSED: "已关闭",
    }[status] ?? status
  );
}

function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message.trim()
    ? cause.message
    : fallback;
}
