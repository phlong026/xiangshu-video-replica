import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearPendingRegeneration,
  type PendingRegeneration,
  readPendingRegeneration,
  savePendingRegeneration,
} from "./regenerationPending";

const originalLocks = Object.getOwnPropertyDescriptor(navigator, "locks");

function installSerialLocks() {
  const queues = new Map<string, Promise<void>>();
  const request = vi.fn((name: string, callback: () => unknown) => {
    const previous = queues.get(name) ?? Promise.resolve();
    const result = previous.then(callback);
    queues.set(
      name,
      result.then(
        () => undefined,
        () => undefined,
      ),
    );
    return result;
  });
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: { request },
  });
  return request;
}

function pending(
  overrides: Partial<PendingRegeneration> = {},
): PendingRegeneration {
  return {
    kind: "task",
    sourceId: "task-original",
    batchId: "batch-original",
    account: "account-a",
    actionKey: "paid-task:task-original",
    request: {
      idempotency_key: "regeneration-round-a",
      generation_reason: "用户已确认原报价",
      quote_token: "q-old",
      max_credits: 12,
      estimated_cost_snapshot: 120,
    },
    quote: {
      quote_token: "q-old",
      quote_expires_at: 1_000,
      max_credits: 12,
      estimated_credits: 12,
      resolution: "768P",
      duration_seconds: 6,
      quantity: 1,
      unit_price_fen_per_second: 20,
      estimated_seconds: 6,
      estimated_price_fen: 120,
      unit_credits: 2,
      credit_price_version: 3,
      discount_rate: "0.9000",
      discount_source: "recharge_package",
    },
    submitted: true,
    ...overrides,
  };
}

function read(value: PendingRegeneration) {
  return readPendingRegeneration(value.account, value.kind, value.sourceId);
}

function nextRound(value: PendingRegeneration): PendingRegeneration {
  return {
    ...value,
    request: {
      ...value.request,
      idempotency_key: "regeneration-round-b",
      quote_token: "q-new",
      max_credits: 30,
    },
    quote: {
      ...value.quote,
      quote_token: "q-new",
      max_credits: 30,
      estimated_credits: 30,
      credit_price_version: 4,
    },
  };
}

const contractChanges: {
  label: string;
  request: Partial<PendingRegeneration["request"]>;
  quote?: Partial<PendingRegeneration["quote"]>;
}[] = [
  {
    label: "报价凭证",
    request: { quote_token: "q-next" },
    quote: { quote_token: "q-next" },
  },
  {
    label: "积分上限",
    request: { max_credits: 30 },
    quote: { max_credits: 30, estimated_credits: 30 },
  },
  { label: "生成理由", request: { generation_reason: "另一次已确认的生成" } },
  { label: "预计费用", request: { estimated_cost_snapshot: 300 } },
];

