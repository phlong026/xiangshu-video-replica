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
  },
  trend: [
    { day: "2026-08-30", succeeded: 210, failed: 28, cost_fen: 39000 },
    { day: "2026-09-05", succeeded: 302, failed: 12, cost_fen: 48620 },
  ],
  todos: {
    pending_pairings: 3,
    failed_tasks_7d: 5,
    reconciliation_problems: 2,
    expiring_codes_7d: 0,
    unconfigured_rates: 2,
    unknown_cost_records: 0,
  },
  device_slots: { bound: 918, total: 1024 },
};

function installFetch() {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes("/api/control/dashboard/summary")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => summaryPayload,
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
    expect(screen.getByText("918 / 1024")).toBeInTheDocument();
    expect(screen.getByText("近 7 日生成与成本")).toBeInTheDocument();
    expect(screen.getByText("成本（元）")).toBeInTheDocument();
    expect(screen.getByText("成功生成数（条）")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /图标/ })).toHaveLength(7);
    expect(screen.getByText("资金账务核对")).toBeInTheDocument();
    expect(screen.queryByText("点击下钻资金流水")).toBeNull();

    // 待办计数
    expect(screen.getByText("待批准配对")).toBeInTheDocument();
    expect(screen.getByText("对账不一致")).toBeInTheDocument();
    // 即将过期激活码为 0 时不提供「去处理」
    const expiring = screen.getByText("即将过期激活码").closest("li");
    expect(expiring?.querySelector("button")).toBeNull();

    // 快捷操作跳转
    fireEvent.click(screen.getByRole("button", { name: "快速发码" }));
    expect(onNavigate).toHaveBeenCalledWith("issueCodes");
    fireEvent.click(screen.getByRole("button", { name: "后台加款" }));
    expect(onNavigate).toHaveBeenCalledWith("customerAdjustments");
    fireEvent.click(screen.getByRole("button", { name: "发放免费秒数" }));
    expect(onNavigate).toHaveBeenCalledWith("customerAdjustments");
    fireEvent.click(screen.getByRole("button", { name: "成本核对" }));
    expect(onNavigate).toHaveBeenCalledWith("costDetails");

    const pendingPairings = screen.getByText("待批准配对").closest("li");
    fireEvent.click(within(pendingPairings as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("codes");
    const missingRates = screen.getByText("费率未配置科目").closest("li");
    fireEvent.click(within(missingRates as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("rates");
    const failedTasks = screen.getByText("失败任务待处理").closest("li");
    fireEvent.click(within(failedTasks as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("failedGenerationRecords");
  });

  it("shows an error banner when the summary request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );
    render(<OverviewPage />);

    expect(await screen.findByText(/network down/)).toBeInTheDocument();
  });
});
