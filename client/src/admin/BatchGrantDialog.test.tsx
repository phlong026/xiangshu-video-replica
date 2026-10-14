import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BatchGrantDialog } from "./BatchGrantDialog";

const createAdjustment = vi.fn();

vi.mock("../api.admin", () => ({
  createCustomerAdjustment: (...args: unknown[]) => createAdjustment(...args),
}));

const targets = [
  { user_id: "u1", username: "甲店" },
  { user_id: "u2", username: "乙店" },
  { user_id: "u3", username: "丙店" },
];

function fill() {
  fireEvent.change(screen.getByLabelText(/每户赠送积分/), {
    target: { value: "50" },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "十月激活活动" },
  });
}

describe("BatchGrantDialog", () => {
  beforeEach(() => createAdjustment.mockReset());

  it("部分失败显示成功 N / 失败 M，只重试失败部分且沿用原幂等键", async () => {
    createAdjustment.mockImplementation(async (userId: string) => {
      if (userId === "u2" && createAdjustment.mock.calls.length <= 3)
        throw new Error("钱包暂时不可用");
      return {};
    });
    const onFinished = vi.fn();
    const onClose = vi.fn();
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={onClose}
        onFinished={onFinished}
      />,
    );
    fill();
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));

    expect(await screen.findByText("成功 2 / 失败 1")).toBeInTheDocument();
    expect(screen.getByText(/乙店：钱包暂时不可用/)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    const firstKeyForU2 = createAdjustment.mock.calls.find(
      (call) => call[0] === "u2",
    )?.[3];

    fireEvent.click(screen.getByRole("button", { name: /重试失败部分（1）/ }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    // 重试只打失败的一户，且幂等键与第一次相同。
    const retries = createAdjustment.mock.calls.slice(3);
    expect(retries.map((call) => call[0])).toEqual(["u2"]);
    expect(retries[0][3]).toBe(firstKeyForU2);
    expect(onFinished).toHaveBeenCalledWith(["u1", "u2", "u3"]);
  });

  it("金额不是正整数时不发请求（浏览器原生校验先拦下）", () => {
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/每户赠送积分/), {
      target: { value: "0" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "原因" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    expect(createAdjustment).not.toHaveBeenCalled();
  });
});
