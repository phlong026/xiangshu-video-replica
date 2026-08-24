import { useMemo } from "react";
import { AdminApp } from "./AdminApp";
import { App, WorkspaceShell } from "./App";
import type { CurrentUser } from "./api";
import { ActivationPage } from "./customer/ActivationPage";
import { LoginPage } from "./customer/LoginPage";
import {
  type CustomerWorkspaceUser,
  customerCredentialStore,
  useCustomerSession,
} from "./customer/useCustomerSession";

export function RootApp({
  path = window.location.pathname,
}: {
  path?: string;
}) {
  if (path === "/customer" || path.startsWith("/customer/")) {
    return <CustomerShell />;
  }
  return path === "/admin" || path.startsWith("/admin/") ? (
    <AdminApp />
  ) : (
    <App />
  );
}

/** The customer entry (FE-02): the seven-screen customer state machine from
 * dev doc §4.1. It never renders the internal login shell, so the internal
 * access-token input is structurally not a customer entry. The workspace
 * screen reuses the shared shell under the customer identity. */
function CustomerShell() {
  // A stable store identity for the whole mount: the in-memory browser store
  // keeps its credentials in closures, so a per-render store would lose them
  // (and every lifecycle listener would re-mount on each render).
  const store = useMemo(customerCredentialStore, []);
  const session = useCustomerSession(store);

  switch (session.screen) {
    case "checking":
      return (
        <main className="centered-shell">
          <section className="login-card" aria-live="polite">
            <span className="eyebrow">JINGXU STUDIO</span>
            <p className="login-hint">正在检查本机登录状态…</p>
          </section>
        </main>
      );
    case "activation":
      return (
        <ActivationPage
          onActivate={(input) => void session.activate(input)}
          isBusy={session.isBusy}
          error={session.error}
        />
      );
    case "login":
      return (
        <LoginPage
          onRetryLogin={() => void session.retryLogin()}
          isBusy={session.isBusy}
          error={session.error}
          conflict={session.conflict}
        />
      );
    case "workspace":
      // The workspace screen is only reachable after activate/login set the
      // identity; the checking fallback below is unreachable in practice.
      return session.user === null ? null : (
        <WorkspaceShell currentUser={customerToCurrentUser(session.user)} />
      );
    case "session-expired":
      return (
        <CustomerTerminalScreen
          title="登录已过期"
          description="会话已过期，请重新登录。"
          actionLabel="重新登录"
          onAction={session.restartAfterExpiry}
        />
      );
    case "session-replaced":
      return (
        <CustomerTerminalScreen
          title="本设备已下线"
          description="您的账号已在另一台设备上登录，本设备会话已被切换下线。"
          actionLabel="重新登录"
          onAction={session.restartAfterExpiry}
        />
      );
    case "device-revoked":
      return (
        <CustomerTerminalScreen
          title="设备已被解绑"
          description="本设备已被解绑，请重新激活后使用。"
          actionLabel="重新激活"
          onAction={session.restartAfterRevocation}
        />
      );
  }
}

/** A terminal screen (§4.2): displaced/expired/revoked sessions are reported
 * as exactly what they are — never as a balance, network, or generic service
 * failure — with the single recovery action the state machine allows. */
function CustomerTerminalScreen({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel: string;
  onAction(): void;
}) {
  return (
    <main className="centered-shell">
      <section className="login-card" aria-labelledby="customer-terminal-title">
        <span className="eyebrow">JINGXU STUDIO</span>
        <h1 id="customer-terminal-title">{title}</h1>
        <p className="login-hint">{description}</p>
        <button type="button" onClick={onAction}>
          {actionLabel}
        </button>
      </section>
    </main>
  );
}

function customerToCurrentUser(user: CustomerWorkspaceUser): CurrentUser {
  return {
    id: user.userId,
    username: user.username ?? "customer",
    display_name: user.username ?? "客户",
    // The customer lane maps to the standard employee permissions: full
    // workspace access, no internal settings page.
    role: "employee",
  };
}
