import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountsPage } from "./AccountsPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function accountPage(offset: number, total: number) {
  return {
    items: [
      {
        id: `user-${offset}`,
        username: `operator-${offset}`,
        display_name: `运营 ${offset}`,
        role: "employee",
        is_active: true,
        available_credits: 18,
        reserved_credits: 2,
        active_token_count: 1,
      },
    ],
    total,
    limit: 20,
    offset,
  };
}

function transactionPage(offset: number, total: number) {
  return {
    items: [
      {
        id: `tx-${offset}`,
        user_id: "user-1",
        username: "operator-1",
        type: "CHARGE",
        available_delta: 10,
        reserved_delta: 0,
        recharge_order_id: "order-1",
        task_id: null,
        billing_round: null,
        created_at: "2026-09-01T10:01:00Z",
      },
    ],
    total,
    limit: 20,
    offset,
  };
}

function installFetch() {
  const fetchMock = vi.fn((url: string) => {
    const { searchParams, pathname } = new URL(String(url));
    const offset = Number(searchParams.get("offset") ?? "0");
    if (pathname.endsWith("/api/control/accounts")) {
      return jsonResponse(accountPage(offset, 41));
    }
    if (pathname.endsWith("/api/control/wallet-transactions")) {
      return jsonResponse(transactionPage(offset, 41));
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("AccountsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders accounts and transactions with the unified labels", async () => {
    installFetch();
    render(<AccountsPage />);

    expect(await screen.findByText("operator-0")).toBeInTheDocument();
    expect(screen.getByText("充值到账")).toBeInTheDocument();
    expect(screen.getByText("员工")).toBeInTheDocument();
  });

  it("pages accounts and transactions with the server total", async () => {
    const fetchMock = installFetch();
    render(<AccountsPage />);

    // 账号与流水各自独立分页；total=41、页大小 20 → 共 3 页（两条分页条）。
    expect(await screen.findAllByText("第 1 / 3 页（共 41 条）")).toHaveLength(
      2,
    );
    expect(screen.getAllByRole("button", { name: "下一页" })).toHaveLength(2);

    fireEvent.click(screen.getAllByRole("button", { name: "下一页" })[0]);

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/control/accounts?limit=20&offset=20"),
        ),
      ).toBe(true),
    );
  });
});
