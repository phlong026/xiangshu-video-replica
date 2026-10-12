import { describe, expect, it, vi } from "vitest";
import { createCloudDraftQueue } from "./cloudDraftQueue";

describe("cloud draft latest-intent queue", () => {
  it("历史成功A在B进行中再次到达时仍排队成为最终写入", async () => {
    let resolveB: (() => void) | undefined;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockResolvedValueOnce(undefined)
      .mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveB = resolve;
        }),
      )
      .mockResolvedValueOnce(undefined);
    const queue = createCloudDraftQueue(write);

    await queue.persist("A", "A", "u1", () => true);
    const pendingB = queue.persist("B", "B", "u1", () => true);
    const finalA = queue.persist("A", "A", "u1", () => true);
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A", "B"]);

    resolveB?.();
    await pendingB;
    await finalA;
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A", "B", "A"]);
  });

  it("前序失败后继续发送仍有效的最新意图", async () => {
    let rejectA: ((cause: Error) => void) | undefined;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockReturnValueOnce(
        new Promise<void>((_resolve, reject) => {
          rejectA = reject;
        }),
      )
      .mockResolvedValueOnce(undefined);
    const queue = createCloudDraftQueue(write);

    const failedA = queue.persist("A", "A", "u1", () => true);
    const currentB = queue.persist("B", "B", "u1", () => true);
    rejectA?.(new Error("A failed"));

    await expect(failedA).rejects.toThrow("A failed");
    await currentB;
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A", "B"]);
  });

  it("同key待写意图的旧谓词失效时改用最新谓词实际发送", async () => {
    let resolveX: (() => void) | undefined;
    let oldCurrent = true;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveX = resolve;
        }),
      )
      .mockResolvedValueOnce(undefined);
    const queue = createCloudDraftQueue(write);

    const activeX = queue.persist("X", "X", "u1", () => true);
    const oldA = queue.persist("A", "old A", "u1", () => oldCurrent);
    oldCurrent = false;
    const latestA = queue.persist("A", "latest A", "u1", () => true);
    resolveX?.();

    await activeX;
    await oldA;
    await latestA;
    expect(write.mock.calls.map(([value]) => value)).toEqual(["X", "latest A"]);
  });

  it("放弃后排队中的同族写入被静默丢弃", async () => {
    let resolveActive: (() => void) | undefined;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveActive = resolve;
        }),
      )
      .mockResolvedValue(undefined);
    const queue = createCloudDraftQueue(write);

    const active = queue.persist("A", "A", "u1", () => true);
    const queued = queue.persist("B", "B", "u1", () => true);
    queue.discard("u1");
    await expect(queued).resolves.toBeUndefined();
    resolveActive?.();
    await active;
    // B 从未发出；A 已在途，完成后被吞掉（不计成功、不报错）。
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A"]);
  });

  it("放弃后在途写入失败不上抛（服务端墓碑已兜底拒绝）", async () => {
    let rejectA: ((cause: Error) => void) | undefined;
    const write = vi.fn<(value: string) => Promise<void>>().mockReturnValueOnce(
      new Promise<void>((_resolve, reject) => {
        rejectA = reject;
      }),
    );
    const queue = createCloudDraftQueue(write);

    const activeA = queue.persist("A", "A", "u1", () => true);
    queue.discard("u1");
    rejectA?.(new Error("409 STUDIO_DRAFT_CONFLICT"));

    // 放弃后的迟到写失败对调用方不可见：既不 reject 也不打断后续编辑。
    await expect(activeA).resolves.toBeUndefined();
  });

  it("放弃后原样重敲的相同内容仍会写入（lastSuccessKey 被清除）", async () => {
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockResolvedValue(undefined);
    const queue = createCloudDraftQueue(write);

    await queue.persist("same", "same", "u1", () => true);
    queue.discard("u1");
    await queue.persist("same", "same", "u1", () => true);
    // 若幂等去重没被放弃清掉，第二次会因 lastSuccessKey 命中被跳过，
    // 云端墓碑就永远等不到真正的新稿。
    expect(write.mock.calls.map(([value]) => value)).toEqual(["same", "same"]);
  });
});
