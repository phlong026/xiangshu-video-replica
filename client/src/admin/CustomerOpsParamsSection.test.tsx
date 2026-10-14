import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CustomerOpsParamsSection } from "./CustomerOpsParamsSection";

const getParams = vi.fn();
const updateParams = vi.fn();

vi.mock("../api.admin", () => ({
  adminActivationErrorMessage: (cause: unknown, fallback: string) =>
    cause instanceof Error ? cause.message : fallback,
  getCustomerOpsParams: () => getParams(),
  updateCustomerOpsParams: (input: unknown) => updateParams(input),
}));

const current = {
  low_balance_threshold: 100,
  health_alert_score: 40,
  rule: "两个页面共用这份参数。",
};

describe("CustomerOpsParamsSection", () => {
  beforeEach(() => {
    getParams.mockReset();
    updateParams.mockReset();
  });

  it("展示当前参数，改值后经原因确认保存", async () => {
    getParams.mockResolvedValue(current);
    updateParams.mockResolvedValue({
      ...current,
      low_balance_threshold: 300,
    });
    render(<CustomerOpsParamsSection />);

    const balance = await screen.findByLabelText("低余额阈值（积分）");
    expect(balance).toHaveValue("100");
    expect(screen.getByRole("button", { name: "保存运营参数" })).toBeDisabled();

    fireEvent.change(balance, { target: { value: "300" } });
    fireEvent.click(screen.getByRole("button", { name: "保存运营参数" }));
    fireEvent.change(await screen.findByLabelText(/原因/), {
      target: { value: "套餐调整后一次生成约 300 积分" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() =>
      expect(updateParams).toHaveBeenCalledWith(
        expect.objectContaining({
          lowBalanceThreshold: 300,
          healthAlertScore: 40,
          reason: "套餐调整后一次生成约 300 积分",
        }),
      ),
    );
    expect(await screen.findByText(/已同步使用新口径/)).toBeInTheDocument();
  });

  it("越界输入在前端拦下，不发请求", async () => {
    getParams.mockResolvedValue(current);
    render(<CustomerOpsParamsSection />);

    fireEvent.change(await screen.findByLabelText("健康分预警线（分）"), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存运营参数" }));

    expect(
      await screen.findByText("健康分预警线需为 1 到 100 之间的整数"),
    ).toBeInTheDocument();
    expect(updateParams).not.toHaveBeenCalled();
  });

  it("只读账号只看到数值，没有表单", async () => {
    getParams.mockResolvedValue(current);
    render(<CustomerOpsParamsSection readOnly />);

    expect(await screen.findByText("100 积分")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存运营参数" })).toBeNull();
  });
});
