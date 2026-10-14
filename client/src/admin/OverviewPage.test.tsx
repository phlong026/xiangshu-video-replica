import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewPage } from "./OverviewPage";

const summaryPayload = {
  today: {
    generation_count: 328,
    succeeded: 302,
    success_rate_pct: 92.1,
    output_seconds: 4860,
    cost_fen: 48620,
    revenue_fen: 161460,
    gross_fen: 112840,
    margin_pct: 69.9,
    online_devices: 47,
    active_customers: 89,
    recharge_fen: 485000,
    recharge_orders: 12,
    // 2026-09-18 匹配梳理 P3：这两个字段后端一直返回，此前总览未展示。
    pending_operations: 4,
    unknown_revenue_operations: 1,
  },
  trend: [
    { day: "2026-08-30", succeeded: 210, failed: 28, cost_fen: 39000 },
    { day: "2026-09-05", succeeded: 302, failed: 12, cost_fen: 48620 },
  ],
  todos: {
    pending_pairings: 3,
    failed_tasks_7d: 5,
    analysis_failures_7d: 3,
    analysis_failure_reasons: [
      {
        error_code: "ANALYSIS_PROVIDER_FAILED",
        failure_phase: "http",
        reason: "model gemini-3.8-flash is not available",
        count: 3,
      },
    ],
    reconciliation_problems: 2,
    expiring_codes_7d: 0,
    unconfigured_rates: 2,
    unknown_cost_records: 0,
  },
  device_slots: { bound: 918, total: 1024 },
};

