import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  AdminActivationError,
  listRefunds,
  markRefundExecution,
  type RefundListItem,
} from "../api.admin";
import { RefundsPage } from "./RefundsPage";

vi.mock("../api.admin", () => ({
  listRefunds: vi.fn(),
  markRefundExecution: vi.fn(),
  AdminActivationError: class extends Error {
    constructor(
      message: string,
      readonly status?: number,
      readonly code?: string,
    ) {
      super(message);
    }
  },
}));

const refund: RefundListItem = {
  adjustment_id: "refund-1",
  target_user_id: "user-1",
  target_username: "fixture-customer",
  target_display_name: "",
  source_document_ref: "RF-001",
  original_recharge_order_no: "PAY-001",
  source_verified: true,
  reason: "退还原充值未用积分",
  created_at: "2026-10-12T00:00:00Z",
  admin_username: "fixture-admin",
  credits_deducted: 30,
  executed: false,
  channel: "",
  channel_label: "",
  provider_ref: "",
  executed_at: "",
  executed_by: "",
  note: "",
};

function loadItems(items: RefundListItem[]) {
  vi.mocked(listRefunds).mockResolvedValue({
    items,
    total: items.length,
    limit: 20,
    offset: 0,
  });
}

beforeEach(() => {
  vi.mocked(listRefunds).mockReset();
  vi.mocked(markRefundExecution).mockReset().mockResolvedValue({
    adjustment_id: refund.adjustment_id,
    executed: true,
  });
});

describe("历史退款原充值核对", () => {
  it.each([false, undefined])(
    "原充值未核对或旧响应缺字段时禁止登记，source_verified=%s",
    async (sourceVerified) => {
      loadItems([{ ...refund, source_verified: sourceVerified }]);
      render(<RefundsPage />);
      expect(
        await screen.findByText("未核对原充值来源，需人工核对"),
      ).toBeInTheDocument();
      const button = screen.getByRole("button", { name: "登记退付" });
      expect(button).toBeDisabled();
      fireEvent.click(button);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(markRefundExecution).not.toHaveBeenCalled();
    },
  );

  it("即使标记已核对，缺少原充值单号仍停止登记", async () => {
    loadItems([{ ...refund, original_recharge_order_no: " " }]);
    render(<RefundsPage />);
    expect(
      await screen.findByText("未核对原充值来源，需人工核对"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登记退付" })).toBeDisabled();
    expect(markRefundExecution).not.toHaveBeenCalled();
  });

  it("已核对原充值的待退付记录可以登记并在确认中显示原单", async () => {
    loadItems([refund]);
    render(<RefundsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "登记退付" }));
    const dialog = screen.getByRole("dialog", { name: "登记退付执行" });
    expect(dialog).toHaveTextContent("原充值订单号：PAY-001");
    fireEvent.change(within(dialog).getByLabelText("第三方退款单号"), {
      target: { value: "RETURN-001" },
    });
    fireEvent.change(within(dialog).getByLabelText("操作原因"), {
      target: { value: "已完成线下退付" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认登记" }));
    await waitFor(() => expect(markRefundExecution).toHaveBeenCalledTimes(1));
    expect(vi.mocked(markRefundExecution).mock.calls[0][0]).toMatchObject({
      adjustmentId: "refund-1",
      providerRef: "RETURN-001",
    });
  });

  it("历史已执行记录保留退付事实且仍提示来源待核对", async () => {
    loadItems([
      {
        ...refund,
        source_verified: false,
        executed: true,
        channel_label: "线下转账退回",
        provider_ref: "HISTORICAL-RETURN",
        executed_at: "2026-10-11T00:00:00Z",
        executed_by: "fixture-operator",
      },
    ]);
    render(<RefundsPage />);
    expect(
      await screen.findByText("未核对原充值来源，需人工核对"),
    ).toBeInTheDocument();
    expect(screen.getByText(/HISTORICAL-RETURN/)).toBeInTheDocument();
    expect(screen.getAllByText("已退付").length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "登记退付" }),
    ).not.toBeInTheDocument();
    expect(markRefundExecution).not.toHaveBeenCalled();
  });

  it("服务端拒绝旧来源时停止登记并标为待人工核对", async () => {
    loadItems([refund]);
    vi.mocked(markRefundExecution).mockRejectedValueOnce(
      new AdminActivationError(
        "需要人工核对原充值来源",
        422,
        "REFUND_SOURCE_REVIEW_REQUIRED",
      ),
    );
    render(<RefundsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "登记退付" }));
    fireEvent.change(screen.getByLabelText("第三方退款单号"), {
      target: { value: "RETURN-001" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "已完成线下退付" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认登记" }));
    expect(
      await screen.findByText("需要人工核对原充值来源"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登记退付" })).toBeDisabled();
    expect(
      screen.getByText("未核对原充值来源，需人工核对"),
    ).toBeInTheDocument();
    expect(markRefundExecution).toHaveBeenCalledTimes(1);
  });

  it("确认期间权限变为只读时不能登记", async () => {
    loadItems([refund]);
    const page = render(<RefundsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "登记退付" }));
    page.rerender(<RefundsPage readOnly />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(markRefundExecution).not.toHaveBeenCalled();
  });
});
