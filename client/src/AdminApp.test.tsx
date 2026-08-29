import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminApp } from "./AdminApp";
import { getAdminCsrfToken, SESSION_EXPIRED_EVENT } from "./api";

const SERVICE_KEY_TEXT = ["service", "key"].join("-");
const MASKED_SERVICE_KEY = ["********", "cret"].join("");
const MASKED_STORAGE_SECRET = ["********", "5678"].join("");
const CSRF_TOKEN_TEXT = ["csrf", "token", "admin"].join("-");

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
};

const adminSession = {
  session_id: "session-1",
  expires_at: "2026-08-28T00:00:00+00:00",
  last_activity_at: "2026-08-27T16:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: adminActor,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function blobResponse() {
  return Promise.resolve({
    ok: true,
    status: 200,
    blob: async () => new Blob(["id\n1"], { type: "text/csv" }),
  });
}

const accountsPage = {
  items: [
    {
      id: "user-1",
      username: "operator-1",
      display_name: "运营一号",
      role: "employee",
      is_active: true,
      available_credits: 18,
      reserved_credits: 2,
      active_token_count: 1,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const ordersPage = {
  items: [
    {
      id: "order-1",
      user_id: "user-1",
      username: "operator-1",
      display_name: "运营一号",
      order_no: "202608190001",
      status: "PENDING",
      amount_fen: 10000,
      credits: 10,
      channel: "alipay",
      provider_trade_no: null,
      created_at: "2026-08-19 10:00:00",
      paid_at: null,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const transactionsPage = {
  items: [
    {
      id: "tx-1",
      user_id: "user-1",
      username: "operator-1",
      type: "CHARGE",
      available_delta: 10,
      reserved_delta: 0,
      recharge_order_id: "order-1",
      task_id: null,
      billing_round: null,
      created_at: "2026-08-19 10:01:00",
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const reconciliation = {
  wallet_count: 1,
  wallet_mismatch_count: 0,
  paid_order_without_charge_count: 2,
  charge_without_paid_order_count: 1,
  pending_order_count: 1,
};

const settings = {
  providers: {
    metaso: { provider: "metaso", configured: false, config: {} },
    apilio: { provider: "apilio", configured: false, config: {} },
    cos: {
      provider: "cos",
      configured: true,
      config: {
        access_key_id: "********1234",
        secret_access_key: MASKED_STORAGE_SECRET,
        bucket: "private-materials",
        region: "ap-shanghai",
      },
    },
    deepseek: { provider: "deepseek", configured: false, config: {} },
  },
  runtime: {
    max_generation_count_per_batch: 4,
    max_concurrent_h3_tasks: 2,
    active_storage_provider: "cos",
  },
  billing: {
    internal_base_unit_price_fen: 1000,
    charged_unit_price_fen: 1000,
    min_recharge_fen: 10000,
    recharge_step_fen: 1000,
  },
  zpay: {
    provider: "zpay",
    configured: true,
    config: {
      pid: "merchant-1",
      key: "********cret",
      enabled_channels: "alipay,wxpay",
    },
  },
  deployment: {
    gateway_url: "https://zpayz.cn/submit.php",
    notify_url: "https://internal.example/api/payments/zpay/notify",
    return_url: "https://internal.example/api/payments/zpay/return",
  },
};

function installFetch(options?: { session?: "valid" | "missing" }) {
  const sessionState = options?.session ?? "missing";
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (
      url.endsWith("/api/control/admin/session") &&
      (!options?.method || options.method === "GET")
    ) {
      if (sessionState === "valid") {
        return jsonResponse(adminSession);
      }
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
    if (url.endsWith("/api/control/admin/session/password")) {
      return jsonResponse(adminSession, 201);
    }
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(adminSession, 201);
    }
    if (
      url.endsWith("/api/control/admin/password") &&
      options?.method === "PUT"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (
      url.endsWith("/api/control/admin/session") &&
      options?.method === "DELETE"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (url.includes("/api/control/accounts?")) {
      return jsonResponse(accountsPage);
    }
    if (url.includes("/api/control/recharge-orders?")) {
      return jsonResponse(ordersPage);
    }
    if (url.includes("/api/control/wallet-transactions?")) {
      return jsonResponse(transactionsPage);
    }
    if (url.includes("/api/control/customers?")) {
      return jsonResponse({
        customers: [],
        total: 0,
        page: 1,
        page_size: 20,
      });
    }
    if (url.endsWith("/api/control/billing-reconciliation")) {
      return jsonResponse(reconciliation);
    }
    if (url.endsWith("/api/control/settings") && !options?.method) {
      return jsonResponse(settings);
    }
    if (url.endsWith("/api/control/settings/zpay")) {
      return jsonResponse(settings.zpay);
    }
    if (url.endsWith("/api/control/settings/billing")) {
      return jsonResponse(settings.billing);
    }
    if (url.endsWith("/api/control/settings/providers/metaso")) {
      return jsonResponse({
        provider: "metaso",
        configured: true,
        config: { api_key: MASKED_SERVICE_KEY },
      });
    }
    if (
      url.endsWith("/api/control/settings/providers/metaso/connection-test")
    ) {
      return jsonResponse({
        status: "configured_only",
        provider: "metaso",
        test_kind: "connection",
      });
    }
    if (url.endsWith("/api/control/settings/runtime")) {
      return jsonResponse(settings.runtime);
    }
    if (url.endsWith("/api/control/recharge-orders/202608190001/sync")) {
      return jsonResponse({ ...ordersPage.items[0], status: "PAID" });
    }
    if (
      url.endsWith("/api/control/recharge-orders.csv") ||
      url.endsWith("/api/control/wallet-transactions.csv")
    ) {
      return blobResponse();
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:test"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
  return fetchMock;
}

async function signInWithPassword() {
  fireEvent.change(await screen.findByLabelText("管理员账号"), {
    target: { value: "admin" },
  });
  fireEvent.change(screen.getByLabelText("管理员密码"), {
    target: { value: "Admin Login Passphrase 2026!" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录后台" }));
  await waitFor(() => {
    expect(
      screen.queryByRole("navigation", { name: "管理端导航" }) ??
        screen.queryByRole("button", { name: "展开导航" }),
    ).toBeTruthy();
  });
}

describe("AdminApp", () => {
  beforeEach(() => {
    window.location.hash = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("starts at the account-password gate before exposing control navigation", async () => {
    const fetchMock = installFetch();

    render(<AdminApp />);

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByLabelText("管理员密码")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "管理端导航" })).toBeNull();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/control/accounts?"),
      ),
    ).toBe(false);
  });

  it("keeps order operations to sync and CSV export", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("button", { name: "充值订单" }));

    expect(
      await screen.findByText(
        (_, element) => element?.textContent === "待支付订单 1",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("已支付未入账 2")).toBeInTheDocument();
    expect(screen.getByText("入账但订单未支付 1")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "同步 202608190001" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "导出充值订单 CSV" }));
    fireEvent.click(screen.getByRole("button", { name: "导出账务流水 CSV" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/recharge-orders/202608190001/sync",
          ),
        ),
      ).toBe(true),
    );
    expect(screen.queryByRole("button", { name: /补单|改余额/ })).toBeNull();
  });

  it("saves ZPay and price settings while deployment URLs stay server-owned", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("button", { name: "支付与价格" }));
    expect(await screen.findByDisplayValue("merchant-1")).toBeInTheDocument();
    expect(screen.getByText("********cret")).toBeInTheDocument();
    expect(screen.queryByLabelText("网关地址")).toBeNull();
    expect(screen.queryByLabelText("异步回调地址")).toBeNull();
    expect(screen.queryByLabelText("同步返回地址")).toBeNull();
    expect(
      screen.getByText("支付接口地址由系统自动配置，无需填写。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("ZPay 商户 PID"), {
      target: { value: "merchant-2" },
    });
    fireEvent.change(screen.getByLabelText("新商户密钥"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存 ZPay 设置" }));
    fireEvent.change(screen.getByLabelText("内部单价（分/条）"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存内部价格" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/control/settings/zpay") &&
            options?.method === "PATCH",
        ),
      ).toBe(true),
    );
    const zpayCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/settings/zpay") &&
        options?.method === "PATCH",
    );
    const zpayRequest = zpayCall?.[1] as RequestInit | undefined;
    expect(zpayRequest).toBeTruthy();
    expect(zpayCall?.[1]?.body).toBe(
      JSON.stringify({
        pid: "merchant-2",
        key: "",
        enabled_channels: ["alipay", "wxpay"],
        confirm: true,
        reason: "更新 ZPay 支付配置",
      }),
    );
    expect(
      new Headers(zpayRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(String(zpayCall?.[1]?.body)).not.toContain("gateway_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("notify_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("return_url");
  });

  it("configures generic services through the production control plane", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("button", { name: "服务配置" }));
    expect(
      await screen.findByRole("heading", { name: "视频生成" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "腾讯云存储" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getAllByLabelText("API Key")[0], {
      target: { value: SERVICE_KEY_TEXT },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" })[0]);

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/control/settings/providers/metaso") &&
            options?.method === "PUT",
        ),
      ).toBe(true),
    );
    const saveCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/settings/providers/metaso") &&
        options?.method === "PUT",
    );
    const saveRequest = saveCall?.[1] as RequestInit | undefined;
    expect(saveRequest).toBeTruthy();
    expect(saveCall?.[1]?.body).toBe(
      JSON.stringify({
        config: { api_key: SERVICE_KEY_TEXT },
        confirm: true,
        reason: "更新 metaso 服务配置",
      }),
    );
    expect(
      new Headers(saveRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(screen.queryByText(/metaso|minimax|cos/i)).toBeNull();
  });

  it("opens activation management only after account-password login", async () => {
    installFetch();

    render(<AdminApp />);
    await signInWithPassword();
    fireEvent.click(screen.getByRole("button", { name: "激活码" }));

    expect(
      await screen.findByRole("button", { name: "生成激活码" }),
    ).toBeInTheDocument();
  });

  it("restores a writable session after refresh without another login", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("管理员密码")).toBeNull();
    expect(screen.getAllByText("管理员一号").length).toBeGreaterThan(0);
  });

  it("returns to the login gate and clears admin session state when a control 401 emits the shared expiry event", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
    expect(getAdminCsrfToken()).toBe(CSRF_TOKEN_TEXT);

    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByLabelText("管理员密码")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "管理端导航" })).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "会话已失效，请重新登录。",
    );
    expect(getAdminCsrfToken()).toBeNull();
  });

  it("removes the shared expiry listener when the admin shell unmounts", async () => {
    installFetch({ session: "valid" });
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");

    const { unmount } = render(<AdminApp />);

    await screen.findByRole("navigation", { name: "管理端导航" });
    const sessionListener = addSpy.mock.calls.find(
      ([name]) => name === SESSION_EXPIRED_EVENT,
    )?.[1];
    expect(sessionListener).toBeTypeOf("function");

    unmount();

    expect(removeSpy).toHaveBeenCalledWith(
      SESSION_EXPIRED_EVENT,
      sessionListener,
    );
  });

  it("uses a one-time credential only to set or recover the password", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);

    fireEvent.click(
      await screen.findByRole("button", { name: "首次设置或找回密码" }),
    );
    fireEvent.change(screen.getByLabelText("一次性恢复凭据"), {
      target: { value: "ASX1.body.signature" },
    });
    fireEvent.click(screen.getByRole("button", { name: "验证恢复凭据" }));
    expect(await screen.findByLabelText("新管理员密码")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("新管理员密码"), {
      target: { value: "Recovered Admin Passphrase 2026!" },
    });
    fireEvent.change(screen.getByLabelText("确认新管理员密码"), {
      target: { value: "Recovered Admin Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存新密码" }));

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          String(url).endsWith("/api/control/admin/password") &&
          options?.method === "PUT",
      ),
    ).toBe(true);
  });

  it("keeps key order actions available on a narrow viewport", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    installFetch();

    render(<AdminApp />);
    await signInWithPassword();
    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
    fireEvent.click(screen.getByRole("button", { name: "充值订单" }));

    expect(
      await screen.findByRole("button", { name: "同步 202608190001" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "导出充值订单 CSV" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeInTheDocument();
  });

  it("groups admin navigation and lets compact layouts collapse and reopen it", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 390,
    });
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("button", { name: "展开导航" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "客户" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));

    expect(screen.getAllByText("运营概览").length).toBeGreaterThan(0);
    expect(screen.getAllByText("客户运营").length).toBeGreaterThan(0);
    expect(screen.getAllByText("系统治理").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "客户" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭导航" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: "客户" }));

    expect(
      await screen.findByRole("heading", { name: "客户管理" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
