import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import {
  type AdjustmentWriteResult,
  type CustomerLifecycleResponse,
  createCustomerAdjustment,
  createIdempotencyKey,
  listCustomerLifecycle,
  readCustomerRetention,
} from "../api.admin";
import { CustomerLifecyclePage } from "./CustomerLifecyclePage";

vi.mock("../api.admin", () => ({
  createCustomerAdjustment: vi.fn(),
  createIdempotencyKey: vi.fn(),
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

beforeEach(() => {
  localStorage.clear();
  vi.mocked(createCustomerAdjustment).mockReset().mockResolvedValue(granted);
  vi.mocked(listCustomerLifecycle).mockReset().mockResolvedValue(customers);
  vi.mocked(readCustomerRetention).mockReset().mockRejectedValue(new Error());
  let nextId = 0;
  vi.mocked(createIdempotencyKey)
    .mockReset()
    .mockImplementation(() => `test-operation-${++nextId}`);
});

async function openGrant(source = "十月激活活动") {
  await screen.findByRole("table", { name: "客户生命周期清单" });
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户A" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户B" }));
  fireEvent.click(screen.getByRole("button", { name: "批量赠送（已选 2）" }));
  fireEvent.change(screen.getByLabelText("发放单号（可追溯）"), {
    target: { value: source },
  });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "十月客户激活" },
  });
}

async function interruptAtB() {
  vi.mocked(createCustomerAdjustment)
    .mockResolvedValueOnce(granted)
    .mockRejectedValueOnce(new Error("网络中断"));
  await openGrant();
  fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
  await waitFor(() =>
    expect(screen.getAllByText(/发放中断：1\/2 完成/).length).toBeGreaterThan(
      0,
    ),
  );
}

test("部分成功后只续发未完成客户，冻结积分、凭证、原因及逐户标识", async () => {
  const page = render(<CustomerLifecyclePage />);
  await interruptAtB();
  const originalB = vi.mocked(createCustomerAdjustment).mock.calls[1];
  expect(originalB[1]).toMatchObject({
    sourceDocumentType: "FREE_GRANT",
    sourceDocumentRef: "十月激活活动",
    credits: 100,
    operationId: expect.any(String),
  });
  expect(originalB[3]).toEqual(expect.any(String));
  expect(
    vi.mocked(createCustomerAdjustment).mock.calls[0][1].operationId,
  ).not.toBe(originalB[1].operationId);
  expect(screen.getByLabelText("每户赠送积分（正整数）")).toBeDisabled();
  expect(screen.getByLabelText("发放单号（可追溯）")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户A" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户C" }));
  page.rerender(<CustomerLifecyclePage onCustomer={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: /继续未完成批次/ }));
  const dialog = screen.getByRole("dialog", { name: "继续批量赠送积分" });
  expect(within(dialog).getByLabelText("原操作原因")).toHaveValue(
    "十月客户激活",
  );
  expect(within(dialog).getByLabelText("原操作原因")).toHaveAttribute(
    "readonly",
  );
  fireEvent.click(
    within(dialog).getByRole("button", { name: "继续发放未完成客户" }),
  );
  await waitFor(() =>
    expect(createCustomerAdjustment).toHaveBeenCalledTimes(3),
  );
  expect(vi.mocked(createCustomerAdjustment).mock.calls[2]).toEqual(originalB);
  expect(
    await screen.findByText("已向 2 名客户各赠送 100 积分。"),
  ).toBeInTheDocument();
  expect(localStorage.length).toBe(0);
});

test("刷新后恢复原批次，成功客户不再发放且新选择不改变目标", async () => {
  const page = render(<CustomerLifecyclePage />);
  await interruptAtB();
  const originalB = vi.mocked(createCustomerAdjustment).mock.calls[1];
  page.unmount();
  render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户C" }));
  expect(screen.queryByRole("button", { name: /批量赠送（已选/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /继续未完成批次/ }));
  fireEvent.click(screen.getByRole("button", { name: "继续发放未完成客户" }));
  await waitFor(() =>
    expect(createCustomerAdjustment).toHaveBeenCalledTimes(3),
  );
  expect(vi.mocked(createCustomerAdjustment).mock.calls[2]).toEqual(originalB);
  expect(
    await screen.findByText("已向 2 名客户各赠送 100 积分。"),
  ).toBeInTheDocument();
});

test("空单号只生成一个批次凭证，响应丢失后刷新仍使用同一操作及幂等键", async () => {
  vi.mocked(createCustomerAdjustment).mockRejectedValueOnce(
    new Error("响应丢失"),
  );
  const page = render(<CustomerLifecyclePage />);
  await openGrant("");
  fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
  await waitFor(() =>
    expect(createCustomerAdjustment).toHaveBeenCalledTimes(1),
  );
  await waitFor(() =>
    expect(screen.getAllByText(/发放中断/).length).toBeGreaterThan(0),
  );
  const originalA = vi.mocked(createCustomerAdjustment).mock.calls[0];
  expect(originalA[3]).toEqual(expect.any(String));
  page.unmount();
  render(<CustomerLifecyclePage />);
  fireEvent.click(
    await screen.findByRole("button", { name: /继续未完成批次/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "继续发放未完成客户" }));
  await waitFor(() =>
    expect(createCustomerAdjustment).toHaveBeenCalledTimes(3),
  );
  expect(vi.mocked(createCustomerAdjustment).mock.calls[1]).toEqual(originalA);
  expect(
    vi.mocked(createCustomerAdjustment).mock.calls[2][1].sourceDocumentRef,
  ).toBe(originalA[1].sourceDocumentRef);
});

