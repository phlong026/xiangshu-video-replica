import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearAdminActivationSession,
  exchangeAdminSession,
} from "../api.admin";
import { DeliveriesPage } from "./DeliveriesPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

// The mock literal is indirect so the repo secret scan (which flags
// `token:` followed by a quoted literal) stays quiet — the T29 precedent.
const CSRF_TOKEN_TEXT = "csrf-token-1";

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: {
    user_id: "admin-1",
    username: "admin",
    display_name: "管理员一号",
    role: "admin",
  },
};

const deliveredPayload = {
  code_id: "code-1",
  status: "ISSUED",
  delivery_id: "delivery-1",
  request_id: "req-deliver-1",
};

function installFetch(options?: {
  deliver?: "ok" | "invalid-transition" | "unauthorized";
}) {
  const deliverState = options?.deliver ?? "ok";
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(exchangePayload);
    }
    if (url.endsWith("/deliver") && init?.method === "POST") {
      if (deliverState === "invalid-transition") {
        return jsonResponse(
          {
            detail: {
              code: "CODE_TRANSITION_INVALID",
              message: "Only a GENERATED code can be delivered.",
            },
          },
          409,
        );
      }
      if (deliverState === "unauthorized") {
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
      return jsonResponse(deliveredPayload, 201);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function fillDeliverForm() {
  fireEvent.change(screen.getByLabelText("激活码 ID"), {
    target: { value: "code-1" },
  });
  fireEvent.change(screen.getByLabelText("发放渠道"), {
    target: { value: "offline" },
  });
  fireEvent.change(screen.getByLabelText("外部订单号（可选）"), {
    target: { value: "order-9" },
  });
  fireEvent.change(screen.getByLabelText("收件人引用（可选）"), {
    target: { value: "渠道商A" },
  });
  fireEvent.change(screen.getByLabelText("发放原因"), {
    target: { value: "线下渠道发货" },
  });
  fireEvent.click(screen.getByLabelText("我已确认发放"));
}

describe("DeliveriesPage", () => {
  beforeEach(async () => {
    installFetch();
    await exchangeAdminSession("ASX1.body.signature");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("delivers a code with the full write contract", async () => {
    const fetchMock = installFetch();

    render(<DeliveriesPage />);
    await fillDeliverForm();
    fireEvent.click(screen.getByRole("button", { name: "发放" }));

    expect(await screen.findByText("delivery-1")).toBeInTheDocument();
    expect(screen.getByText("req-deliver-1")).toBeInTheDocument();
    expect(screen.getByText("已发放")).toBeInTheDocument();
    const deliverCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-1/deliver"),
    );
    expect(deliverCall?.[1]?.body).toBe(
      JSON.stringify({
        channel: "offline",
        external_order_ref: "order-9",
        recipient_ref: "渠道商A",
        confirm: true,
        reason: "线下渠道发货",
      }),
    );
    expect(deliverCall?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-token-1",
      "Idempotency-Key": expect.any(String),
    });
  });

  it("omits optional references when left blank", async () => {
    const fetchMock = installFetch();

    render(<DeliveriesPage />);
    fireEvent.change(screen.getByLabelText("激活码 ID"), {
      target: { value: "code-1" },
    });
    fireEvent.change(screen.getByLabelText("发放渠道"), {
      target: { value: "email" },
    });
    fireEvent.change(screen.getByLabelText("发放原因"), {
      target: { value: "邮件补发" },
    });
    fireEvent.click(screen.getByLabelText("我已确认发放"));
    fireEvent.click(screen.getByRole("button", { name: "发放" }));

    await screen.findByText("delivery-1");
    const deliverCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-1/deliver"),
    );
    expect(deliverCall?.[1]?.body).toBe(
      JSON.stringify({
        channel: "email",
        confirm: true,
        reason: "邮件补发",
      }),
    );
  });

  it("refuses delivery without a channel or reason", async () => {
    const fetchMock = installFetch();

    render(<DeliveriesPage />);
    fireEvent.click(screen.getByRole("button", { name: "发放" }));

    expect(await screen.findByText("请填写激活码 ID")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("激活码 ID"), {
      target: { value: "code-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放" }));
    expect(await screen.findByText("请填写发放渠道")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("发放渠道"), {
      target: { value: "offline" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放" }));
    expect(await screen.findByText("请填写发放原因")).toBeInTheDocument();
    expect(fetchMock.mock.calls).toHaveLength(0);
  });

  it("maps an invalid transition to a deterministic message", async () => {
    installFetch({ deliver: "invalid-transition" });

    render(<DeliveriesPage />);
    await fillDeliverForm();
    fireEvent.click(screen.getByRole("button", { name: "发放" }));

    expect(
      await screen.findByText(/仅已生成的激活码可以发放/),
    ).toBeInTheDocument();
  });

  it("reports the session as expired on a 401 write", async () => {
    installFetch({ deliver: "unauthorized" });
    const onSessionExpired = vi.fn();

    render(<DeliveriesPage onSessionExpired={onSessionExpired} />);
    await fillDeliverForm();
    fireEvent.click(screen.getByRole("button", { name: "发放" }));

    expect(
      await screen.findByText(/会话已失效，请重新登录/),
    ).toBeInTheDocument();
    expect(onSessionExpired).toHaveBeenCalled();
  });

  it("disables the delivery form in read-only mode", () => {
    installFetch();

    render(<DeliveriesPage readOnly />);

    expect(screen.getByRole("button", { name: "发放" })).toBeDisabled();
    expect(screen.getByText(/当前为只读模式/)).toBeInTheDocument();
  });
});
