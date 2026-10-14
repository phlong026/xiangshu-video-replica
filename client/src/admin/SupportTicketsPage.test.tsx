import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { SupportTicketsPage } from "./SupportTicketsPage";

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => setAdminCsrfToken("csrf-token-1"));

const ticket = {
  id: "t-1",
  target_user_id: "u-1",
  target_username: "hongya",
  target_display_name: "洪雅店",
  category: "billing",
  category_label: "计费/资金",
  summary: "充值后积分未到账",
  status: "open",
  status_label: "待处理",
  handler_user_id: "",
  handler_username: "",
  resolution: "",
  created_by: "admin_u",
  created_at: "2026-10-14T02:00:00Z",
  updated_at: "2026-10-14T02:00:00Z",
  priority: "P0",
  related_ref: "order:W15-ORDER-1",
};

function installFetch() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = new URL(url).pathname;
    let body: unknown = {};
    if (path.endsWith("/support-tickets/handlers")) {
      body = {
        items: [
          { user_id: "admin_u", username: "admin_u", display_name: "值班" },
          { user_id: "ops_2", username: "ops_2", display_name: "" },
        ],
      };
    } else if (path.endsWith("/support-tickets") && method === "GET") {
      body = { items: [ticket], total: 1, limit: 20, offset: 0 };
    } else if (path.endsWith("/customers")) {
      body = {
        items: [{ user_id: "u-9", username: "jxhj", display_name: "江西店" }],
        total: 1,
      };
    } else {
      body = { id: "t-new" };
    }
    return { ok: true, status: 200, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function lastWrite(fetchMock: ReturnType<typeof installFetch>, method: string) {
  const call = [...fetchMock.mock.calls]
    .reverse()
    .find(([, init]) => (init as RequestInit | undefined)?.method === method);
  return JSON.parse(String((call?.[1] as RequestInit | undefined)?.body));
}

it("清单显示优先级与可跳转的关联订单", async () => {
  installFetch();
  render(<SupportTicketsPage />);
  expect(await screen.findByText("充值后积分未到账")).toBeInTheDocument();
  expect(screen.getByText("P0")).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "充值订单 W15-ORDER-1" }),
  ).toHaveAttribute("href", "#admin/funds?sub=orders&orderNo=W15-ORDER-1");
});

it("登记工单：按名字选客户、选优先级与关联任务号", async () => {
  const fetchMock = installFetch();
  render(<SupportTicketsPage />);
  await screen.findByText("充值后积分未到账");
  fireEvent.click(screen.getByRole("button", { name: "登记工单" }));
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "江西" },
  });
  fireEvent.click(
    await screen.findByRole("button", { name: "江西店（jxhj）" }),
  );
  fireEvent.change(screen.getByLabelText("优先级"), {
    target: { value: "P1" },
  });
  fireEvent.change(screen.getByLabelText("关联对象（可选）"), {
    target: { value: "task" },
  });
  fireEvent.change(screen.getByLabelText("生成任务号"), {
    target: { value: "gen-42" },
  });
  fireEvent.change(screen.getByLabelText(/问题描述/), {
    target: { value: "生成卡住" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "客户群反馈" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认登记" }));
  await waitFor(() =>
    expect(screen.getByText("工单已登记。")).toBeInTheDocument(),
  );
  expect(lastWrite(fetchMock, "POST")).toMatchObject({
    target_user_id: "u-9",
    priority: "P1",
    related_ref: "task:gen-42",
    summary: "生成卡住",
  });
});

it("指派负责人并调整优先级", async () => {
  const fetchMock = installFetch();
  render(<SupportTicketsPage />);
  await screen.findByText("充值后积分未到账");
  fireEvent.click(screen.getByRole("button", { name: "指派 / 优先级" }));
  const handler = screen.getByLabelText("负责人");
  await waitFor(() => expect(handler).not.toBeDisabled());
  fireEvent.change(handler, { target: { value: "ops_2" } });
  fireEvent.change(screen.getAllByLabelText("优先级").at(-1) as HTMLElement, {
    target: { value: "P2" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "转交运营二组" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() =>
    expect(screen.getByText("工单负责人与优先级已更新。")).toBeInTheDocument(),
  );
  expect(lastWrite(fetchMock, "PATCH")).toMatchObject({
    handler_user_id: "ops_2",
    priority: "P2",
  });
});

it("客户详情内嵌：按客户编号精确取、不显示客户筛选，登记时关联客户固定", async () => {
  const fetchMock = installFetch();
  render(
    <SupportTicketsPage
      customer={{ user_id: "u-1", username: "hongya", display_name: "洪雅店" }}
    />,
  );
  expect(await screen.findByText("充值后积分未到账")).toBeInTheDocument();
  const listCall = fetchMock.mock.calls
    .map(([url]) => new URL(String(url)))
    .find((url) => url.pathname.endsWith("/support-tickets"));
  expect(listCall?.searchParams.get("user_id")).toBe("u-1");
  expect(screen.queryByLabelText("客户")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "登记工单" }));
  expect(await screen.findByText("关联客户：hongya")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/问题描述/), {
    target: { value: "积分没到账" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "客户反馈" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认登记" }));
  await waitFor(() =>
    expect(lastWrite(fetchMock, "POST").target_user_id).toBe("u-1"),
  );
});
