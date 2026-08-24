import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  AdminActivationError,
  type AdminActorInfo,
  adminActivationErrorMessage,
  clearAdminActivationSession,
  deleteAdminSession,
  exchangeAdminSession,
  fetchAdminSession,
} from "../api.admin";
import { ActivationCodeBatchesPage } from "./ActivationCodeBatchesPage";
import { ActivationCodesPage } from "./ActivationCodesPage";
import { DeliveriesPage } from "./DeliveriesPage";

type SectionPhase = "checking" | "anonymous" | "readonly" | "ready";
type SubPage = "batches" | "codes" | "deliveries";

const subPages: Array<{ id: SubPage; label: string }> = [
  { id: "batches", label: "激活码批次" },
  { id: "codes", label: "激活码列表" },
  { id: "deliveries", label: "激活码发放" },
];

/**
 * T32 — the activation-code management section of the admin shell.
 *
 * Owns the T09 admin session lifecycle for the activation pages: the operator
 * exchanges a one-time credential for an HttpOnly cookie plus a CSRF token.
 * The CSRF token lives in adapter memory only (ADM-01 No-Go: no browser
 * persistence), so after a refresh the cookie still allows read-only access
 * while writes require a fresh sign-in.
 */
export function AdminActivationSection() {
  const [phase, setPhase] = useState<SectionPhase>("checking");
  const [actor, setActor] = useState<AdminActorInfo | null>(null);
  const [credential, setCredential] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [activePage, setActivePage] = useState<SubPage>("batches");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const session = await fetchAdminSession();
        if (!cancelled) {
          setActor(session.actor);
          setPhase("readonly");
        }
      } catch (cause) {
        if (cancelled) {
          return;
        }
        if (cause instanceof AdminActivationError && cause.status === 401) {
          setPhase("anonymous");
          return;
        }
        setError(adminActivationErrorMessage(cause, "读取管理会话失败"));
        setPhase("anonymous");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const submitCredential = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setError("");
      setNotice("");
      const trimmed = credential.trim();
      if (!trimmed) {
        setError("请填写管理登录凭据");
        return;
      }
      try {
        const result = await exchangeAdminSession(trimmed);
        setActor(result.actor);
        setCredential("");
        setPhase("ready");
      } catch (cause) {
        setError(adminActivationErrorMessage(cause, "管理登录失败"));
      }
    },
    [credential],
  );

  const signOut = useCallback(async () => {
    setError("");
    setNotice("");
    try {
      await deleteAdminSession();
    } catch {
      // A dead session logs out client-side all the same.
    } finally {
      clearAdminActivationSession();
      setActor(null);
      setPhase("anonymous");
      setActivePage("batches");
    }
  }, []);

  const handleSessionExpired = useCallback(() => {
    clearAdminActivationSession();
    setActor(null);
    setPhase("anonymous");
    setActivePage("batches");
    setError("会话已失效，请重新登录");
  }, []);

  if (phase === "checking") {
    return (
      <section className="admin-panel" aria-label="激活码管理">
        <p>正在检查管理会话…</p>
      </section>
    );
  }

  if (phase === "anonymous") {
    return (
      <section className="admin-panel" aria-label="管理端登录">
        <h2>管理端登录</h2>
        <p className="admin-hint">
          使用一次性管理登录凭据进入激活码管理；凭据由运维签发，登录后以
          HttpOnly Cookie 保持会话。
        </p>
        <form className="admin-form" onSubmit={submitCredential}>
          <label>
            管理登录凭据
            <input
              autoComplete="off"
              placeholder="ASX1.…"
              type="password"
              value={credential}
              onChange={(event) => setCredential(event.target.value)}
            />
          </label>
          <button type="submit">登录管理端</button>
        </form>
        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
      </section>
    );
  }

  const readOnly = phase === "readonly" || actor?.role === "auditor";

  return (
    <section className="admin-panel" aria-label="激活码管理">
      <div className="admin-session">
        <span>{actor?.display_name}</span>
        <span>{roleLabel(actor?.role ?? "")}</span>
        {phase === "ready" ? (
          <button type="button" onClick={() => void signOut()}>
            退出登录
          </button>
        ) : (
          <button type="button" onClick={() => setPhase("anonymous")}>
            重新登录
          </button>
        )}
      </div>

      {phase === "readonly" ? (
        <p className="wallet-notice" role="status">
          会话令牌已随页面刷新丢失，重新登录后才能执行写操作。
        </p>
      ) : null}
      {actor?.role === "auditor" ? (
        <p className="wallet-notice" role="status">
          审计员只读：仅可查看，不能执行写操作。
        </p>
      ) : null}
      {notice ? (
        <p className="wallet-notice" role="status">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <nav className="admin-tabs" aria-label="激活码管理导航">
        {subPages.map((page) => (
          <button
            aria-current={activePage === page.id ? "page" : undefined}
            className={
              activePage === page.id ? "admin-tab is-active" : "admin-tab"
            }
            key={page.id}
            type="button"
            onClick={() => setActivePage(page.id)}
          >
            {page.label}
          </button>
        ))}
      </nav>

      {activePage === "batches" ? (
        <ActivationCodeBatchesPage
          readOnly={readOnly}
          onSessionExpired={handleSessionExpired}
        />
      ) : null}
      {activePage === "codes" ? (
        <ActivationCodesPage
          readOnly={readOnly}
          onSessionExpired={handleSessionExpired}
        />
      ) : null}
      {activePage === "deliveries" ? (
        <DeliveriesPage
          readOnly={readOnly}
          onSessionExpired={handleSessionExpired}
        />
      ) : null}
    </section>
  );
}

function roleLabel(role: string): string {
  return role === "admin" ? "管理员" : role === "auditor" ? "审计员" : role;
}
