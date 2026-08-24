import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { clearAdminActivationSession } from "../api.admin";
import { AdminActivationSection } from "./AdminActivationSection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
};

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: "csrf-token-1",
  actor: adminActor,
};

const emptyCodesPage = { items: [], limit: 50, offset: 0 };

function installFetch(options?: {
  session?: "valid" | "missing";
  exchange?: "valid" | "invalid";
  exchangeRole?: string;
}) {
  const sessionState = options?.session ?? "missing";
  const exchangeState = options?.exchange ?? "valid";
  const exchangeRole = options?.exchangeRole ?? "admin";
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/admin/session/exchange")) {
      if (exchangeState === "invalid") {
        return jsonResponse(
          {
            detail: {
              code: "EXCHANGE_CREDENTIAL_INVALID",
              message: "Exchange credential is invalid or expired.",
            },
          },
          401,
        );
      }
      return jsonResponse({
        ...exchangePayload,
        actor: { ...adminActor, role: exchangeRole },
      });
    }
    if (
      url.endsWith("/api/control/admin/session") &&
      (!init?.method || init.method === "GET")
    ) {
      if (sessionState === "missing") {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      return jsonResponse({
        session_id: "session-1",
        expires_at: "2026-08-23T20:00:00+00:00",
        last_activity_at: "2026-08-23T12:00:00+00:00",
        actor: adminActor,
      });
    }
    if (
      url.endsWith("/api/control/admin/session") &&
      init?.method === "DELETE"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (url.includes("/api/control/activation-codes")) {
      return jsonResponse(emptyCodesPage);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function signIn() {
  fireEvent.change(await screen.findByLabelText("管理登录凭据"), {
    target: { value: "ASX1.body.signature" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录管理端" }));
}

describe("AdminActivationSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("shows the sign-in form when there is no admin session", async () => {
    installFetch();

    render(<AdminActivationSection />);

    expect(await screen.findByLabelText("管理登录凭据")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "登录管理端" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活码批次" })).toBeNull();
  });

  it("exchanges the credential and reveals the activation pages", async () => {
    const fetchMock = installFetch();

    render(<AdminActivationSection />);
    await signIn();

    expect(await screen.findByText("管理员一号")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "激活码批次" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "激活码列表" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "激活码发放" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "退出登录" }),
    ).toBeInTheDocument();
    const exchangeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/control/admin/session/exchange"),
    );
    expect(exchangeCall?.[1]?.body).toBe(
      JSON.stringify({ credential: "ASX1.body.signature" }),
    );
  });

  it("surfaces an invalid exchange credential without leaking details", async () => {
    installFetch({ exchange: "invalid" });

    render(<AdminActivationSection />);
    await signIn();

    expect(await screen.findByText(/交换凭据无效或已过期/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "激活码批次" })).toBeNull();
  });

  it("recovers a read-only view after a refresh dropped the CSRF token", async () => {
    installFetch({ session: "valid" });

    render(<AdminActivationSection />);

    expect(await screen.findByText("管理员一号")).toBeInTheDocument();
    expect(screen.getByText(/重新登录后才能执行写操作/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "退出登录" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "激活码批次" }),
    ).toBeInTheDocument();
  });

  it("marks auditor sessions as read-only operators", async () => {
    installFetch({ exchangeRole: "auditor" });

    render(<AdminActivationSection />);
    await signIn();

    expect(await screen.findByText(/审计员只读/)).toBeInTheDocument();
  });

  it("logs out and returns to the sign-in form", async () => {
    const fetchMock = installFetch();

    render(<AdminActivationSection />);
    await signIn();
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));

    expect(await screen.findByLabelText("管理登录凭据")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/api/control/admin/session") &&
          init?.method === "DELETE",
      ),
    ).toBe(true);
  });

  it("switches between the activation sub pages", async () => {
    installFetch();

    render(<AdminActivationSection />);
    await signIn();

    fireEvent.click(await screen.findByRole("button", { name: "激活码发放" }));
    expect(
      await screen.findByRole("heading", { name: "发放激活码" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "激活码批次" }));
    expect(
      await screen.findByRole("heading", { name: "创建批次" }),
    ).toBeInTheDocument();
  });
});
