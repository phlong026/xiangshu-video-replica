import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { CustomerRefundSection } from "./CustomerRefundSection";

vi.mock("../api.admin", () => ({
  createCustomerAdjustment: vi.fn(),
  AdminActivationError: class extends Error {
    readonly status: number | undefined;
    constructor(message: string, status?: number) {
      super(message);
      this.name = "AdminActivationError";
      this.status = status;
    }
  },
}));

function fillForm({
  credits = "30",
  ref = "RF-0928-01",
  reason = "客户申请退还未用积分",
  originalOrderNo = "PAY-20261012-001",
}: {
  credits?: string;
  ref?: string;
  reason?: string;
  originalOrderNo?: string;
} = {}) {
  fireEvent.change(screen.getByLabelText("扣减积分"), {
    target: { value: credits },
  });
  fireEvent.change(screen.getByLabelText("原充值订单号"), {
    target: { value: originalOrderNo },
  });
  fireEvent.change(screen.getByLabelText("审批单号"), {
    target: { value: ref },
  });
  fireEvent.change(screen.getByLabelText("原因"), {
    target: { value: reason },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交退款扣减" }));
}

describe("CustomerRefundSection（P0-2 退款扣减迁入客户详情）", () => {
  beforeEach(() => {
    vi.mocked(adminApi.createCustomerAdjustment).mockReset();
  });

  it("blocks amounts above the available balance before calling the API", () => {
    render(
      <CustomerRefundSection
        availableCredits={20}
        onRefunded={vi.fn()}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm({ credits: "30" });
    expect(screen.getByText(/最多可扣减 20 积分/)).toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
  });

  it("requires an approval reference to align with the offline refund", () => {
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={vi.fn()}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm({ ref: "  " });
    expect(screen.getByText(/请填写审批单号/)).toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
  });

  it("sends a negative reversal and reports the new balance", async () => {
    const onRefunded = vi.fn();
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValue({
      request_id: "req-1",
      wallet_balance_after: 70,
    } as Awaited<ReturnType<typeof adminApi.createCustomerAdjustment>>);
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={onRefunded}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm();
    const dialog = await screen.findByRole("dialog", { name: "确认退款扣减" });
    expect(dialog).toHaveTextContent("扣减 30 积分");
    expect(dialog).toHaveTextContent("原充值订单号：PAY-20261012-001");
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));

    await waitFor(() => expect(onRefunded).toHaveBeenCalled());
    const [userId, input, reason] = vi.mocked(adminApi.createCustomerAdjustment)
      .mock.calls[0];
    expect(userId).toBe("user-1");
    expect(input).toEqual({
      sourceDocumentType: "REFUND_APPROVAL",
      sourceDocumentRef: "RF-0928-01",
      originalRechargeOrderNo: "PAY-20261012-001",
      credits: -30,
    });
    expect(reason).toBe("客户申请退还未用积分");
    expect(await screen.findByText(/余额 70 积分/)).toBeInTheDocument();
    expect(screen.getByLabelText("原充值订单号")).toHaveValue("");
    // 扣减成功后提示去资金中心登记退付，两边互相可达（T40）。
    expect(
      screen.getByRole("link", { name: "去资金中心登记退付执行" }),
    ).toHaveAttribute("href", "#admin/funds?sub=refunds");
  });

  it("reuses the idempotency key after an uncertain failure", async () => {
    vi.mocked(adminApi.createCustomerAdjustment)
      .mockRejectedValueOnce(new Error("网络超时"))
      .mockResolvedValueOnce({
        request_id: "req-2",
        wallet_balance_after: 70,
      } as Awaited<ReturnType<typeof adminApi.createCustomerAdjustment>>);
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={vi.fn()}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm();
    await screen.findByRole("dialog", { name: "确认退款扣减" });
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    expect(await screen.findByText(/结果未确认/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));

    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(2),
    );
    const keys = vi
      .mocked(adminApi.createCustomerAdjustment)
      .mock.calls.map((call) => call[3]);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("shows a read-only note to auditors", () => {
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={vi.fn()}
        readOnly
        userId="user-1"
      />,
    );
    expect(
      screen.getByText("审计员仅可查看，不能扣减积分。"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("扣减积分")).toBeNull();
  });
});

describe("原充值绑定和中断保护", () => {
  const result = {
    request_id: "req-source",
    wallet_balance_after: 70,
    original_recharge_order_no: "PAY-20261012-001",
  } as Awaited<ReturnType<typeof adminApi.createCustomerAdjustment>>;

  beforeEach(() => {
    vi.mocked(adminApi.createCustomerAdjustment)
      .mockReset()
      .mockResolvedValue(result);
  });

  function refundSection(
    userId = "user-1",
    onRefunded = vi.fn(),
    readOnly = false,
  ) {
    return (
      <CustomerRefundSection
        userId={userId}
        availableCredits={100}
        readOnly={readOnly}
        onRefunded={onRefunded}
      />
    );
  }

  it.each(["REFUND_APPROVAL", "LEDGER_CORRECTION"])(
    "%s 必须填写原充值订单号",
    (sourceType) => {
      render(refundSection());
      fireEvent.change(screen.getByLabelText("依据"), {
        target: { value: sourceType },
      });
      fillForm({ originalOrderNo: "  " });
      expect(screen.getByText(/请填写原充值订单号/)).toBeInTheDocument();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
    },
  );

  it("说明总余额只是上限且原单来源和余额由服务端核对", () => {
    render(refundSection());
    expect(screen.getByText(/钱包可用余额仅是整体上限/)).toBeInTheDocument();
    expect(screen.getByText(/原充值可退积分由服务端核对/)).toBeInTheDocument();
    expect(
      screen.getByText(/赠送、预留、已消费及其他订单积分不能扣回/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/来源无法核对或原单余额不足时暂停.*人工核对/),
    ).toBeInTheDocument();
  });

  it("确认显示并发送去除首尾空格的原充值单，不混用审批单号", async () => {
    render(refundSection());
    fillForm({ originalOrderNo: "  PAY-20261012-001  " });
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "原充值订单号：PAY-20261012-001",
    );
    expect(screen.getByRole("dialog")).toHaveTextContent("RF-0928-01");
    expect(screen.getByLabelText("原充值订单号")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(1),
    );
    expect(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0][1],
    ).toMatchObject({
      originalRechargeOrderNo: "PAY-20261012-001",
      sourceDocumentRef: "RF-0928-01",
    });
  });

  it("取消确认保留原充值单，修改后必须重新确认", async () => {
    render(refundSection());
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByLabelText("原充值订单号")).toHaveValue(
      "PAY-20261012-001",
    );
    fillForm({ originalOrderNo: "PAY-OTHER" });
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "原充值订单号：PAY-OTHER",
    );
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValueOnce({
      ...result,
      original_recharge_order_no: "PAY-OTHER",
    });
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(1),
    );
    expect(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0][1]
        .originalRechargeOrderNo,
    ).toBe("PAY-OTHER");
  });

  it.each([
    "原充值来源无法核对，请人工核对",
    "原充值可退积分不足，请人工核对",
    "原充值包含赠送或套餐积分，请人工核对",
  ])("确定拒绝后保留单号并停止扣减：%s", async (message) => {
    vi.mocked(adminApi.createCustomerAdjustment).mockRejectedValueOnce(
      new adminApi.AdminActivationError(message, 422),
    );
    const onRefunded = vi.fn();
    render(refundSection("user-1", onRefunded));
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("原充值订单号")).toHaveValue(
      "PAY-20261012-001",
    );
    expect(screen.getByLabelText("原充值订单号")).toBeEnabled();
    expect(onRefunded).not.toHaveBeenCalled();
    expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(1);
  });

  it("未知结果关闭弹窗后锁定完整请求，再次提交复用原充值及幂等键", async () => {
    vi.mocked(adminApi.createCustomerAdjustment).mockRejectedValueOnce(
      new Error("响应丢失"),
    );
    render(refundSection());
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await screen.findByText(/结果未确认/);
    const firstCall = vi.mocked(adminApi.createCustomerAdjustment).mock
      .calls[0];
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    for (const label of [
      "原充值订单号",
      "扣减积分",
      "审批单号",
      "原因",
      "依据",
    ]) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }
    fireEvent.click(screen.getByRole("button", { name: "提交退款扣减" }));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "原充值订单号：PAY-20261012-001",
    );
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(2),
    );
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[1]).toEqual(
      firstCall,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("原充值订单号")).toHaveValue(""),
    );
  });

  it("同一渲染周期反复提交确认只发出一次请求", async () => {
    let resolve: ((value: typeof result) => void) | undefined;
    vi.mocked(adminApi.createCustomerAdjustment).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    render(refundSection());
    fillForm();
    const dialog = screen.getByRole("dialog");
    act(() => {
      fireEvent.submit(dialog);
      fireEvent.submit(dialog);
    });
    expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(1);
    await act(async () => resolve?.(result));
  });

  it("切换客户撤销尚未提交的旧确认，不会将原单扣到新客户", () => {
    const page = render(refundSection());
    fillForm();
    page.rerender(refundSection("user-2"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("原充值订单号")).toHaveValue("");
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
  });

  it("旧客户请求迟到成功不会更新新客户的界面或回调", async () => {
    let resolve: ((value: typeof result) => void) | undefined;
    vi.mocked(adminApi.createCustomerAdjustment).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const onRefunded = vi.fn();
    const page = render(refundSection("user-1", onRefunded));
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    page.rerender(refundSection("user-2", onRefunded));
    await act(async () => resolve?.(result));
    expect(onRefunded).not.toHaveBeenCalled();
    expect(screen.queryByText(/已从原充值订单/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("原充值订单号")).toHaveValue("");
  });

  it("同一挂载内切回原客户后恢复未知结果请求，不换用其他客户的重试键", async () => {
    vi.mocked(adminApi.createCustomerAdjustment).mockRejectedValueOnce(
      new Error("响应丢失"),
    );
    const page = render(refundSection());
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await screen.findByText(/结果未确认/);
    const firstCall = vi.mocked(adminApi.createCustomerAdjustment).mock
      .calls[0];
    page.rerender(refundSection("user-2"));
    fillForm({ originalOrderNo: "PAY-CUSTOMER-2" });
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValueOnce({
      ...result,
      original_recharge_order_no: "PAY-CUSTOMER-2",
    });
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    page.rerender(refundSection("user-1"));
    expect(screen.getByLabelText("原充值订单号")).toHaveValue(
      "PAY-20261012-001",
    );
    expect(screen.getByLabelText("原充值订单号")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "提交退款扣减" }));
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(3),
    );
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[2]).toEqual(
      firstCall,
    );
    expect(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[1][3],
    ).not.toBe(firstCall[3]);
  });

  it("切换成只读角色后旧确认不能写入", () => {
    const page = render(refundSection());
    fillForm();
    page.rerender(refundSection("user-1", vi.fn(), true));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
  });

  it("成功回包的原充值单不符时保留原请求并提示人工核对", async () => {
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValueOnce({
      ...result,
      original_recharge_order_no: "PAY-UNEXPECTED",
    });
    const onRefunded = vi.fn();
    render(refundSection("user-1", onRefunded));
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    expect(
      await screen.findByText(/返回的原充值订单号不一致.*人工核对/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("原充值订单号")).toHaveValue(
      "PAY-20261012-001",
    );
    expect(onRefunded).not.toHaveBeenCalled();
  });
});
