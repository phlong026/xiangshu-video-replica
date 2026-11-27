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
  reason_label: "任务结果迟迟未确认",
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
  next_step: "请人工核实任务结果。可在本页查询的视频会显示「查询视频结果」。",
};

function mockQueue(items: (typeof baseItem)[]) {
  vi.mocked(adminApi.adminRead).mockResolvedValue({
    items,
    total: items.length,
  });
}

describe("BillingReconciliationQueue（2026-11-15 审计补闭环）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("表头行只渲染一层 tr（裸 th，不再嵌套 tr）", async () => {
    mockQueue([baseItem]);
    render(<BillingReconciliationQueue readOnly={false} />);
    await screen.findByText("任务结果迟迟未确认");
    expect(screen.getByText("查询编号 task_VIDEO_x")).toBeInTheDocument();
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
    // 状态化供给：收口写入成功后的 reload 必须拿到 RESOLVED 行；
    // 用共享标志驱动，调用次数（StrictMode 双跑与否）不影响结果。
    let resolvedStage = false;
    const escalatedItems = { items: [baseItem], total: 1 };
    const resolvedItems = {
      items: [{ ...baseItem, state: "RESOLVED" as const }],
      total: 1,
    };
    vi.mocked(adminApi.adminRead).mockImplementation(async () =>
      resolvedStage ? resolvedItems : escalatedItems,
    );
    vi.mocked(adminApi.adminWrite).mockImplementation(async () => {
      resolvedStage = true;
      return {
        id: baseItem.id,
        state: "RESOLVED",
        resolution: "MANUAL_VERIFIED",
        billing_state: "FAILED",
      };
    });
    render(<BillingReconciliationQueue readOnly={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "完成核对" }));
    fireEvent.change(
      await screen.findByPlaceholderText("填写核对结果和凭证（必填）"),
      { target: { value: "已按失败核对，预留已释放" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "确认完成" }));
    await waitFor(() => {
      expect(adminApi.adminWrite).toHaveBeenCalledWith(
        "/api/control/billing-reconciliation/items/item-1/resolve",
        { note: "已按失败核对，预留已释放" },
        expect.any(String),
        expect.any(String),
      );
    });
    // 行级断言：必须是 tbody 里的 RESOLVED 行，而不是状态筛选下拉里的
    // 「已收口」选项（getByText 会撞上它）。
    await waitFor(() => {
      expect(
        document.querySelector("tbody tr[data-state='RESOLVED']"),
      ).not.toBeNull();
    });
  });

  it("筛选切换后丢弃过期响应（旧请求晚到不覆盖新列表）", async () => {
    // 首个请求（ALL）先发后至：持有 promise，等 OPEN 响应落地后再放行，
    // 界面必须仍显示 OPEN 的数据与总数。
    let releaseAll: (value: {
      items: (typeof baseItem)[];
      total: number;
    }) => void = () => {};
    const allGate = new Promise<{ items: (typeof baseItem)[]; total: number }>(
      (resolve) => {
        releaseAll = resolve;
      },
    );
    const openItems = {
      items: [{ ...baseItem, state: "OPEN" as const }],
      total: 7,
    };
    vi.mocked(adminApi.adminRead).mockImplementation(async (path: string) => {
      if (path.includes("state=OPEN")) return openItems;
      return await allGate;
    });
    render(<BillingReconciliationQueue readOnly={false} />);
    fireEvent.change(screen.getByDisplayValue("全部状态"), {
      target: { value: "OPEN" },
    });
    await screen.findByText("待核对");
    expect(screen.getByText("共 7 条（当前筛选）")).toBeInTheDocument();
    // 旧 ALL 响应此刻才返回：内容必须被丢弃。
    releaseAll({ items: [baseItem], total: 1 });
    await waitFor(() => {
      expect(screen.getByText("共 7 条（当前筛选）")).toBeInTheDocument();
    });
    // 行级断言（绕开筛选下拉里的同名选项）：没有 ESCALATED 行被渲染。
    expect(
      document.querySelector("tbody tr[data-state='ESCALATED']"),
    ).toBeNull();
  });

  it("计费仍未终态时展示后端 409 原文，不吞错误", async () => {
    const escalatedItems = { items: [baseItem], total: 1 };
    vi.mocked(adminApi.adminRead).mockImplementation(
      async () => escalatedItems,
    );
    vi.mocked(adminApi.adminWrite).mockRejectedValue(
      new Error(
        "这笔积分还未结算。请先确认任务结果并补齐凭证。需要人工调整积分时，请走审批。",
      ),
    );
    render(<BillingReconciliationQueue readOnly={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "完成核对" }));
    fireEvent.change(
      await screen.findByPlaceholderText("填写核对结果和凭证（必填）"),
      { target: { value: "口头说已处理" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "确认完成" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/这笔积分还未结算/);
    });
  });
});