test("无法持久化时在首笔写入之前停止，避免刷新后重复发放", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("存储空间不足");
  });
  render(<CustomerLifecyclePage />);
  await openGrant();
  fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
  await waitFor(() =>
    expect(screen.getAllByText(/无法保存批次恢复记录/).length).toBeGreaterThan(
      0,
    ),
  );
  expect(createCustomerAdjustment).not.toHaveBeenCalled();
});

test("保存成功进度失败时停止后续客户，同页继续仍跳过已成功客户", async () => {
  const write = Storage.prototype.setItem;
  let writes = 0;
  const storage = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(function (this: Storage, key: string, value: string) {
      writes += 1;
      if (writes === 2) throw new Error("存储空间不足");
      write.call(this, key, value);
    });
  render(<CustomerLifecyclePage />);
  await openGrant();
  fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
  await waitFor(() =>
    expect(screen.getAllByText(/无法保存批次恢复记录/).length).toBeGreaterThan(
      0,
    ),
  );
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(1);
  storage.mockRestore();
  fireEvent.click(screen.getByRole("button", { name: "继续发放未完成客户" }));
  await waitFor(() =>
    expect(createCustomerAdjustment).toHaveBeenCalledTimes(2),
  );
  expect(
    vi.mocked(createCustomerAdjustment).mock.calls.map((call) => call[0]),
  ).toEqual(["user-A", "user-B"]);
});

test("损坏的恢复记录保留并阻止新批次，不静默丢弃发放记录", async () => {
  const key = "admin:customer-grant-batch:v1:http://127.0.0.1:8000";
  localStorage.setItem(key, "{invalid-json");
  render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  expect(screen.getByText(/无法读取批次恢复记录/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户A" }));
  expect(screen.queryByRole("button", { name: /批量赠送（已选/ })).toBeNull();
  expect(createCustomerAdjustment).not.toHaveBeenCalled();
  expect(localStorage.getItem(key)).toBe("{invalid-json");
});

test("发放过程中重复提交仍只运行一轮", async () => {
  let finishFirst: ((result: AdjustmentWriteResult) => void) | undefined;
  vi.mocked(createCustomerAdjustment).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishFirst = resolve;
      }),
  );
  render(<CustomerLifecyclePage />);
  await openGrant();
  const form = screen.getByRole("dialog", { name: "批量赠送积分" });
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(1);
  finishFirst?.(granted);
  await screen.findByText("已向 2 名客户各赠送 100 积分。");
  expect(
    vi.mocked(createCustomerAdjustment).mock.calls.map((call) => call[0]),
  ).toEqual(["user-A", "user-B"]);
});

test("只读角色可以查看原批次状态但不能恢复发放", async () => {
  const page = render(<CustomerLifecyclePage />);
  await interruptAtB();
  page.unmount();
  render(<CustomerLifecyclePage readOnly />);
  expect(await screen.findByText(/已成功客户不会重复发放/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /继续未完成批次/ })).toBeNull();
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(2);
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

test("最后清理记录失败后刷新只清理完成记录，不重复发放", async () => {
  const remove = vi
    .spyOn(Storage.prototype, "removeItem")
    .mockImplementation(() => {
      throw new Error("暂时无法清理记录");
    });
  const page = render(<CustomerLifecyclePage />);
  await openGrant();
  fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
  await waitFor(() =>
    expect(screen.getAllByText(/发放中断：2\/2 完成/).length).toBeGreaterThan(
      0,
    ),
  );
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(2);
  page.unmount();
  remove.mockRestore();
  render(<CustomerLifecyclePage />);
  fireEvent.click(
    await screen.findByRole("button", { name: /继续未完成批次/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "继续发放未完成客户" }));
  await screen.findByText("已向 2 名客户各赠送 100 积分。");
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(2);
  expect(localStorage.length).toBe(0);
});

test("其他服务端的批次不会在当前服务端恢复", async () => {
  const page = render(<CustomerLifecyclePage />);
  await interruptAtB();
  page.unmount();
  vi.stubEnv("VITE_API_BASE_URL", "https://other-api.example.test");
  render(<CustomerLifecyclePage />);
  await screen.findByRole("table", { name: "客户生命周期清单" });
  fireEvent.click(screen.getByRole("checkbox", { name: "选择 客户C" }));
  expect(
    screen.getByRole("button", { name: "批量赠送（已选 1）" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /继续未完成批次/ })).toBeNull();
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(2);
  expect(localStorage.length).toBe(1);
});

test("组件未卸载时切换服务端也不能把原批次发到新环境", async () => {
  render(<CustomerLifecyclePage />);
  await interruptAtB();
  vi.stubEnv("VITE_API_BASE_URL", "https://other-api.example.test");
  fireEvent.click(screen.getByRole("button", { name: "继续发放未完成客户" }));
  await waitFor(() =>
    expect(screen.getAllByText(/服务端地址已更改/).length).toBeGreaterThan(0),
  );
  expect(createCustomerAdjustment).toHaveBeenCalledTimes(2);
  expect(localStorage.length).toBe(1);
});

test("超出服务端整数范围的积分不会建立无法完成的批次", async () => {
  render(<CustomerLifecyclePage />);
  await openGrant();
  fireEvent.change(screen.getByLabelText("每户赠送积分（正整数）"), {
    target: { value: "2147483648" },
  });
  fireEvent.submit(screen.getByRole("dialog", { name: "批量赠送积分" }));
  expect(
    screen.getAllByText(/赠送积分必须是 1 至 2147483647 的整数/).length,
  ).toBeGreaterThan(0);
  expect(createCustomerAdjustment).not.toHaveBeenCalled();
  expect(localStorage.length).toBe(0);
});