function installFetch(payload: unknown = summaryPayload) {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes("/api/control/dashboard/summary")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      });
    }
    if (url.includes("/api/control/customers?")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          items: [
            { user_id: "u-1", username: "hongya", display_name: "洪雅店" },
          ],
          total: 1,
        }),
      });
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:test"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("OverviewPage", () => {
  it("renders KPI cards, trend, todos and quick actions from the summary", async () => {
    const onNavigate = vi.fn();
    installFetch();
    render(<OverviewPage onNavigate={onNavigate} />);

    expect(await screen.findByText("328")).toBeInTheDocument();
    expect(screen.getByText("¥4850.00")).toBeInTheDocument();
    expect(screen.getByText("¥486.20")).toBeInTheDocument();
    expect(screen.getByText("¥1128.40")).toBeInTheDocument();
    expect(screen.queryByText("918 台")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "登录设备" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "有效客户" })).toHaveTextContent(
      "89",
    );
    expect(screen.getByText("近 7 日生成与成本")).toBeInTheDocument();
    expect(screen.getByText("成本（元）")).toBeInTheDocument();
    expect(screen.getByText("成功生成数（条）")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /图标/ })).toHaveLength(6);
    // 对账异常只在待办里出现一次，不再重复成 KPI 卡。
    expect(screen.queryByRole("region", { name: "对账异常" })).toBeNull();
    expect(screen.queryByText("点击下钻资金流水")).toBeNull();

    // 待办计数
    expect(screen.queryByText("待批准配对")).not.toBeInTheDocument();
    expect(screen.getByText("对账不一致")).toBeInTheDocument();
    expect(screen.queryByText("即将过期激活码")).not.toBeInTheDocument();

    // 快捷操作跳转
    expect(
      screen.queryByRole("button", { name: "快速发码" }),
    ).not.toBeInTheDocument();
    // P0-1：收款开通与赠送分开入口，「后台加款」字样不再出现。
    expect(
      screen.queryByRole("button", { name: "后台加款" }),
    ).not.toBeInTheDocument();
    // T13：快捷操作先选客户，选定后带 userId 直达该客户的对应表单。
    fireEvent.click(screen.getByRole("button", { name: "开通套餐（已收款）" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "前往表单" }));
    expect(within(dialog).getByText("请先选择客户。")).toBeInTheDocument();
    expect(onNavigate).not.toHaveBeenCalledWith(
      "customerPackage",
      expect.anything(),
    );
    fireEvent.change(within(dialog).getByRole("searchbox"), {
      target: { value: "洪雅" },
    });
    fireEvent.click(
      await within(dialog).findByRole("button", { name: "洪雅店（hongya）" }),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "前往表单" }));
    expect(onNavigate).toHaveBeenCalledWith("customerPackage", {
      userId: "u-1",
    });
    fireEvent.click(screen.getByRole("button", { name: "赠送积分" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("赠送积分：选择客户");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "退款扣减" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("退款扣减：选择客户");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "成本核对" }));
    expect(onNavigate).toHaveBeenCalledWith("costDetails");

    const missingRates = screen.getByText("成本待配置").closest("li");
    fireEvent.click(within(missingRates as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("rates");
    const failedTasks = screen.getByText("失败任务待处理").closest("li");
    fireEvent.click(within(failedTasks as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("failedGenerationRecords");

    // 2026-09-18 匹配梳理 P3：这两个字段后端一直在返回，总览此前只字未提。
    // 它们决定"今天的数字能不能信"，所以摆进待办并可一键去处理。
    const pendingOps = screen.getByText("今日操作待结算").closest("li");
    expect(pendingOps).toHaveTextContent("4");
    expect(pendingOps).toHaveTextContent("当日金额尚未定稿");
    // P0-5：这两项是计费操作口径，落到经营分析而不是资金流水。
    fireEvent.click(within(pendingOps as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("pendingOperations");

    const unknownRevenue = screen.getByText("今日收入未确定").closest("li");
    expect(unknownRevenue).toHaveTextContent("1");
    fireEvent.click(within(unknownRevenue as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("unknownRevenue");

    // 拆解失败单列一行，并把最集中的上游原因摆在行上。
    expect(screen.getByText("拆解失败待排查")).toBeInTheDocument();
    expect(
      screen.getByText("model gemini-3.8-flash is not available"),
    ).toBeInTheDocument();
    const analysisFailures = screen.getByText("拆解失败待排查").closest("li");
    fireEvent.click(
      within(analysisFailures as HTMLElement).getByRole("button"),
    );
    expect(onNavigate).toHaveBeenCalledWith("analysisFailures");
  });

  it("lets auditors open todo lists but hides quick write actions", async () => {
    const onNavigate = vi.fn();
    installFetch();
    render(<OverviewPage onNavigate={onNavigate} readOnly />);

    const failedTasks = (await screen.findByText("失败任务待处理")).closest(
      "li",
    );
    fireEvent.click(
      within(failedTasks as HTMLElement).getByRole("button", {
        name: "去查看",
      }),
    );
    expect(onNavigate).toHaveBeenCalledWith("failedGenerationRecords");
    expect(screen.queryByRole("button", { name: "去处理" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "开通套餐（已收款）" }),
    ).toBeNull();
  });

  it("shows an error banner when the summary request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );
    render(<OverviewPage />);

    expect(await screen.findByText(/network down/)).toBeInTheDocument();
  });

  it("首次读取失败给出重试，重试成功后展示数据与更新时间", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => summaryPayload,
      });
    vi.stubGlobal("fetch", fetchMock);
    render(<OverviewPage />);
    expect(await screen.findByText(/network down/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("328")).toBeInTheDocument();
    expect(screen.getByText(/数据更新于/)).toBeInTheDocument();
    expect(screen.queryByText(/network down/)).toBeNull();
  });

  it("刷新失败时保留上次数据，只局部提示", async () => {
    const fetchMock = installFetch();
    render(<OverviewPage />);
    expect(await screen.findByText("328")).toBeInTheDocument();
    fetchMock.mockImplementationOnce(() =>
      Promise.reject(new Error("gateway timeout")),
    );
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(
      await screen.findByText(/刷新失败，下方仍是上次成功读取的数据/),
    ).toBeInTheDocument();
    expect(screen.getByText("328")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "自动刷新：开（60 秒）" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("待办按业务域分组；告警不可用时显示「—」而不是 0", async () => {
    installFetch({
      ...summaryPayload,
      todos: {
        ...summaryPayload.todos,
        open_tickets: 4,
        queue_attention: 1,
        content_prepare_failed: 0,
        pending_orders: 0,
        active_alerts: null,
      },
    });
    const onNavigate = vi.fn();
    render(<OverviewPage onNavigate={onNavigate} />);
    const customer = await screen.findByRole("region", { name: "客户待办" });
    expect(within(customer).getByText("工单未结案")).toBeInTheDocument();
    fireEvent.click(within(customer).getByRole("button", { name: "去处理" }));
    expect(onNavigate).toHaveBeenCalledWith("openTickets");
    const system = screen.getByRole("region", { name: "系统待办" });
    expect(within(system).getByText("告警聚合暂不可用")).toBeInTheDocument();
    expect(within(system).getByText("—")).toBeInTheDocument();
    for (const group of ["资金", "生成", "内容"]) {
      expect(
        screen.getByRole("region", { name: `${group}待办` }),
      ).toBeInTheDocument();
    }
  });

  it("keeps unsettled and legacy costs unknown in cards and chart", async () => {
    const onNavigate = vi.fn();
    installFetch({
      ...summaryPayload,
      today: {
        ...summaryPayload.today,
        cost_fen: null,
        gross_fen: null,
        margin_pct: null,
        pending_operations: 1,
        legacy_cost_records: 2,
        legacy_settlements: 1,
      },
      trend: [{ day: "2026-09-05", succeeded: 1, failed: 0, cost_fen: null }],
      todos: {
        ...summaryPayload.todos,
        unknown_cost_records: 3,
      },
    });
    render(<OverviewPage onNavigate={onNavigate} />);
    const cost = await screen.findByRole("region", { name: "今日成本" });
    expect(cost).toHaveTextContent("待核对");
    expect(cost).not.toHaveTextContent("¥0.00");
    expect(screen.getByRole("region", { name: "今日毛利" })).toHaveTextContent(
      "待核对",
    );
    expect(screen.getByTitle("成本待核对")).toBeInTheDocument();
    expect(screen.queryByTitle("成本 ¥0.00")).not.toBeInTheDocument();
    expect(
      screen.getByText(/2 条历史成本、1 条历史结算待核对/),
    ).toBeInTheDocument();
    // P0-5 收口：KpiCard 的「待核对」不是一个终点，待办行要能一键落到
    // 条数同口径的 attention=unknown_cost 清单（AdminApp 三层映射已就绪）。
    const unknownCost = screen.getByText("今日成本待核对").closest("li");
    expect(unknownCost).toHaveTextContent("3");
    expect(unknownCost).toHaveTextContent("成本金额尚未确定");
    fireEvent.click(within(unknownCost as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("unknownCost");
  });
});
