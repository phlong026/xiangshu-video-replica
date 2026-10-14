import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReportsPage } from "./ReportsPage";

const exportSummary = vi.fn();
const exportBilling = vi.fn();
const listRecent = vi.fn();

vi.mock("../api.admin", () => ({
  exportSummaryReportCsv: (input: unknown) => exportSummary(input),
  exportBillingReportCsv: (input: unknown) => exportBilling(input),
  listRecentExports: () => listRecent(),
}));

describe("ReportsPage", () => {
  beforeEach(() => {
    exportSummary.mockReset().mockResolvedValue({ filename: "x.csv" });
    exportBilling.mockReset().mockResolvedValue({ filename: "y.csv.gz" });
    listRecent.mockReset().mockResolvedValue({ items: [] });
  });

  it("季度模式按自然季度起止导出（第 4 季度 = 10-01 至 12-31）", async () => {
    render(<ReportsPage />);
    fireEvent.change(screen.getByLabelText("区间类型"), {
      target: { value: "quarter" },
    });
    fireEvent.change(screen.getByLabelText("年份"), {
      target: { value: "2026" },
    });
    fireEvent.change(screen.getByLabelText("季度"), { target: { value: "4" } });
    expect(
      screen.getByText("当前区间：2026-10-01 至 2026-12-31（共 92 天）"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "导出 CSV" })[0]);
    await waitFor(() =>
      expect(exportSummary).toHaveBeenCalledWith({
        kind: "business",
        start_date: "2026-10-01",
        end_date: "2026-12-31",
      }),
    );
    // 季度长度（92 天）账务明细仍可导出。
    fireEvent.click(screen.getAllByRole("button", { name: "导出 CSV" })[2]);
    await waitFor(() =>
      expect(exportBilling).toHaveBeenCalledWith({
        start_date: "2026-10-01",
        end_date: "2026-12-31",
      }),
    );
  });

  it("自定义区间：未选完、倒置、超长都给出原因并禁用；超过 92 天只禁用账务明细", () => {
    render(<ReportsPage />);
    fireEvent.change(screen.getByLabelText("区间类型"), {
      target: { value: "custom" },
    });
    expect(screen.getByText("请先选定报表区间。")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: "导出 CSV" }))
      expect(button).toBeDisabled();

    fireEvent.change(screen.getByLabelText("开始日期"), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText("结束日期"), {
      target: { value: "2026-06-30" },
    });
    const buttons = screen.getAllByRole("button", { name: "导出 CSV" });
    expect(buttons[0]).toBeEnabled();
    expect(buttons[1]).toBeEnabled();
    expect(buttons[2]).toBeDisabled();
    expect(
      screen.getByText("账务明细区间最长 92 天，请缩短区间。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("结束日期"), {
      target: { value: "2028-06-30" },
    });
    expect(screen.getByText("区间最长 366 天。")).toBeInTheDocument();
  });

  it("列出最近导出记录（谁、何时、范围），导出成功后刷新", async () => {
    listRecent.mockResolvedValue({
      items: [
        {
          at: "2026-10-14T03:00:00Z",
          actor: "财务甲",
          kind: "summary_funds",
          label: "资金报表",
          filters: { start: "2026-10-01", end: "2026-10-31" },
        },
      ],
    });
    render(<ReportsPage />);
    expect(
      await screen.findByText(/财务甲 · 资金报表 · 2026-10-01 至 2026-10-31/),
    ).toBeInTheDocument();
    const before = listRecent.mock.calls.length;
    fireEvent.click(screen.getAllByRole("button", { name: "导出 CSV" })[1]);
    await waitFor(() => expect(listRecent.mock.calls.length).toBe(before + 1));
  });

  it("清空或半成品的月份不卸载页面：给出原因并禁用导出", () => {
    render(<ReportsPage />);
    const input = screen.getByLabelText("报表月份");
    for (const value of ["", "2026-", "abc", "2026-13"]) {
      // type="month" 会把非法值收敛成空串；半成品与非法值都不得走到
      // Date.UTC(NaN) → toISOString() 的 RangeError——那会卸载整棵管理端树
      // （main 侧修过同一处事故，T42 重写本页时把这道防护一并带过来）。
      fireEvent.change(input, { target: { value } });
      expect(screen.getByText("请先选定报表区间。")).toBeInTheDocument();
      for (const button of screen.getAllByRole("button", { name: "导出 CSV" }))
        expect(button).toBeDisabled();
    }
    fireEvent.change(input, { target: { value: "2026-01" } });
    expect(
      screen.getByText("当前区间：2026-01-01 至 2026-01-31（共 31 天）"),
    ).toBeInTheDocument();
  });
});
