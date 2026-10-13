// T31：报表页月份输入的健壮性——空值/半成品/非法月份不得卸载页面。
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ReportsPage } from "./ReportsPage";

vi.mock("../api.admin", () => ({
  exportSummaryReportCsv: vi.fn(
    async () => ({ filename: "summary.csv" }) as { filename: string },
  ),
  exportBillingReportCsv: vi.fn(
    async () => ({ filename: "billing.csv" }) as { filename: string },
  ),
}));

function monthInput() {
  return screen.getByLabelText("报表月份") as HTMLInputElement;
}

// 带泛型才能读 .disabled；getAllByRole 默认返回 HTMLElement。
function exportButtons() {
  return screen.getAllByRole<HTMLButtonElement>("button", { name: "导出 CSV" });
}

test("默认本月：三张报表可导出", () => {
  render(<ReportsPage />);
  expect(monthInput().value).toMatch(/^\d{4}-\d{2}$/);
  const buttons = exportButtons();
  expect(buttons).toHaveLength(3);
  expect(buttons.every((button) => !button.disabled)).toBe(true);
});

test("清空月份不卸载页面：提示不完整且导出禁用", () => {
  render(<ReportsPage />);
  fireEvent.change(monthInput(), { target: { value: "" } });
  expect(screen.getByRole("alert")).toHaveTextContent("月份格式不完整");
  for (const button of exportButtons()) {
    expect(button.disabled).toBe(true);
  }
  // 页面骨架仍在（标题/输入都在），没有白屏。
  expect(screen.getByRole("heading", { name: "报表中心" })).toBeInTheDocument();
});

test("半成品与非法月份同样被拦下", () => {
  render(<ReportsPage />);
  fireEvent.change(monthInput(), { target: { value: "2026-" } });
  expect(screen.getByRole("alert")).toBeInTheDocument();
  fireEvent.change(monthInput(), { target: { value: "abc" } });
  expect(screen.getByRole("alert")).toBeInTheDocument();
  for (const button of exportButtons()) {
    expect(button.disabled).toBe(true);
  }
});

test("恢复合法值后导出恢复可用", () => {
  render(<ReportsPage />);
  fireEvent.change(monthInput(), { target: { value: "" } });
  fireEvent.change(monthInput(), { target: { value: "2026-01" } });
  expect(screen.queryByRole("alert")).toBeNull();
  for (const button of exportButtons()) {
    expect(button.disabled).toBe(false);
  }
});
