import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { SessionsPage } from "./SessionsPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const CUSTOMER_ID = "customer-1";

const sessionItem = {
  session_id: "sess-1",
  user_id: CUSTOMER_ID,
  username: "customer_one",
  device_id: "device-1",
  session_epoch: 3,
  lease_until: "2026-09-01T12:00:00+00:00",
  last_heartbeat_at: "2026-09-01T11:55:00+00:00",
  created_at: "2026-08-30T08:00:00+00:00",
  updated_at: "2026-09-01T11:55:00+00:00",
  device_name: "办公室电脑",
  platform: "windows",
  slot_no: 1,
  device_status: "ACTIVE",
};

function installFetch(options?: {
  sessionsStatus?: number;
  adjustmentStatus?: number;
}) {
  const fetchMock = vi.fn((url: string) => {
    if (
      String(url).includes(`/api/control/customers/${CUSTOMER_ID}/sessions`)
    ) {
      if (options?.sessionsStatus) {
        return jsonResponse({}, options.sessionsStatus);
      }
      return jsonResponse({
        items: [sessionItem],
        total: 3,
        limit: 50,
        offset: 0,
      });
    }
    if (
      String(url).includes(`/api/control/customers/${CUSTOMER_ID}/adjustments`)
    ) {
      return jsonResponse(
        {
          adjustment_id: "adj-1",
          order_id: "order-1",
          credits: "10",
          amount_fen: "10000",
          pricing_scope: "CUSTOMER_STANDARD",
          wallet_balance_after: 60,
          source_document_type: "CS_TICKET",
          source_document_ref: "manual-20260902-001",
          request_id: "req-adj-1",
        },
        options?.adjustmentStatus ?? 201,
      );
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function queryCustomerSessions() {
  fireEvent.change(screen.getByLabelText("客户 ID"), {
    target: { value: CUSTOMER_ID },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询会话" }));
  await screen.findByText("会话 sess-1");
}

describe("SessionsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the empty state before any query without issuing a request", () => {
    const fetchMock = installFetch();
    render(<SessionsPage />);

    expect(screen.getByText("该客户当前没有活动会话。")).toBeInTheDocument();
    expect(screen.getByLabelText("客户 ID")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "查询会话" }),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("loads and displays the sessions of the queried customer", async () => {
    const fetchMock = installFetch();
    render(<SessionsPage />);

    await queryCustomerSessions();

    expect(screen.getByText("会话 sess-1")).toBeInTheDocument();
    expect(screen.getByText("客户 customer_one")).toBeInTheDocument();
    expect(screen.getByText("设备 办公室电脑")).toBeInTheDocument();
    expect(screen.getByText("槽位 #1")).toBeInTheDocument();
    expect(screen.getByText("状态 ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("共 3 条，当前显示 1 条。")).toBeInTheDocument();

    // A successful query targets the customer for the T23 background write.
    expect(screen.getByText("目标客户：customer-1")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "执行后台加款" }),
    ).toBeInTheDocument();

    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith(
          `/api/control/customers/${CUSTOMER_ID}/sessions?limit=50`,
        ),
      ),
    ).toBe(true);
  });

  it("shows the load failure as an alert", async () => {
    installFetch({ sessionsStatus: 500 });
    render(<SessionsPage />);

    fireEvent.change(screen.getByLabelText("客户 ID"), {
      target: { value: CUSTOMER_ID },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询会话" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("加载失败：读取客户会话失败（500）");
  });

  it("validates the adjustment write before sending anything", async () => {
    const fetchMock = installFetch();
    render(<SessionsPage />);

    await queryCustomerSessions();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "执行后台加款" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("请输入大于 0 的加款条数");
    // Validation must reject the submission before any write request.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("routes the adjustment through the high-risk confirmation dialog", async () => {
    setAdminCsrfToken("csrf-token-1");
    const fetchMock = installFetch({ adjustmentStatus: 201 });
    render(<SessionsPage />);

    await queryCustomerSessions();

    fireEvent.change(screen.getByLabelText("加款条数"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("来源单号"), {
      target: { value: "manual-20260902-001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "执行后台加款" }));

    // 表单通过校验后打开高危确认对话框：原因必填 + 我已知晓勾选。
    const dialog = await screen.findByRole("dialog", { name: "确认后台加款" });
    expect(dialog).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "确认加款" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "请填写操作原因",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服工单补发" },
    });
    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认加款" }));

    expect(await screen.findByText(/调账成功/)).toBeInTheDocument();
    // 会话查询 + 调账写入 + 加款成功后的会话刷新。
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const writeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/adjustments"),
    );
    expect(writeCall).toBeDefined();
  });

  it("stays read-only for auditors when mounted with a fixed user", () => {
    const fetchMock = installFetch();
    render(<SessionsPage userId={CUSTOMER_ID} readOnly />);

    expect(screen.getByText("该客户当前没有活动会话。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查询会话" })).toBeNull();
    expect(screen.queryByRole("button", { name: "执行后台加款" })).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
