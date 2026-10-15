import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiBaseUrl } from "../apiBase";
import { BatchGrantDialog } from "./BatchGrantDialog";
import { batchStorageKey } from "./batchAdjustments";

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
  beforeEach(() => {
    createAdjustment.mockReset();
    localStorage.clear();
  });

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

  it("首次发放即落盘恢复记录，完成清除；operationId 逐户唯一且重试沿用", async () => {
    createAdjustment.mockImplementation(async (userId: string) => {
      if (userId === "u2" && createAdjustment.mock.calls.length <= 3)
        throw new Error("网络超时");
      return {};
    });
    const onFinished = vi.fn();
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={onFinished}
      />,
    );
    fill();
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    await screen.findByText("成功 2 / 失败 1");

    const stored = JSON.parse(
      localStorage.getItem(batchStorageKey()) ?? "null",
    ) as { targets: { userId: string; operationId: string; done: boolean }[] };
    expect(
      stored.targets
        .filter((target) => target.done)
        .map((target) => target.userId)
        .sort(),
    ).toEqual(["u1", "u3"]);
    const operations = createAdjustment.mock.calls.map(
      (call) => (call[1] as { operationId: string }).operationId,
    );
    expect(new Set(operations).size).toBe(operations.length);

    fireEvent.click(screen.getByRole("button", { name: /重试失败部分（1）/ }));
    await waitFor(() => expect(onFinished).toHaveBeenCalled());
    const retry = createAdjustment.mock.calls.at(-1) as unknown as [
      string,
      { operationId: string },
      string,
      string,
    ];
    expect(retry[0]).toBe("u2");
    expect(retry[1].operationId).toBe(
      stored.targets.find((target) => target.userId === "u2")?.operationId,
    );
    // 全部完成后恢复记录必须清掉，否则下次会误续一批已完成的发放。
    expect(localStorage.getItem(batchStorageKey())).toBeNull();
  });

  it("有恢复记录时按记录续发：只发未完成客户、沿用原标识、不接受新勾选", async () => {
    localStorage.setItem(
      batchStorageKey(),
      JSON.stringify({
        version: 1,
        id: "batch-1",
        apiBase: apiBaseUrl(),
        sourceDocumentRef: "十月激活活动",
        credits: 50,
        reason: "十月激活活动",
        targets: [
          {
            userId: "u1",
            username: "甲店",
            operationId: "op-1",
            idempotencyKey: "key-1",
            done: true,
          },
          {
            userId: "u2",
            username: "乙店",
            operationId: "op-2",
            idempotencyKey: "key-2",
            done: false,
          },
        ],
      }),
    );
    createAdjustment.mockResolvedValue({});
    const onClose = vi.fn();
    render(
      <BatchGrantDialog
        open
        targets={[{ user_id: "u9", username: "这次新勾的店" }]}
        onClose={onClose}
        onFinished={vi.fn()}
      />,
    );

    expect(await screen.findByText(/还有 1 名客户未发放/)).toBeInTheDocument();
    // 记录里的金额与单号是请求哈希的一部分，续发时不允许改。
    expect(screen.getByLabelText(/每户赠送积分/)).toBeDisabled();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "继续发放" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /继续发放未完成客户（1）/ }),
    );

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(createAdjustment).toHaveBeenCalledTimes(1);
    const [userId, body, , key] = createAdjustment.mock.calls[0] as [
      string,
      { operationId: string; credits: number },
      string,
      string,
    ];
    expect(userId).toBe("u2");
    expect(body.operationId).toBe("op-2");
    expect(body.credits).toBe(50);
    expect(key).toBe("key-2");
    expect(localStorage.getItem(batchStorageKey())).toBeNull();
  });

  it("恢复记录写不进去时，首笔发送之前就停下", async () => {
    const spy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("quota exceeded");
      });
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    fill();
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    expect(await screen.findByText(/无法保存批次恢复记录/)).toBeInTheDocument();
    expect(createAdjustment).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("发放中途写不进进度就中断整批，已成功客户在记录里保持 done", async () => {
    const original = Storage.prototype.setItem;
    let writes = 0;
    const spy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(function (this: Storage, key: string, value: string) {
        writes += 1;
        // 1 = 整批记录，2 = 第一户进度；从第三户的进度开始写不进去。
        if (writes > 2) throw new Error("quota exceeded");
        original.call(this, key, value);
      });
    createAdjustment.mockResolvedValue({});
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    fill();
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));

    expect(await screen.findByText(/无法保存批次恢复记录/)).toBeInTheDocument();
    expect(createAdjustment).toHaveBeenCalledTimes(2);
    const stored = JSON.parse(
      localStorage.getItem(batchStorageKey()) ?? "null",
    ) as { targets: { userId: string; done: boolean }[] };
    expect(
      stored.targets
        .filter((target) => target.done)
        .map((target) => target.userId),
    ).toEqual(["u1"]);
    spy.mockRestore();
  });

  it("恢复记录损坏时报可处置的错，不新建批次也不覆盖记录", async () => {
    localStorage.setItem(batchStorageKey(), "{invalid-json");
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    expect(await screen.findByText(/无法读取批次恢复记录/)).toBeInTheDocument();
    fill();
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    expect(createAdjustment).not.toHaveBeenCalled();
    expect(localStorage.getItem(batchStorageKey())).toBe("{invalid-json");
  });

  it("其他服务端的批次不会在当前服务端恢复", async () => {
    localStorage.setItem(
      "admin:customer-grant-batch:v1:https://other-api.example.test",
      JSON.stringify({
        version: 1,
        id: "batch-other",
        apiBase: "https://other-api.example.test",
        sourceDocumentRef: "别的环境的活动",
        credits: 100,
        reason: "别的环境",
        targets: [
          {
            userId: "u1",
            username: "甲店",
            operationId: "op-1",
            idempotencyKey: "key-1",
            done: false,
          },
        ],
      }),
    );
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    expect(screen.queryByText(/未完成的发放批次/)).toBeNull();
    expect(
      screen.getByRole("button", { name: "确认发放" }),
    ).toBeInTheDocument();
  });

  it("对话框打开期间切换服务端：续发被拦下，原批次不会发到新环境", async () => {
    localStorage.setItem(
      batchStorageKey(),
      JSON.stringify({
        version: 1,
        id: "batch-1",
        apiBase: apiBaseUrl(),
        sourceDocumentRef: "十月激活活动",
        credits: 50,
        reason: "十月激活活动",
        targets: [
          {
            userId: "u2",
            username: "乙店",
            operationId: "op-2",
            idempotencyKey: "key-2",
            done: false,
          },
        ],
      }),
    );
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    await screen.findByText(/未完成的发放批次/);
    vi.stubEnv("VITE_API_BASE_URL", "https://other-api.example.test");
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "继续发放" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /继续发放未完成客户（1）/ }),
    );
    expect(await screen.findByText(/服务端地址已更改/)).toBeInTheDocument();
    expect(createAdjustment).not.toHaveBeenCalled();
    vi.unstubAllEnvs();
  });

  it("超出服务端整数上限的积分不建立批次", async () => {
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/每户赠送积分/), {
      target: { value: "2147483648" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "原因" },
    });
    // number 输入带 max，浏览器原生校验会先挡住按钮点击；这里直接提交表单，
    // 验证的是对话框自己的校验（原生校验不是资金安全的最后一道）。
    fireEvent.submit(screen.getByRole("dialog", { name: "批量赠送积分" }));
    expect(
      await screen.findByText(/赠送积分必须是 1 至 2147483647 的正整数/),
    ).toBeInTheDocument();
    expect(createAdjustment).not.toHaveBeenCalled();
    expect(localStorage.getItem(batchStorageKey())).toBeNull();
  });

  it("未填单号时全批共用一个批次凭证，重试也沿用", async () => {
    createAdjustment.mockImplementation(async (userId: string) => {
      if (userId === "u2" && createAdjustment.mock.calls.length <= 3)
        throw new Error("网络超时");
      return {};
    });
    const onFinished = vi.fn();
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={vi.fn()}
        onFinished={onFinished}
      />,
    );
    fireEvent.change(screen.getByLabelText(/每户赠送积分/), {
      target: { value: "50" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "十月激活活动" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    await screen.findByText("成功 2 / 失败 1");
    fireEvent.click(screen.getByRole("button", { name: /重试失败部分（1）/ }));
    await waitFor(() => expect(onFinished).toHaveBeenCalled());

    const refs = createAdjustment.mock.calls.map(
      (call) => (call[1] as { sourceDocumentRef: string }).sourceDocumentRef,
    );
    expect(new Set(refs).size).toBe(1);
    expect(refs[0]).toMatch(/^batch-/);
  });

  it("完成后清理记录失败不算发放失败：记录里全部标记 done，不会重复发放", async () => {
    const spy = vi
      .spyOn(Storage.prototype, "removeItem")
      .mockImplementation(() => {
        throw new Error("denied");
      });
    createAdjustment.mockResolvedValue({});
    const onClose = vi.fn();
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={onClose}
        onFinished={vi.fn()}
      />,
    );
    fill();
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const stored = JSON.parse(
      localStorage.getItem(batchStorageKey()) ?? "null",
    ) as { targets: { done: boolean }[] };
    expect(stored.targets.every((target) => target.done)).toBe(true);
    spy.mockRestore();
  });

  it("发放过程中重复提交只跑一轮", async () => {
    createAdjustment.mockResolvedValue({});
    const onClose = vi.fn();
    render(
      <BatchGrantDialog
        open
        targets={targets}
        onClose={onClose}
        onFinished={vi.fn()}
      />,
    );
    fill();
    const confirm = screen.getByRole("button", { name: "确认发放" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(createAdjustment).toHaveBeenCalledTimes(targets.length);
  });
});