describe("付费重新生成待核对请求的持久化边界", () => {
  beforeEach(() => {
    localStorage.clear();
    installSerialLocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    if (originalLocks) {
      Object.defineProperty(navigator, "locks", originalLocks);
    } else {
      Reflect.deleteProperty(navigator, "locks");
    }
  });

  it("刷新后保留原幂等键和完整冻结报价，过期报价也不会变成新轮次", async () => {
    const value = pending();
    const frozen = structuredClone(value);
    await savePendingRegeneration(value);
    // 模拟离开旧页面后的内存变化；恢复只能以已保存的原请求为准。
    value.request.idempotency_key = "unsubmitted-new-key";
    value.quote.max_credits = 99;
    vi.resetModules();
    const refreshed = await import("./regenerationPending");
    const restored = refreshed.readPendingRegeneration(
      frozen.account,
      frozen.kind,
      frozen.sourceId,
    );
    expect(restored).toEqual(frozen);
    expect(restored?.request.idempotency_key).toBe("regeneration-round-a");
    expect(restored?.quote.quote_expires_at).toBe(1_000);
    expect(restored?.quote.max_credits).toBe(12);
    expect(restored?.quote.credit_price_version).toBe(3);
  });

  it("同一来源的请求按账号隔离，清理旧账号不影响新账号", async () => {
    const accountA = pending();
    const accountB = nextRound(pending({ account: "account-b" }));
    await savePendingRegeneration(accountA);
    expect(read(accountB)).toBeNull();
    await savePendingRegeneration(accountB);
    expect(read(accountA)).toEqual(accountA);
    expect(read(accountB)).toEqual(accountB);
    await clearPendingRegeneration(accountA);
    expect(read(accountA)).toBeNull();
    expect(read(accountB)).toEqual(accountB);
  });

  it("批次、单条和不同来源各自保留待核对请求", async () => {
    const task = pending();
    const batch = nextRound(pending({ kind: "batch" }));
    const otherTask = nextRound(pending({ sourceId: "task-other" }));
    await Promise.all(
      [task, batch, otherTask].map((value) => savePendingRegeneration(value)),
    );
    expect(read(task)).toEqual(task);
    expect(read(batch)).toEqual(batch);
    expect(read(otherTask)).toEqual(otherTask);
    expect(localStorage.length).toBe(3);
  });

  it("重复保存同一轮次不会新建操作身份", async () => {
    const value = pending();
    await savePendingRegeneration(value);
    await savePendingRegeneration(structuredClone(value));
    expect(read(value)).toEqual(value);
    expect(localStorage.length).toBe(1);
  });

  it.each(contractChanges)(
    "同键但$label改变时不能覆盖已有冻结合同",
    async ({ request, quote }) => {
      const original = pending();
      const changed = {
        ...original,
        request: { ...original.request, ...request },
        quote: { ...original.quote, ...quote },
      };
      await savePendingRegeneration(original);
      const writes = vi.spyOn(Storage.prototype, "setItem");
      expect(changed.request.idempotency_key).toBe(
        original.request.idempotency_key,
      );
      await expect(savePendingRegeneration(changed)).rejects.toThrow(
        "已有待核对请求",
      );
      expect(writes).not.toHaveBeenCalled();
      expect(read(original)).toEqual(original);
    },
  );

  it.each(contractChanges)(
    "同键新合同$label改变后，迟到旧响应不能清除新合同",
    async ({ request, quote }) => {
      const original = pending();
      const changed = {
        ...original,
        request: { ...original.request, ...request },
        quote: { ...original.quote, ...quote },
      };
      await savePendingRegeneration(original);
      await clearPendingRegeneration(original);
      await savePendingRegeneration(changed);
      const removals = vi.spyOn(Storage.prototype, "removeItem");
      expect(changed.request.idempotency_key).toBe(
        original.request.idempotency_key,
      );
      await clearPendingRegeneration(original);
      expect(removals).not.toHaveBeenCalled();
      expect(read(changed)).toEqual(changed);
      await clearPendingRegeneration(changed);
      expect(read(changed)).toBeNull();
    },
  );

  it("迟到的旧响应不能清除已经保存的新轮次", async () => {
    const locks = installSerialLocks();
    const original = pending();
    const newer = nextRound(original);
    await savePendingRegeneration(original);
    await clearPendingRegeneration(original);
    await savePendingRegeneration(newer);
    await clearPendingRegeneration(original);
    expect(read(newer)).toEqual(newer);
    expect(locks).toHaveBeenCalledTimes(4);
    expect(new Set(locks.mock.calls.map(([name]) => name)).size).toBe(1);
  });

  it("新轮次保存和旧响应清理同时排队时也不会丢失新请求", async () => {
    const locks = installSerialLocks();
    const original = pending();
    const newer = nextRound(original);
    await savePendingRegeneration(original);
    await clearPendingRegeneration(original);
    await Promise.all([
      savePendingRegeneration(newer),
      clearPendingRegeneration(original),
    ]);
    expect(read(newer)).toEqual(newer);
    expect(locks).toHaveBeenCalledTimes(4);
    expect(new Set(locks.mock.calls.map(([name]) => name)).size).toBe(1);
  });

  it("两个窗口同时为同一来源保存不同键时只接受一个请求", async () => {
    const locks = installSerialLocks();
    const original = pending();
    const competing = nextRound(original);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    const results = await Promise.allSettled([
      savePendingRegeneration(original),
      savePendingRegeneration(competing),
    ]);
    expect(results.map((result) => result.status)).toEqual([
      "fulfilled",
      "rejected",
    ]);
    if (results[1].status !== "rejected") throw new Error("第二个请求应被拒绝");
    expect(results[1].reason).toEqual(
      expect.objectContaining({
        message: expect.stringContaining("已有待核对请求"),
      }),
    );
    expect(read(original)).toEqual(original);
    expect(writes).toHaveBeenCalledOnce();
    expect(locks).toHaveBeenCalledTimes(2);
    expect(locks.mock.calls[0][0]).toBe(locks.mock.calls[1][0]);
  });

  it("读取存储被拒绝时不能把未知结果当作没有旧请求", async () => {
    const value = pending();
    await savePendingRegeneration(value);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("synthetic-storage-denied");
    });
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(() => read(value)).toThrow("暂不新建付费任务");
    await expect(savePendingRegeneration(nextRound(value))).rejects.toThrow(
      "暂不新建付费任务",
    );
    expect(writes).not.toHaveBeenCalled();
  });

  it.each(["抛出异常", "静默丢弃"])(
    "写入存储%s时拒绝继续提交",
    async (mode) => {
      const value = pending();
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        if (mode === "抛出异常") throw new Error("synthetic-storage-full");
      });
      await expect(savePendingRegeneration(value)).rejects.toThrow();
      expect(read(value)).toBeNull();
    },
  );

  it.each(["抛出异常", "静默丢弃"])(
    "删除存储%s时仍保留待核对记录并报告失败",
    async (mode) => {
      const value = pending();
      await savePendingRegeneration(value);
      vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
        if (mode === "抛出异常") throw new Error("synthetic-storage-denied");
      });
      await expect(clearPendingRegeneration(value)).rejects.toThrow(
        "暂不新建付费任务",
      );
      expect(read(value)).toEqual(value);
    },
  );

  it("缺少跨窗口锁时保存和清理均失败关闭", async () => {
    const value = pending();
    await savePendingRegeneration(value);
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: undefined,
    });
    const writes = vi.spyOn(Storage.prototype, "setItem");
    const removals = vi.spyOn(Storage.prototype, "removeItem");
    await expect(savePendingRegeneration(nextRound(value))).rejects.toThrow(
      "暂不新建付费任务",
    );
    await expect(clearPendingRegeneration(value)).rejects.toThrow(
      "暂不新建付费任务",
    );
    expect(writes).not.toHaveBeenCalled();
    expect(removals).not.toHaveBeenCalled();
    expect(read(value)).toEqual(value);
  });

  it.each([
    ["非 JSON", "{"],
    ["缺失内容", "null"],
    ["串号记录", JSON.stringify(pending({ account: "account-b" }))],
    ["未提交记录", JSON.stringify(pending({ submitted: false }))],
    [
      "报价凭证不一致",
      JSON.stringify(
        pending({
          request: { ...pending().request, quote_token: "q-other" },
        }),
      ),
    ],
    [
      "报价上限不一致",
      JSON.stringify(
        pending({ request: { ...pending().request, max_credits: 99 } }),
      ),
    ],
    [
      "显示金额不一致",
      JSON.stringify(
        pending({ quote: { ...pending().quote, estimated_credits: 99 } }),
      ),
    ],
  ])("%s不会被当作空记录而覆盖", async (_name, corrupt) => {
    const value = pending();
    await savePendingRegeneration(value);
    const storageKey = localStorage.key(0);
    if (!storageKey) throw new Error("应已保存原请求");
    localStorage.setItem(storageKey, corrupt);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(() => read(value)).toThrow("暂不新建付费任务");
    await expect(savePendingRegeneration(nextRound(value))).rejects.toThrow(
      "暂不新建付费任务",
    );
    expect(writes).not.toHaveBeenCalled();
    expect(localStorage.getItem(storageKey)).toBe(corrupt);
  });
});
