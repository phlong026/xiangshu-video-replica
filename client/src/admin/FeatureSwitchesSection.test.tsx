import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FeatureSwitchesSection } from "./FeatureSwitchesSection";

const readEnabled = vi.fn();
const setEnabled = vi.fn();

vi.mock("../api.admin", () => ({
  readPublishStatsEnabled: () => readEnabled(),
  setPublishStatsEnabled: (input: unknown) => setEnabled(input),
}));

describe("FeatureSwitchesSection", () => {
  beforeEach(() => {
    readEnabled.mockReset();
    setEnabled.mockReset();
  });

  it("默认关闭时显示未开启，写权限可开启并通知外壳", async () => {
    readEnabled.mockResolvedValue({ enabled: false });
    setEnabled.mockResolvedValue({ enabled: true });
    const onChanged = vi.fn();
    render(<FeatureSwitchesSection onChanged={onChanged} />);

    expect(await screen.findByText("未开启（默认）")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开启回流" }));
    fireEvent.change(await screen.findByLabelText(/原因/), {
      target: { value: "已确认采集授权口径" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认" }));

    await waitFor(() =>
      expect(setEnabled).toHaveBeenCalledWith({
        enabled: true,
        reason: "已确认采集授权口径",
      }),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(true));
    expect(await screen.findByText("已开启")).toBeInTheDocument();
  });

  it("只读账号看得到状态但没有切换按钮", async () => {
    readEnabled.mockResolvedValue({ enabled: true });
    render(<FeatureSwitchesSection readOnly />);

    expect(await screen.findByText("已开启")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "关闭回流" })).toBeNull();
  });

  it("读取失败时给出错误提示", async () => {
    readEnabled.mockRejectedValue(new Error("读取功能开关失败"));
    render(<FeatureSwitchesSection />);

    expect(await screen.findByText("读取功能开关失败")).toBeInTheDocument();
  });
});
