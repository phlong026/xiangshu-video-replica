import { type FormEvent, useCallback, useEffect, useState } from "react";
import { AdminActivationSection } from "./admin/AdminActivationSection";
import { AuditEventsPage } from "./admin/AuditEventsPage";
import { CustomersPage } from "./admin/CustomersPage";
import { DevicesPage } from "./admin/DevicesPage";
import { SessionsPage } from "./admin/SessionsPage";
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
  SESSION_EXPIRED_EVENT,
  syncControlRechargeOrder,
  updateControlBillingSettings,
  updateControlZPaySettings,
} from "./api";
import {
  AdminActivationError,
  type AdminActorInfo,
  adminActivationErrorMessage,
  clearAdminActivationSession,
  deleteAdminSession,
  exchangeAdminSession,
  fetchAdminSession,
  loginAdminWithPassword,
  recoverAdminPassword,
} from "./api.admin";
import jingxuLogoMark from "./assets/brand/jingxu-logo-mark.png";
import { SettingsPanel } from "./SettingsPanel";

type AuthPhase =
  | "checking"
  | "anonymous"
  | "recovery"
  | "password-setup"
  | "ready";

type AdminTab =
  | "accounts"
  | "orders"
  | "settings"
  | "services"
  | "activation"
  | "customers"
  | "devices"
  | "sessions"
  | "audit";

const tabGroups: Array<{
  id: string;
  label: string;
  tabs: Array<{ id: AdminTab; label: string; helper: string }>;
}> = [
  {
    id: "overview",
    label: "运营概览",
    tabs: [
      { id: "accounts", label: "账号与钱包", helper: "钱包、额度与流水" },
      { id: "orders", label: "充值订单", helper: "支付、对账与导出" },
    ],
  },
  {
    id: "operations",
    label: "客户运营",
    tabs: [
      { id: "activation", label: "激活码", helper: "批次、发放与状态" },
      {
        id: "customers",
        label: "客户",
        helper: "管理客户账户、授权状态、生成结果与结算消耗",
      },
      { id: "devices", label: "设备", helper: "终端在线与风控动作" },
      { id: "sessions", label: "会话", helper: "在线态与单在线约束" },
    ],
  },
  {
    id: "governance",
    label: "系统治理",
    tabs: [
      { id: "settings", label: "支付与价格", helper: "定价与渠道设置" },
      { id: "services", label: "服务配置", helper: "上游服务与运行参数" },
      { id: "audit", label: "审计", helper: "操作留痕与事件检索" },
    ],
  },
];

const tabPageTitles: Record<AdminTab, string> = {
  accounts: "账号与钱包",
  orders: "充值订单",
  settings: "支付与价格",
  services: "服务配置",
  activation: "激活码",
  customers: "客户管理",
  devices: "设备管理",
  sessions: "会话管理",
  audit: "审计日志",
};

const compactNavigationBreakpoint = 1024;

