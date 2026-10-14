import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminGenerationRecord } from "../api.admin";
import { BatchCompensateDialog } from "./BatchCompensateDialog";

const createAdjustment = vi.fn();

vi.mock("../api.admin", () => ({
  createCustomerAdjustment: (...args: unknown[]) => createAdjustment(...args),
}));

function record(id: string, user: string, credits: number) {
  return {
    record_id: id,
    user_id: user,
    username: `${user}-name`,
    charged_credits: credits,
  } as AdminGenerationRecord;
}

describe("BatchCompensateDialog", () => {
  beforeEach(() => createAdjustment.mockReset());

  it("原扣 0 积分的记录被跳过；部分失败可只重试失败的记录且沿用原幂等键", async () => {
    createAdjustment.mockImplementation(async (userId: string) => {
      if (userId === "u2" && createAdjustment.mock.calls.length === 2)
        throw new Error("钱包暂时不可用");
      return {};
    });
    const onClose = vi.fn();
    const onFinished = vi.fn();
    render(
      <BatchCompensateDialog
        open
        records={[
          record("r1", "u1", 5),
          record("r2", "u2", 7),
          record("r0", "u0", 0),
        ]}
        onClose={onClose}
        onFinished={onFinished}
      />,
    );
    expect(
      screen.getByText(/另有 1 条原扣 0 积分，已跳过/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "补偿故障" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认补偿" }));

    expect(await screen.findByText("成功 1 / 失败 1")).toBeInTheDocument();
    const firstKey = createAdjustment.mock.calls.find(
      (c) => c[0] === "u2",
    )?.[3];
    fireEvent.click(screen.getByRole("button", { name: /重试失败部分（1）/ }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const retries = createAdjustment.mock.calls.slice(2);
    expect(retries.map((c) => c[0])).toEqual(["u2"]);
    expect(retries[0][3]).toBe(firstKey);
    expect(onFinished).toHaveBeenCalled();
  });
});
