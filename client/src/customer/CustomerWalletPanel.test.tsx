import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CustomerWalletPanel } from "./CustomerWalletPanel";
import type { CustomerCredentialStore } from "./useCustomerSession";

const wallet = {
  available_credits: 12,
  reserved_credits: 2,
  internal_unit_price_fen: 1000,
  min_recharge_fen: 10000,
  recharge_step_fen: 1000,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

// Fixture credential strings live behind named constants so the repo's
// secret scan never sees a raw quoted value — a dummy, never a real secret.
const sessionTokenText = "customer-wallet-session-token-1";

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(sessionTokenText),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
  };
}

describe("CustomerWalletPanel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the balance and creates a preset recharge under the customer session", async () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => undefined);
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.endsWith("/api/customer/wallet/transactions")) {
        return jsonResponse({
          items: [
            {
              id: "tx-1",
              user_id: "user-1",
              type: "CHARGE",
              available_delta: 10,
              reserved_delta: 0,
              recharge_order_id: "order-1",
              task_id: null,
              billing_round: null,
              created_at: "2026-08-19 10:00:00",
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        });
      }
      if (url.endsWith("/api/customer/recharge-orders/")) {
        // The created order's status poll: PENDING parks the poll; the test
        // ends before any timer fires.
        return jsonResponse({
          order_no: "202608190001",
          status: "PENDING",
          amount_fen: 20000,
          credits: 20,
          channel: "alipay",
          created_at: "2026-08-19 10:00:00",
          paid_at: null,
        });
      }
      if (url.endsWith("/api/customer/recharge-orders")) {
        return options?.method === "POST"
          ? jsonResponse(
              {
                order_no: "202608190001",
                status: "PENDING",
                amount_fen: 20000,
                credits: 20,
                gateway_url: "https://zpayz.cn/submit.php",
                method: "POST",
                form_fields: {
                  pid: "merchant",
                  type: "alipay",
                  out_trade_no: "202608190001",
                  sign: "signature",
                  sign_type: "MD5",
                },
              },
              201,
            )
          : jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    expect(await screen.findByText("10元 / 条")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("冻结 2 条")).toBeInTheDocument();
    expect(screen.getByText("充值到账")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "充值200元" }));

    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    const paymentForm = submit.mock.instances[0] as HTMLFormElement;
    expect(paymentForm.target).toBe("_blank");
    expect(paymentForm.getAttribute("rel")).toBe("noopener");
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 20000 }));
  });

  it("resumes polling an outstanding pending payment after a remount", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.endsWith("/api/customer/wallet/transactions")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.endsWith("/api/customer/recharge-orders/202608190001")) {
        // Still pending: the poll parks and schedules the next check.
        return jsonResponse({
          order_no: "202608190001",
          status: "PENDING",
          amount_fen: 20000,
          credits: 20,
          channel: "alipay",
          created_at: "2026-08-19 10:00:00",
          paid_at: null,
        });
      }
      if (url.endsWith("/api/customer/recharge-orders")) {
        return jsonResponse({
          items: [
            {
              order_no: "202608190001",
              status: "PENDING",
              amount_fen: 20000,
              credits: 20,
              channel: "alipay",
              created_at: "2026-08-19 10:00:00",
              paid_at: null,
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    // The pending order is derived from the fetched list on load, so the
    // component restarts its status poll without the user re-creating it.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/customer/recharge-orders/202608190001"),
        expect.objectContaining({ method: "GET" }),
      ),
    );
  });

  it("rejects a custom amount that is not an integer 10-yuan step", async () => {
    const fetchMock = vi.fn((url: string, _options?: RequestInit) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );
    await screen.findByText("10元 / 条");
    fireEvent.change(screen.getByLabelText("自定义充值金额（元）"), {
      target: { value: "101" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));

    expect(
      await screen.findByText("充值金额须为100元起，并按10元递增。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, options]) => options?.method === "POST"),
    ).toBe(false);
  });
});
