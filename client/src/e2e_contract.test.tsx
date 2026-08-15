import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const healthResponse = { status: "ok", service: "video-replica-api" };

const completedFakeBatch = {
  id: "batch-fake-e2e",
  status: "SUCCEEDED",
  quantity: 2,
  progress: {
    total_count: 2,
    terminal_count: 2,
    progress_percent: 100,
    counts: {
      pending: 0,
      submitting: 0,
      queued: 0,
      running: 0,
      archiving: 0,
      succeeded: 2,
      failed: 0,
      cancelled: 0,
      needs_attention: 0,
    },
  },
  tasks: [
    {
      id: "task-fake-1",
      status: "SUCCEEDED",
      archive_status: "ARCHIVED",
      quality_status: "AUDIO_OK",
      quality_issue_codes: [],
      result_asset_id: "asset-fake-1",
      prompt_snapshot: { status: "LOCKED" },
    },
    {
      id: "task-fake-2",
      status: "SUCCEEDED",
      archive_status: "ARCHIVED",
      quality_status: "AUDIO_OK",
      quality_issue_codes: [],
      result_asset_id: "asset-fake-2",
      prompt_snapshot: { status: "LOCKED" },
    },
  ],
};

describe("Fake provider E2E contract", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("renders the locked prompt batch progress and archived fake results", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/auth/me")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            id: "employee_1",
            username: "employee_1",
            display_name: "林夏",
            role: "employee",
          }),
        });
      }
      if (url.endsWith("/health")) {
        return Promise.resolve({
          ok: true,
          json: async () => healthResponse,
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => completedFakeBatch,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    fireEvent.change(await screen.findByLabelText("Batch ID"), {
      target: { value: " batch-fake-e2e " },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    expect(await screen.findByText("100%")).toBeInTheDocument();
    expect(screen.getByText("已完成 2 / 2")).toBeInTheDocument();
    expect(screen.getByText("task-fake-1")).toBeInTheDocument();
    expect(screen.getByText("task-fake-2")).toBeInTheDocument();
    expect(screen.getAllByText("阶段：已归档")).toHaveLength(2);
    expect(screen.queryByText("需要处理")).not.toBeInTheDocument();
    expect(screen.getAllByText("结果已归档")).toHaveLength(2);
    expect(window.localStorage.getItem("generation.batchId")).toBe(
      "batch-fake-e2e",
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8000/api/generation-batches/batch-fake-e2e",
        expect.objectContaining({
          headers: expect.any(Headers),
          signal: expect.any(AbortSignal),
        }),
      ),
    );
  });
});
