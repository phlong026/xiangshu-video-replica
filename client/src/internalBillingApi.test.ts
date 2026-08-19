import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createRechargeOrder,
  downloadControlRechargeOrdersCsv,
  downloadControlWalletTransactionsCsv,
  getControlAccounts,
  getControlSettings,
  getWallet,
  listRechargeOrders,
  listWalletTransactions,
  setInternalAccessToken,
  updateControlZPaySettings,
} from "./api";

describe("internal billing API", () => {
  afterEach(() => {
    setInternalAccessToken(null);
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends the transient Bearer token to business wallet and recharge APIs", async () => {
    setInternalAccessToken("internal-user-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getWallet();
    await listWalletTransactions({ limit: 20, offset: 0 });
    await createRechargeOrder(10000);
    await listRechargeOrders({ limit: 20, offset: 0 });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/wallet",
      "http://127.0.0.1:8000/api/wallet/transactions?limit=20&offset=0",
      "http://127.0.0.1:8000/api/recharge-orders",
      "http://127.0.0.1:8000/api/recharge-orders?limit=20&offset=0",
    ]);
    for (const [, options] of fetchMock.mock.calls) {
      expect((options.headers as Headers).get("Authorization")).toBe(
        "Bearer internal-user-token",
      );
    }
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ amount_fen: 10000 }),
      }),
    );
  });

  it("keeps control requests separate from business and development identity", async () => {
    setInternalAccessToken("must-not-reach-control-api");
    vi.stubEnv("VITE_DEV_USER_ID", "employee_1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getControlAccounts();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/control/accounts?limit=50&offset=0",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).has("Authorization")).toBe(false);
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });

  it("reads masked control settings and only submits the allowed ZPay fields", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          zpay: {
            provider: "zpay",
            configured: true,
            config: { pid: "merchant", key: "********cret" },
          },
          billing: {},
          deployment: {},
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          provider: "zpay",
          configured: true,
          config: { pid: "merchant", key: "********cret" },
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const snapshot = await getControlSettings();
    await updateControlZPaySettings({
      pid: "merchant",
      key: "",
      enabled_channels: ["alipay", "wxpay"],
    });

    expect(snapshot.zpay.config.key).toBe("********cret");
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          pid: "merchant",
          key: "",
          enabled_channels: ["alipay", "wxpay"],
        }),
      }),
    );
  });

  it("downloads read-only control CSV exports through the protected proxy", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    const createObjectURL = vi.fn(() => "blob:control-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["id\n1"], { type: "text/csv" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await downloadControlRechargeOrdersCsv();
    await downloadControlWalletTransactionsCsv();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/control/recharge-orders.csv",
      "http://127.0.0.1:8000/api/control/wallet-transactions.csv",
    ]);
    expect(createObjectURL).toHaveBeenCalledTimes(2);
  });
});
