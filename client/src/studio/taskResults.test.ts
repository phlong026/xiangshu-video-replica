import { describe, expect, it } from "vitest";
import { taskHasDeliverable } from "./taskResults";

describe("taskHasDeliverable（FIX-01）", () => {
  it("已完成任务总是可交付", () => {
    expect(
      taskHasDeliverable({ status: "completed", backendKind: "oral_task" }),
    ).toBe(true);
  });

  it("部分完成 / 待核对的生成批次只要有成功成片就可预览", () => {
    for (const status of ["partial", "uncertain"] as const) {
      expect(
        taskHasDeliverable({
          status,
          backendKind: "generation_batch",
          deliverableCount: 2,
        }),
      ).toBe(true);
      expect(
        taskHasDeliverable({
          status,
          backendKind: "generation_batch",
          deliverableCount: 0,
        }),
      ).toBe(false);
    }
  });

  it("口播任务不按批次条数判断", () => {
    expect(
      taskHasDeliverable({
        status: "uncertain",
        backendKind: "oral_task",
        deliverableCount: 1,
      }),
    ).toBe(false);
  });
});
