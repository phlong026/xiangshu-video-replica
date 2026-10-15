import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import {
  type AdjustmentWriteResult,
  type CustomerLifecycleResponse,
  createCustomerAdjustment,
  listCustomerLifecycle,
  readCustomerRetention,
} from "../api.admin";
import { apiBaseUrl } from "../apiBase";
import { batchStorageKey } from "./batchAdjustments";
import { CustomerLifecyclePage } from "./CustomerLifecyclePage";

vi.mock("../api.admin", () => ({
  createCustomerAdjustment: vi.fn(),
  listCustomerLifecycle: vi.fn(),
  readCustomerRetention: vi.fn(),
}));

const customers: CustomerLifecycleResponse = {
  items: ["A", "B", "C"].map((name) => ({
    user_id: `user-${name}`,
    username: `客户${name}`,
    display_name: "",
    created_at: "2026-10-01T00:00:00Z",
    last_active_at: "2026-10-01T00:00:00Z",
    available_credits: 0,
    recharge_total_fen: 0,
    succeeded_30d: 0,
    failed_30d: 0,
    stage: "dormant",
    health_score: 30,
  })),
  distribution: [],
  lifecycleRule: "生命周期规则",
  healthRule: "健康分规则",
  // 客户运营参数（T29）：页面据这两项标红健康分与低余额，夹具给中性值。
  healthAlertScore: 40,
  lowBalanceThreshold: 100,
  limit: 20,
  offset: 0,
};
const granted: AdjustmentWriteResult = {
  adjustment_id: "adjustment-test",
  order_id: "order-test",
  credits: "100",
  amount_fen: "0",
  pricing_scope: "FREE_GRANT",
  wallet_balance_after: 100,
  source_document_type: "FREE_GRANT",
  source_document_ref: "十月激活活动",
  request_id: "request-test",
};

function pendingBatchRecord(doneFirst: boolean) {
  return {
    version: 1,
    id: "batch-1",
    apiBase: apiBaseUrl(),
    sourceDocumentRef: "十月激活活动",
    credits: 100,
    reason: "十月客户激活",
    targets: [
      {
        userId: "user-A",
        username: "客户A",
        operationId: "op-a",
        idempotencyKey: "key-a",
        done: doneFirst,
      },
      {
        userId: "user-B",
        username: "客户B",
        operationId: "op-b",
        idempotencyKey: "key-b",
        done: false,
      },
    ],
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(createCustomerAdjustment).mockReset().mockResolvedValue(granted);
  vi.mocked(listCustomerLifecycle).mockReset().mockResolvedValue(customers);
  vi.mocked(readCustomerRetention).mockReset().mockRejectedValue(new Error());
});

test("勾选客户后才出现批量赠送入口，只读角色没有入口", async () => {
  const page = render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  expect(screen.queryByRole("button", { name: /批量赠送/ })).toBeNull();
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户A" }));
  expect(
    screen.getByRole("button", { name: "批量赠送（已选 1）" }),
  ).toBeInTheDocument();

  page.unmount();
  render(<CustomerLifecyclePage readOnly />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  expect(screen.queryByRole("button", { name: /批量赠送/ })).toBeNull();
});

test("有未完成批次时给出横幅与继续入口；只读角色只能看不能续发", async () => {
  localStorage.setItem(
    batchStorageKey(),
    JSON.stringify(pendingBatchRecord(true)),
  );
  const page = render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  expect(document.body.textContent).toContain("有未完成的发放批次");
  expect(
    screen.getByRole("button", { name: "继续未完成批次" }),
  ).toBeInTheDocument();

  page.unmount();
  render(<CustomerLifecyclePage readOnly />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  expect(document.body.textContent).toContain("有未完成的发放批次");
  expect(screen.queryByRole("button", { name: "继续未完成批次" })).toBeNull();
});

test("从待续批次继续：只发未完成客户、沿用原标识，完成后清理记录并刷新", async () => {
  localStorage.setItem(
    batchStorageKey(),
    JSON.stringify(pendingBatchRecord(true)),
  );
  render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  fireEvent.click(screen.getByRole("button", { name: "继续未完成批次" }));
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "继续发放" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: /继续发放未完成客户（1）/ }),
  );

  await waitFor(() =>
    expect(createCustomerAdjustment).toHaveBeenCalledTimes(1),
  );
  const [userId, body, , key] = vi.mocked(createCustomerAdjustment).mock
    .calls[0] as unknown as [string, { operationId: string }, string, string];
  expect(userId).toBe("user-B");
  expect(body.operationId).toBe("op-b");
  expect(key).toBe("key-b");
  expect(localStorage.getItem(batchStorageKey())).toBeNull();
  // onFinished 收到的是整批 id（含此前已完成的客户），提示按整批计数。
  expect(
    await screen.findByText(/已向 2 名客户赠送积分，列表已刷新/),
  ).toBeInTheDocument();
});

test("恢复记录损坏时页面照常渲染、不静默丢弃记录，新发放由对话框拦下", async () => {
  localStorage.setItem(batchStorageKey(), "{invalid-json");
  render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  expect(screen.queryByRole("button", { name: "继续未完成批次" })).toBeNull();
  expect(
    screen.queryByRole("button", { name: /批量赠送（已选 0）/ }),
  ).toBeNull();

  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户A" }));
  fireEvent.click(screen.getByRole("button", { name: "批量赠送（已选 1）" }));
  expect(await screen.findByText(/无法读取批次恢复记录/)).toBeInTheDocument();
  expect(createCustomerAdjustment).not.toHaveBeenCalled();
  // 损坏的内容必须原样留着，等人工核对，不能被自动清掉。
  expect(localStorage.getItem(batchStorageKey())).toBe("{invalid-json");
});

test("调账适配器发送稳定业务标识及幂等键，并兼容未提供业务标识的旧调用", async () => {
  const actual =
    await vi.importActual<typeof import("../api.admin")>("../api.admin");
  const { setAdminCsrfToken, clearAdminCsrfToken } = await import("../api");
  const fetchRequest = vi.spyOn(globalThis, "fetch").mockImplementation(
    async () =>
      new Response(JSON.stringify(granted), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  setAdminCsrfToken("test-admin-csrf");
  try {
    const input = {
      sourceDocumentType: "FREE_GRANT",
      sourceDocumentRef: "十月激活活动",
      credits: 100,
    };
    await actual.createCustomerAdjustment(
      "user-A",
      { ...input, operationId: "batch-A-operation" },
      "原操作原因",
      "stable-key-A",
    );
    const request = fetchRequest.mock.calls[0][1];
    expect(JSON.parse(String(request?.body))).toMatchObject({
      operation_id: "batch-A-operation",
      source_document_ref: "十月激活活动",
      confirm: true,
      reason: "原操作原因",
    });
    expect(new Headers(request?.headers).get("Idempotency-Key")).toBe(
      "stable-key-A",
    );
    await actual.createCustomerAdjustment(
      "user-B",
      input,
      "旧调用原因",
      "stable-key-B",
    );
    expect(
      JSON.parse(String(fetchRequest.mock.calls[1][1]?.body)),
    ).not.toHaveProperty("operation_id");
  } finally {
    clearAdminCsrfToken();
  }
});
