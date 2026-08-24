import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearAdminActivationSession,
  exchangeAdminSession,
} from "../api.admin";
import { ActivationCodesPage } from "./ActivationCodesPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: "csrf-token-1",
  actor: {
    user_id: "admin-1",
    username: "admin",
    display_name: "管理员一号",
    role: "admin",
  },
};

const codesPage = {
  items: [
    {
      code_id: "code-1",
      batch_id: "batch-1",
      masked_code: "XS****01",
      status: "GENERATED",
      bound_user_id: null,
      issued_at: null,
    },
    {
      code_id: "code-2",
      batch_id: "batch-1",
      masked_code: "XS****02",
      status: "ACTIVE",
      bound_user_id: "user-9",
      issued_at: "2026-08-20T10:00:00+00:00",
    },
  ],
  limit: 50,
  offset: 0,
};

function installFetch(options?: {
  list?: "ok" | "unauthorized";
  suspend?: "ok" | "invalid-transition";
}) {
  const listState = options?.list ?? "ok";
  const suspendState = options?.suspend ?? "ok";
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(exchangePayload);
    }
    if (url.includes("/api/control/activation-codes?")) {
      if (listState === "unauthorized") {
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
      return jsonResponse(codesPage);
    }
    if (url.endsWith("/suspend") && init?.method === "POST") {
      if (suspendState === "invalid-transition") {
        return jsonResponse(
          {
            detail: {
              code: "CODE_TRANSITION_INVALID",
              message:
                "The activation code cannot move from REVOKED to SUSPENDED.",
            },
          },
          409,
        );
      }
      return jsonResponse({
        code_id: "code-2",
        status: "SUSPENDED",
        request_id: "req-suspend-1",
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ActivationCodesPage", () => {
  beforeEach(async () => {
    installFetch();
    await exchangeAdminSession("ASX1.body.signature");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("loads the code list on mount with masked codes only", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodesPage />);

    expect(await screen.findByText("XS****01")).toBeInTheDocument();
    expect(screen.getByText("XS****02")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "已生成" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "已激活" })).toBeInTheDocument();
    expect(screen.getByText("user-9")).toBeInTheDocument();
    const listCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/control/activation-codes?"),
    );
    expect(String(listCall?.[0])).toBe(
      "http://127.0.0.1:8000/api/control/activation-codes?limit=50&offset=0",
    );
    expect(screen.queryByText(/XS-[A-Z]{4}/)).toBeNull();
  });

  it("filters the list by batch id and status", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodesPage />);
    await screen.findByText("XS****01");

    fireEvent.change(screen.getByLabelText("批次 ID"), {
      target: { value: "batch-1" },
    });
    fireEvent.change(screen.getByLabelText("状态"), {
      target: { value: "SUSPENDED" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/activation-codes?batch_id=batch-1&status=SUSPENDED&limit=50&offset=0",
          ),
        ),
      ).toBe(true),
    );
  });

  it("suspends a code with reason, confirmation and the request id", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodesPage />);
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "暂停" }))[1],
    );

    fireEvent.change(await screen.findByLabelText("操作原因"), {
      target: { value: "风控暂停" },
    });
    fireEvent.click(screen.getByLabelText("我已确认操作"));
    fireEvent.click(screen.getByRole("button", { name: "确认暂停" }));

    expect(await screen.findByText(/req-suspend-1/)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "已暂停" })).toBeInTheDocument();
    const suspendCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-2/suspend"),
    );
    expect(suspendCall?.[1]?.body).toBe(
      JSON.stringify({ confirm: true, reason: "风控暂停" }),
    );
    expect(suspendCall?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-token-1",
      "Idempotency-Key": expect.any(String),
    });
  });

  it("refuses a transition without a reason", async () => {
    const fetchMock = installFetch();

    render(<ActivationCodesPage />);
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "暂停" }))[1],
    );

    fireEvent.click(screen.getByLabelText("我已确认操作"));
    fireEvent.click(screen.getByRole("button", { name: "确认暂停" }));

    expect(await screen.findByText("请填写操作原因")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith("/suspend")),
    ).toBe(false);
  });

  it("maps an invalid transition to a deterministic message", async () => {
    installFetch({ suspend: "invalid-transition" });

    render(<ActivationCodesPage />);
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "暂停" }))[1],
    );

    fireEvent.change(await screen.findByLabelText("操作原因"), {
      target: { value: "尝试暂停" },
    });
    fireEvent.click(screen.getByLabelText("我已确认操作"));
    fireEvent.click(screen.getByRole("button", { name: "确认暂停" }));

    expect(await screen.findByText(/当前状态不允许该操作/)).toBeInTheDocument();
  });

  it("reports the session as expired on a 401 read", async () => {
    installFetch({ list: "unauthorized" });
    const onSessionExpired = vi.fn();

    render(<ActivationCodesPage onSessionExpired={onSessionExpired} />);

    expect(
      await screen.findByText(/会话已失效，请重新登录/),
    ).toBeInTheDocument();
    expect(onSessionExpired).toHaveBeenCalled();
  });

  it("hides every write action in read-only mode", async () => {
    installFetch();

    render(<ActivationCodesPage readOnly />);

    expect(await screen.findByText("XS****01")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "暂停" })).toBeNull();
    expect(screen.queryByRole("button", { name: "恢复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "作废" })).toBeNull();
    expect(screen.getByText(/当前为只读模式/)).toBeInTheDocument();
  });
});