export function AdminApp() {
  const [authPhase, setAuthPhase] = useState<AuthPhase>("checking");
  const [actor, setActor] = useState<AdminActorInfo | null>(null);
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [recoveryCredential, setRecoveryCredential] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
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
  const [isCompactNavigation, setIsCompactNavigation] = useState(() =>
    typeof window !== "undefined"
      ? window.innerWidth < compactNavigationBreakpoint
      : false,
  );
  const [isNavigationOpen, setIsNavigationOpen] = useState(() =>
    typeof window !== "undefined"
      ? window.innerWidth >= compactNavigationBreakpoint
      : true,
  );

  const handleSessionExpired = useCallback(
    (message = "会话已失效，请重新登录。") => {
      clearAdminActivationSession();
      setActor(null);
      setAuthPhase("anonymous");
      setActiveTab("accounts");
      setLoginPassword("");
      setRecoveryCredential("");
      setNewPassword("");
      setConfirmPassword("");
      setIsNavigationOpen(
        typeof window !== "undefined" &&
          window.innerWidth >= compactNavigationBreakpoint,
      );
      setNotice("");
      setError(message);
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const session = await fetchAdminSession();
        if (cancelled) {
          return;
        }
        if (!session.csrf_token) {
          clearAdminActivationSession();
          setAuthPhase("anonymous");
          return;
        }
        setActor(session.actor);
        setAuthPhase("ready");
      } catch (cause) {
        if (cancelled) {
          return;
        }
        clearAdminActivationSession();
        if (!(cause instanceof AdminActivationError && cause.status === 401)) {
          setError(adminActivationErrorMessage(cause, "读取管理会话失败"));
        }
        setAuthPhase("anonymous");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onSessionExpired = () => {
      handleSessionExpired();
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    };
  }, [handleSessionExpired]);

  useEffect(() => {
    const syncNavigationMode = () => {
      const nextCompact = window.innerWidth < compactNavigationBreakpoint;
      setIsCompactNavigation(nextCompact);
      setIsNavigationOpen((current) => (nextCompact ? current : true));
    };
    syncNavigationMode();
    window.addEventListener("resize", syncNavigationMode);
    return () => window.removeEventListener("resize", syncNavigationMode);
  }, []);

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
    if (authPhase !== "ready" || activeTab !== "accounts") {
      return;
    }
    void loadAccounts();
  }, [activeTab, authPhase, loadAccounts]);

  useEffect(() => {
    if (authPhase !== "ready" || activeTab !== "orders") {
      return;
    }
    void loadOrders();
  }, [activeTab, authPhase, loadOrders]);

  useEffect(() => {
    if (
      authPhase !== "ready" ||
      (activeTab !== "settings" && activeTab !== "activation")
    ) {
      return;
    }
    void loadSettings();
  }, [activeTab, authPhase, loadSettings]);

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

  async function signInWithPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    const username = loginUsername.trim();
    if (!username || !loginPassword) {
      setError("请输入管理员账号和密码。");
      return;
    }
    try {
      const result = await loginAdminWithPassword(username, loginPassword);
      setActor(result.actor);
      setLoginPassword("");
      setAuthPhase("ready");
      setActiveTab("accounts");
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "后台登录失败"));
    }
  }

  async function verifyRecoveryCredential(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    const credential = recoveryCredential.trim();
    if (!credential) {
      setError("请输入一次性恢复凭据。");
      return;
    }
    try {
      const result = await exchangeAdminSession(credential);
      setActor(result.actor);
      setLoginUsername(result.actor.username);
      setRecoveryCredential("");
      setAuthPhase("password-setup");
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "恢复凭据验证失败"));
    }
  }

  async function saveRecoveredPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (newPassword.length < 12 || newPassword.length > 128) {
      setError("新密码需为 12 至 128 个字符。");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致。");
      return;
    }
    try {
      await recoverAdminPassword(newPassword);
      setActor(null);
      setNewPassword("");
      setConfirmPassword("");
      setAuthPhase("anonymous");
      setNotice("密码已设置，请使用管理员账号和新密码登录。");
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "设置管理员密码失败"));
    }
  }

  const signOut = useCallback(async () => {
    try {
      await deleteAdminSession();
    } catch {
      // A revoked or expired server session is already logged out in practice.
    } finally {
      handleSessionExpired("");
    }
  }, [handleSessionExpired]);

  function toggleChannel(channel: "alipay" | "wxpay") {
    setChannels((current) =>
      current.includes(channel)
        ? current.filter((item) => item !== channel)
        : [...current, channel],
    );
  }

  if (authPhase !== "ready" || !actor) {
    return (
      <main className="admin-shell">
        <header className="admin-header">
          <div>
            <span className="eyebrow">CONTROL CENTER</span>
            <h1>运营管理后台</h1>
          </div>
        </header>

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

        {authPhase === "checking" ? (
          <section className="admin-panel" aria-label="后台登录">
            <p>正在检查登录状态…</p>
          </section>
        ) : null}

        {authPhase === "anonymous" ? (
          <section className="admin-panel" aria-label="后台登录">
            <h2>管理员登录</h2>
            <form className="admin-form" onSubmit={signInWithPassword}>
              <label>
                管理员账号
                <input
                  autoComplete="username"
                  value={loginUsername}
                  onChange={(event) => setLoginUsername(event.target.value)}
                />
              </label>
              <label>
                管理员密码
                <input
                  autoComplete="current-password"
                  type="password"
                  value={loginPassword}
                  onChange={(event) => setLoginPassword(event.target.value)}
                />
              </label>
              <button type="submit">登录后台</button>
            </form>
            <button
              type="button"
              onClick={() => {
                setError("");
                setNotice("");
                setAuthPhase("recovery");
              }}
            >
              首次设置或找回密码
            </button>
          </section>
        ) : null}

        {authPhase === "recovery" ? (
          <section className="admin-panel" aria-label="管理员密码恢复">
            <h2>首次设置或找回密码</h2>
            <p className="admin-hint">
              一次性恢复凭据只用于验证身份和设置新密码，不能作为日常登录方式。
            </p>
            <form className="admin-form" onSubmit={verifyRecoveryCredential}>
              <label>
                一次性恢复凭据
                <input
                  autoComplete="off"
                  placeholder="ASX1.…"
                  type="password"
                  value={recoveryCredential}
                  onChange={(event) =>
                    setRecoveryCredential(event.target.value)
                  }
                />
              </label>
              <button type="submit">验证恢复凭据</button>
            </form>
            <button type="button" onClick={() => setAuthPhase("anonymous")}>
              返回账号密码登录
            </button>
          </section>
        ) : null}

        {authPhase === "password-setup" ? (
          <section className="admin-panel" aria-label="设置管理员密码">
            <h2>设置管理员密码</h2>
            <p className="admin-hint">
              当前账号：{actor?.username}。保存后，所有旧会话都会失效。
            </p>
            <form className="admin-form" onSubmit={saveRecoveredPassword}>
              <label>
                新管理员密码
                <input
                  autoComplete="new-password"
                  type="password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                />
              </label>
              <label>
                确认新管理员密码
                <input
                  autoComplete="new-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                />
              </label>
              <button type="submit">保存新密码</button>
            </form>
          </section>
        ) : null}
      </main>
    );
  }

  const activeTabMeta = tabGroups
    .flatMap((group) => group.tabs)
    .find((tab) => tab.id === activeTab);
  const activeGroupLabel =
    tabGroups.find((group) => group.tabs.some((tab) => tab.id === activeTab))
      ?.label ?? "运营后台";
  const activePageTitle = tabPageTitles[activeTab];

  return (
    <main className="admin-shell admin-shell--control">
      <header className="admin-header admin-header--workspace">
        <div className="admin-title-block">
          {isCompactNavigation ? (
            <button
              aria-controls="admin-navigation"
              aria-expanded={isNavigationOpen}
              aria-label={isNavigationOpen ? "关闭导航" : "展开导航"}
              className="admin-nav-toggle"
              type="button"
              onClick={() => setIsNavigationOpen((current) => !current)}
            >
              菜单
            </button>
          ) : null}
          <span className="admin-breadcrumb">
            {activeGroupLabel} / {activePageTitle}
          </span>
        </div>
        <div className="admin-session">
          <time dateTime={new Date().toISOString().slice(0, 10)}>
            {new Date().toLocaleDateString("zh-CN")}
          </time>
          <div className="admin-session__identity">
            <span>{actor.display_name}</span>
            <span>{roleLabel(actor.role)}</span>
          </div>
          <button type="button" onClick={() => void signOut()}>
            退出登录
          </button>
        </div>
      </header>

      <section className="admin-stage">
        {isCompactNavigation && isNavigationOpen ? (
          <button
            aria-label="关闭导航遮罩"
            className="admin-sidebar-backdrop"
            type="button"
            onClick={() => setIsNavigationOpen(false)}
          />
        ) : null}
        <aside
          aria-hidden={isCompactNavigation && !isNavigationOpen}
          className={
            isCompactNavigation && isNavigationOpen
              ? "admin-sidebar is-open"
              : "admin-sidebar"
          }
        >
          <div className="admin-sidebar__top">
            <div className="admin-brand">
              <img alt="" aria-hidden="true" src={jingxuLogoMark} />
              <div>
                <strong>镜序 Studio</strong>
                <span>OPERATIONS</span>
              </div>
            </div>
            <div className="admin-sidebar__context">
              <p className="admin-sidebar__label">当前模块</p>
              <h2>{activePageTitle}</h2>
              <p className="admin-sidebar__helper">
                {activeTabMeta?.helper ?? "运营核心视图"}
              </p>
            </div>
          </div>

          {isNavigationOpen ? (
            <nav
              aria-label="管理端导航"
              className="admin-nav-groups"
              id="admin-navigation"
            >
              {tabGroups.map((group) => (
                <section className="admin-nav-group" key={group.id}>
                  <h3>{group.label}</h3>
                  <div className="admin-nav-group__items">
                    {group.tabs.map((tab) => (
                      <button
                        aria-current={activeTab === tab.id ? "page" : undefined}
                        aria-label={tab.label}
                        className={
                          activeTab === tab.id
                            ? "admin-tab is-active"
                            : "admin-tab"
                        }
                        key={tab.id}
                        type="button"
                        onClick={() => {
                          setActiveTab(tab.id);
                          if (isCompactNavigation) {
                            setIsNavigationOpen(false);
                          }
                        }}
                      >
                        <span>{tab.label}</span>
                        <small>{tab.helper}</small>
                      </button>
                    ))}
                  </div>
                </section>
              ))}
            </nav>
          ) : null}
          <div className="admin-sidebar__footer">
            <span>{actor.display_name}</span>
            <small>{roleLabel(actor.role)} · 安全会话</small>
          </div>
        </aside>

        <div className="admin-workspace">
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

          <header className="admin-page-heading">
            <span>{activeGroupLabel}</span>
            <h1>{activePageTitle}</h1>
            <p>{activeTabMeta?.helper ?? "运营核心视图"}</p>
          </header>

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
                <button
                  type="button"
                  onClick={() => void exportRechargeOrders()}
                >
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
                    已支付未入账{" "}
                    {reconciliation.paid_order_without_charge_count}
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
                          {order.status === "PENDING" &&
                          actor.role !== "auditor" ? (
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
                    disabled={actor.role === "auditor"}
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
                    disabled={actor.role === "auditor"}
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
                      disabled={actor.role === "auditor"}
                      type="checkbox"
                      onChange={() => toggleChannel("alipay")}
                    />
                    支付宝
                  </label>
                  <label>
                    <input
                      checked={channels.includes("wxpay")}
                      disabled={actor.role === "auditor"}
                      type="checkbox"
                      onChange={() => toggleChannel("wxpay")}
                    />
                    微信
                  </label>
                </fieldset>
                <p className="admin-hint">
                  支付接口地址由系统自动配置，无需填写。
                </p>
                <button disabled={actor.role === "auditor"} type="submit">
                  保存 ZPay 设置
                </button>
              </form>

              <form className="admin-form" onSubmit={saveBilling}>
                <h2>内部价格</h2>
                <label>
                  内部单价（分/条）
                  <input
                    disabled={actor.role === "auditor"}
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
                    disabled={actor.role === "auditor"}
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
                    disabled={actor.role === "auditor"}
                    inputMode="numeric"
                    type="number"
                    value={billing?.recharge_step_fen ?? 0}
                    onChange={(event) =>
                      updateBilling("recharge_step_fen", event.target.value)
                    }
                  />
                </label>
                <button disabled={actor.role === "auditor"} type="submit">
                  保存内部价格
                </button>
              </form>
            </section>
          ) : null}
          {activeTab === "activation" ? (
            <AdminActivationSection
              actor={actor}
              unitPriceFen={billing?.internal_base_unit_price_fen ?? null}
              onSessionExpired={handleSessionExpired}
            />
          ) : null}
          {activeTab === "services" ? (
            <section className="admin-panel" aria-label="服务配置">
              <SettingsPanel
                readOnly={actor.role === "auditor"}
                source="control"
              />
            </section>
          ) : null}
          {activeTab === "customers" ? (
            <CustomersPage
              embedded
              readOnly={actor.role === "auditor"}
              onOpenDevices={() => setActiveTab("devices")}
            />
          ) : null}
          {activeTab === "devices" ? (
            <DevicesPage readOnly={actor.role === "auditor"} />
          ) : null}
          {activeTab === "sessions" ? (
            <SessionsPage readOnly={actor.role === "auditor"} />
          ) : null}
          {activeTab === "audit" ? <AuditEventsPage /> : null}
        </div>
      </section>
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
