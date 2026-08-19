import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminApp } from "./AdminApp";

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

function installFetch() {
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (url.includes("/api/control/accounts?")) {
      return jsonResponse(accountsPage);
    }
    if (url.includes("/api/control/recharge-orders?")) {
      return jsonResponse(ordersPage);
    }
    if (url.includes("/api/control/wallet-transactions?")) {
      return jsonResponse(transactionsPage);
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

describe("AdminApp", () => {
  beforeEach(() => {
    window.location.hash = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("shows the three internal admin tabs with read-only accounts and wallets", async () => {
    installFetch();

    render(<AdminApp />);

    for (const label of ["账号与钱包", "充值订单", "支付与价格"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(await screen.findAllByText("operator-1")).toHaveLength(2);
    expect(screen.getByText("18")).toBeInTheDocument();
    expect(screen.getByText("tx-1")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /调整余额|手工加款/ }),
    ).toBeNull();
  });

  it("keeps order operations to sync and CSV export", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);

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

  it("saves ZPay and price settings without submitting deployment URLs", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);

    fireEvent.click(screen.getByRole("button", { name: "支付与价格" }));
    expect(await screen.findByDisplayValue("merchant-1")).toBeInTheDocument();
    expect(screen.getByText("********cret")).toBeInTheDocument();
    expect(screen.getByLabelText("网关地址")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("异步回调地址")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("同步返回地址")).toHaveAttribute("readonly");

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
    expect(zpayCall?.[1]?.body).toBe(
      JSON.stringify({
        pid: "merchant-2",
        key: "",
        enabled_channels: ["alipay", "wxpay"],
      }),
    );
    expect(String(zpayCall?.[1]?.body)).not.toContain("gateway_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("notify_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("return_url");
  });

  it("keeps key order actions available on a narrow viewport", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    installFetch();

    render(<AdminApp />);
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
});
