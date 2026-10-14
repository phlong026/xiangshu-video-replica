import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BatchAnnotateDialog } from "./BatchAnnotateDialog";

const batchAnnotate = vi.fn();

vi.mock("../api.admin", () => ({
  batchAnnotateCustomers: (...args: unknown[]) => batchAnnotate(...args),
  listCustomerTags: async () => ({ items: [{ tag: "VIP", count: 3 }] }),
  listCustomerOwnerCandidates: async () => ({
    items: [{ user_id: "ops", username: "ops", display_name: "运营甲" }],
  }),
}));

const targets = [
  { user_id: "u1", username: "甲店" },
  { user_id: "u2", username: "乙店" },
];

describe("BatchAnnotateDialog", () => {
  beforeEach(() => batchAnnotate.mockReset());

  it("增量加标签并指定负责人，全部成功后关闭", async () => {
    batchAnnotate.mockResolvedValue({ results: [], succeeded: 2, failed: 0 });
    const onClose = vi.fn();
    const onFinished = vi.fn();
    render(
      <BatchAnnotateDialog
        open
        targets={targets}
        onClose={onClose}
        onFinished={onFinished}
      />,
    );
    fireEvent.change(screen.getByLabelText(/加标签/), {
      target: { value: "续费关注，重点" },
    });
    fireEvent.change(await screen.findByRole("combobox", { name: /负责人/ }), {
      target: { value: "ops" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "月度整理" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(batchAnnotate).toHaveBeenCalledWith(
      {
        userIds: ["u1", "u2"],
        addTags: ["续费关注", "重点"],
        removeTags: [],
        setOwner: true,
        ownerUserId: "ops",
      },
      "月度整理",
      expect.any(String),
    );
    expect(onFinished).toHaveBeenCalled();
  });

  it("什么都没填时本地拦截", async () => {
    render(
      <BatchAnnotateDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "月度整理" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    expect(
      await screen.findByText("请至少填写一项：加标签、去标签或负责人。"),
    ).toBeInTheDocument();
    expect(batchAnnotate).not.toHaveBeenCalled();
  });

  it("部分失败时保留对话框并列出失败客户", async () => {
    batchAnnotate.mockResolvedValue({
      results: [
        { user_id: "u1", ok: true, message: "" },
        {
          user_id: "u2",
          ok: false,
          message: "叠加后标签超过 10 个，请先清理。",
        },
      ],
      succeeded: 1,
      failed: 1,
    });
    const onClose = vi.fn();
    render(
      <BatchAnnotateDialog
        open
        targets={targets}
        onClose={onClose}
        onFinished={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/加标签/), {
      target: { value: "新标签" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "整理" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));

    expect(await screen.findByText("成功 1 / 失败 1")).toBeInTheDocument();
    expect(screen.getByText(/乙店：叠加后标签超过 10 个/)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});
