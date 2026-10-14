import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkerObservations } from "../api.admin";
import { WorkerObservationsPage } from "./WorkerObservationsPage";

const listObservations = vi.fn();

vi.mock("../api.admin", () => ({
  listWorkerObservations: () => listObservations(),
}));

function observations(
  overrides: Partial<WorkerObservations["attention"]> = {},
): WorkerObservations {
  return {
    queues: { video: [], oral: [], viralRefresh: [], publish: [] },
    attention: {
      videoStaleMinutes: null,
      videoStaleThresholdMinutes: 30,
      oralStaleMinutes: null,
      oralStaleThresholdMinutes: 30,
      publishStaleMinutes: null,
      publishStaleThresholdMinutes: 30,
      oralQuarantinedTasks: 0,
      oralQuarantinedAvatars: 0,
      oralQuarantinedVoices: 0,
      stuckPreparations: 0,
      stuckPreparationOldestMinutes: 0,
      ...overrides,
    },
    rule: "口径说明",
  } as WorkerObservations;
}

describe("WorkerObservationsPage", () => {
  beforeEach(() => listObservations.mockReset());
  afterEach(() => vi.useRealTimers());

  it("超阈值与隔离区条目给出一步跳到生成记录的链接", async () => {
    listObservations.mockResolvedValue(
      observations({
        videoStaleMinutes: 90,
        oralQuarantinedTasks: 2,
        oralQuarantinedVoices: 1,
      }),
    );
    render(<WorkerObservationsPage />);

    expect(await screen.findByRole("link", { name: "去处理" })).toHaveAttribute(
      "href",
      "#admin/generationRecords?recordType=VIDEO&status=RUNNING",
    );
    expect(screen.getByRole("link", { name: "处理口播任务" })).toHaveAttribute(
      "href",
      "#admin/generationRecords?recordType=ORAL_VIDEO&status=SUBMISSION_UNCERTAIN",
    );
    expect(screen.getByRole("link", { name: "处理声音" })).toHaveAttribute(
      "href",
      "#admin/generationRecords?recordType=ORAL_VOICE&status=SUBMISSION_UNCERTAIN",
    );
    expect(screen.queryByRole("link", { name: "处理分身" })).toBeNull();
  });

  it("未超阈值、隔离区为空时不出现链接", async () => {
    listObservations.mockResolvedValue(observations({ videoStaleMinutes: 5 }));
    render(<WorkerObservationsPage />);

    await screen.findByText("需要关注");
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("默认每 30 秒自动刷新，可手动关闭", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    listObservations.mockResolvedValue(observations());
    render(<WorkerObservationsPage />);
    await screen.findByText("需要关注");
    const calls = listObservations.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(listObservations.mock.calls.length).toBe(calls + 1);

    screen.getByRole("button", { name: /自动刷新：开/ }).click();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(listObservations.mock.calls.length).toBe(calls + 1);
  });
});
