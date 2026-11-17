import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  localStorage.clear();
  vi.resetModules();
});
describe("长视频任务定位持久化", () => {
  it("重启后恢复定位，读取不会重新发起付费任务", async () => {
    let session = await import("./longReplicaSession");
    session.handOffLongReplicaSource("one", { projectId: "p", assetId: "a" });
    session.getLongReplicaSession("one").run = {
      projectId: "p",
      assetId: "a",
      taskId: "task",
    };
    vi.resetModules();
    session = await import("./longReplicaSession");
    expect(session.getLongReplicaSession("one").run?.taskId).toBe("task");
    expect(session.getLongReplicaSession("one").pendingAutoStart).toBe(false);
    expect(session.getLongReplicaSession("two").run).toBeNull();
  });
  it("被新来源替代的旧引用不能把定位写回", async () => {
    const session = await import("./longReplicaSession");
    session.handOffLongReplicaSource("one", {
      projectId: "old",
      assetId: "old",
    });
    const old = session.getLongReplicaSession("one");
    session.handOffLongReplicaSource("one", {
      projectId: "new",
      assetId: "new",
    });
    old.run = { projectId: "old", assetId: "old", taskId: "late" };
    expect(
      JSON.parse(localStorage.getItem("long-replica-run:one")!).projectId,
    ).toBe("new");
  });
});
