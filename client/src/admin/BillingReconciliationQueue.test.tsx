// 卡单核对队列回归（2026-11-15 审计修复）：
// 1) 表头必须是裸 <th>——此前把整个 <tr> 塞进 DataTable 再包一层 <tr>，
//    渲染出非法的 <tr><tr><th> 嵌套，浏览器把单元格甩出表头行（列错位）。
// 2) 行内「收口」：输入备注后走标准 adminWrite 信封（confirm/reason 由
//    api 层注入），后端 409 时把原文展示出来，不吞错误。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as adminApi from "../api.admin";
import { BillingReconciliationQueue } from "./BillingReconciliationQueue";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
  adminWrite: vi.fn(),
}));

const baseItem = {
  id: "item-1",
  service: "video_768p",
  source_kind: "generation",
  source_id: "gen-1",
  billing_round: 1,
  owner_user_id: "user-1",
  reason: "UNKNOWN_TIMEOUT",
  reason_label: "提交结果未知超时，需人工核对供应商实况",
  detail: {},
  provider_receipt_id: "task_VIDEO_x",
  state: "ESCALATED" as const,
  check_count: 12,
  last_checked_at: null,
  escalated_at: null,
  resolution: null,
  resolution_note: null,
  resolved_by: null,
  resolved_at: null,
  created_at: "2026-09-30T02:33:46+00:00",
  updated_at: "2026-11-15T10:00:00+00:00",
  next_step: "已升级人工：请先用专项对账接口核对供应商实况，再回到这里收口。",
};

function mockQueue(items: typeof baseItem[]) {
  vi.mocked(adminApi.adminRead).mockResolvedValue({ items, total: items.length });
}

describe("BillingReconciliationQueue（2026-11-15 审计补闭环）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("表头行只渲染一层 tr（裸 th，不再嵌套 tr）", async () => {
    mockQueue([baseItem]);
    render(<BillingReconciliationQueue readOnly={false} />);
    await screen.findByText("已升级人工");
    const headRows = document.querySelectorAll("thead tr");
    expect(headRows).toHaveLength(1);
    // 非法嵌套：<tr> 直接出现在 <tr> 里
    expect(document.querySelectorAll("thead tr tr")).toHaveLength(0);
    expect(headRows[0].querySelectorAll("th")).toHaveLength(8);
  });

  it("状态筛选会把 state 传给队列接口", async () => {
    mockQueue([]);
    render(<BillingReconciliationQueue readOnly={false} />);
    await screen.findByText("当前筛选下没有卡单记录。");
    fireEvent.change(screen.getByDisplayValue("全部状态"), {
      target: { value: "ESCALATED" },
    });
    await waitFor(() => {
      expect(adminApi.adminRead).toHaveBeenLastCalledWith(
        expect.stringContaining("state=ESCALATED"),
        expect.any(String),
      );
    });
  });

  it("收口成功后刷新队列", async () => {
    const resolvedItem = { ...baseItem, state: "RESOLVED" as const };
    // StrictMode 会双跑挂载 effect：先供两条初始（ESCALATED），收口后的
    // reload 才拿到 RESOLVED——按序供给，不依赖调用次数。
    vi.mocked(adminApi.adminRead)
      .mockResolvedValueOnce({ items: [baseItem], total: 1 })
      .mockResolvedValueOnce({ items: [baseItem], total: 1 })
      .mockResolvedValue({ items: [resolvedItem], total: 1 });
    vi.mocked(adminApi.adminWrite).mockResolvedValue({
      id: baseItem.id,
      state: "RESOLVED",
      resolution: "MANUAL_VERIFIED",
      billing_state: "FAILED",
    });
    render(<BillingReconciliationQueue readOnly={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "收口" }));
    fireEvent.change(
      await screen.findByPlaceholderText("核对凭据/结论备注（必填）"),
      { target: { value: "已按失败核对，预留已释放" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "确认收口" }));
    await waitFor(() => {
      expect(adminApi.adminWrite).toHaveBeenCalledWith(
        "/api/control/billing-reconciliation/items/item-1/resolve",
        { note: "已按失败核对，预留已释放" },
        expect.any(String),
        expect.any(String),
      );
    });
    await waitFor(() => {
      expect(screen.getByText("已收口")).toBeInTheDocument();
    });
  });

  it("计费仍未终态时展示后端 409 原文，不吞错误", async () => {
    // 同上：按序供两条初始响应，StrictMode 双跑也稳定。
    vi.mocked(adminApi.adminRead)
      .mockResolvedValueOnce({ items: [baseItem], total: 1 })
      .mockResolvedValueOnce({ items: [baseItem], total: 1 })
      .mockResolvedValue({ items: [baseItem], total: 1 });
    vi.mocked(adminApi.adminWrite).mockRejectedValue(
      new Error(
        "计费仍未收口：请先用专项对账接口核对供应商实况或补齐计费证据；涉及真实调账须走调账审批，本队列不能直接退分。",
      ),
    );
    render(<BillingReconciliationQueue readOnly={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "收口" }));
    fireEvent.change(
      await screen.findByPlaceholderText("核对凭据/结论备注（必填）"),
      { target: { value: "口头说已处理" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "确认收口" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/计费仍未收口/);
    });
  });
});
