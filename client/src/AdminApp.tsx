import { type FormEvent, useCallback, useEffect, useState } from "react";
import { AccountsPage } from "./admin/AccountsPage";
import { AdminActivationSection } from "./admin/AdminActivationSection";
import { AuditEventsPage } from "./admin/AuditEventsPage";
import { CustomersPage } from "./admin/CustomersPage";
import { DevicesPage } from "./admin/DevicesPage";
import { GenerationRecordsPage } from "./admin/GenerationRecordsPage";
import { OrdersPage } from "./admin/OrdersPage";
import { PaymentSettingsSection } from "./admin/PaymentSettingsSection";
import { QueueModeSection } from "./admin/QueueModeSection";
import { SessionsPage } from "./admin/SessionsPage";
import { PageBanner } from "./admin/ui/PageBanner";
import { roleLabel } from "./admin/ui/vocabulary";
import { SESSION_EXPIRED_EVENT } from "./api";
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
  | "devices"
  | "customers"
  | "generationRecords"
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
      { id: "accounts", label: "账号与钱包", helper: "钱包、条数与流水" },
      { id: "orders", label: "充值订单", helper: "支付、查单、对账与导出" },
    ],
  },
  {
    id: "operations",
    label: "客户运营",
    tabs: [
      {
        id: "activation",
        label: "激活码与发放",
        helper: "生成、发放、暂停恢复与撤销",
      },
      { id: "devices", label: "设备", helper: "绑定状态与强制下线" },
      {
        id: "customers",
        label: "客户",
        helper: "客户账户、售价、免费条数与调账",
      },
      {
        id: "generationRecords",
        label: "生成记录",
        helper: "视频、图片与 AI 评分费用追溯",
      },
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
  activation: "激活码与发放",
  devices: "设备管理",
  customers: "客户管理",
  generationRecords: "用户生成记录",
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
  const [sessionUserId, setSessionUserId] = useState<string | undefined>(
    undefined,
  );
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

  if (authPhase !== "ready" || !actor) {
    return (
      <main className="admin-shell">
        <header className="admin-header">
          <div>
            <span className="eyebrow">CONTROL CENTER</span>
            <h1>运营管理后台</h1>
          </div>
        </header>

        {error ? <PageBanner tone="error">{error}</PageBanner> : null}
        {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

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
  const readOnly = actor.role === "auditor";

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
                          // C3：切标签清掉上一页残留的全局提示。
                          setError("");
                          setNotice("");
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
          {error ? <PageBanner tone="error">{error}</PageBanner> : null}
          {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

          <header className="admin-page-heading">
            <span>{activeGroupLabel}</span>
            <h1>{activePageTitle}</h1>
            <p>{activeTabMeta?.helper ?? "运营核心视图"}</p>
          </header>

          {activeTab === "accounts" ? <AccountsPage /> : null}
          {activeTab === "orders" ? <OrdersPage readOnly={readOnly} /> : null}
          {activeTab === "settings" ? (
            <PaymentSettingsSection readOnly={readOnly} />
          ) : null}
          {activeTab === "services" ? (
            <>
              <QueueModeSection readOnly={readOnly} />
              <section className="admin-panel" aria-label="服务配置">
                <SettingsPanel readOnly={readOnly} source="control" />
              </section>
            </>
          ) : null}
          {activeTab === "activation" ? (
            <AdminActivationSection
              actor={actor}
              onSessionExpired={handleSessionExpired}
            />
          ) : null}
          {activeTab === "devices" ? <DevicesPage readOnly={readOnly} /> : null}
          {activeTab === "customers" ? (
            <CustomersPage
              embedded
              readOnly={readOnly}
              onOpenDevices={() => setActiveTab("devices")}
              onOpenSessions={(userId) => {
                setSessionUserId(userId);
                setActiveTab("sessions");
              }}
            />
          ) : null}
          {activeTab === "generationRecords" ? <GenerationRecordsPage /> : null}
          {activeTab === "sessions" ? (
            <SessionsPage readOnly={readOnly} userId={sessionUserId} />
          ) : null}
          {activeTab === "audit" ? <AuditEventsPage /> : null}
        </div>
      </section>
    </main>
  );
}
