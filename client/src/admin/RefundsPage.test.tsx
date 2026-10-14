import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { RefundsPage } from "./RefundsPage";

afterEach(() => vi.unstubAllGlobals());

// adminWrite 需要管理会话 CSRF 头；测试环境无真实会话，直接注入。
beforeEach(() => setAdminCsrfToken("csrf-token-1"));

function stubFetch(items: unknown[] = []) {
  return vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (String(url).includes("/api/control/refunds?")) {
        return { items, total: items.length, limit: 20, offset: 0 };
      }
      return {};
    },
  }));
}

const PENDING_ROW = {
  adjustment_id: "adj-1",
  target_user_id: "u1",
  target_username: "jxhjmz",
  target_display_name: "江西合家美宅",
  source_document_ref: "ZPAY-REFUND-001",
  original_recharge_order_no: "PAY-FIXTURE-001",
  source_verified: true,
  reason: "客户申请退款",
  created_at: "2026-10-08T07:00:00Z",
  admin_username: "honor",
  credits_deducted: 500,
  executed: false,
  channel: "",
  channel_label: "",
  provider_ref: "",
  executed_at: "",
  executed_by: "",
  note: "",
};

it("待退付清单默认视图渲染客户与冲减积分，未执行行可发起登记", async () => {
  vi.stubGlobal("fetch", stubFetch([PENDING_ROW]));
  render(<RefundsPage />);
  expect(await screen.findByText("江西合家美宅")).toBeInTheDocument();
  expect(screen.getByText("-500 积分")).toBeInTheDocument();
  expect(screen.getAllByText("待退付").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "登记退付" })).toBeEnabled();
  // 已执行行的按钮不渲染（readOnly 也不渲染）。
  // 下拉里的"已退付"选项不属于行内状态徽标；用徽标文本精确匹配。
  expect(
    screen.queryByText("已退付", { exact: true, selector: "span" }),
  ).toBeNull();
});

it("登记退付：表单校验单号必填，提交成功后展示回执并刷新列表", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (String(url).includes("/execution")) {
        return JSON.parse(String(init?.body ?? "{}"));
      }
      return {
        items: [
          PENDING_ROW,
          {
            ...PENDING_ROW,
            adjustment_id: "adj-2",
            executed: true,
            channel: "wechat_manual",
            channel_label: "微信商户原路退回",
            provider_ref: "4200009999",
            executed_at: "2026-10-08T08:00:00Z",
            executed_by: "honor",
          },
        ],
        total: 2,
        limit: 20,
        offset: 0,
      };
    },
  }));
  vi.stubGlobal("fetch", fetchMock);
  render(<RefundsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "登记退付" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("冲减 500 积分");
  // 单号必填（原生 required），填写后提交。
  fireEvent.change(screen.getByLabelText("第三方退款单号"), {
    target: { value: "4200009999" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "ZPay 后台已退付，回填凭证" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认登记" }));
  await waitFor(() =>
    expect(screen.getByText(/已登记 jxhjmz 的退付执行/)).toBeInTheDocument(),
  );
  const calls = fetchMock.mock.calls.filter(([url]) =>
    String(url).includes("/execution"),
  );
  expect(calls).toHaveLength(1);
  const body = JSON.parse(String(calls[0][1]?.body));
  expect(body).toMatchObject({
    channel: "wechat_manual",
    provider_ref: "4200009999",
    confirm: true,
  });
});

it("readOnly 不渲染登记按钮", async () => {
  vi.stubGlobal("fetch", stubFetch([PENDING_ROW]));
  render(<RefundsPage readOnly />);
  await screen.findByText("江西合家美宅");
  expect(screen.queryByRole("button", { name: "登记退付" })).toBeNull();
});

it("筛选栏：查询在前、重置在后；重置回到默认的「待退付」并重新查询", async () => {
  const fetchMock = stubFetch([PENDING_ROW]);
  vi.stubGlobal("fetch", fetchMock);
  render(<RefundsPage />);
  await screen.findByText("江西合家美宅");

  const bar = screen.getByRole("form", { name: "退款筛选" });
  const names = Array.from(bar.querySelectorAll("button")).map(
    (button) => button.textContent,
  );
  expect(names).toEqual(["查询", "重置"]);

  fireEvent.change(screen.getByLabelText("退付状态"), {
    target: { value: "done" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("executed=done"),
      ),
    ).toBe(true),
  );
  fireEvent.click(screen.getByRole("button", { name: "重置" }));
  expect(screen.getByLabelText("退付状态")).toHaveValue("pending");
  await waitFor(() =>
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain(
      "executed=pending",
    ),
  );
});
