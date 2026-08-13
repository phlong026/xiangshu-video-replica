import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const healthResponse = { status: "ok", service: "video-replica-api" };

function batchResponse(overrides = {}) {
  return {
    id: "batch-1",
    status: "RUNNING",
    quantity: 2,
    progress: {
      total_count: 2,
      terminal_count: 1,
      progress_percent: 50,
      counts: {
        pending: 0,
        submitting: 0,
        queued: 0,
        running: 1,
        archiving: 0,
        succeeded: 1,
        failed: 0,
        cancelled: 0,
        needs_attention: 1,
      },
    },
    tasks: [
      {
        id: "task-done",
        status: "SUCCEEDED",
        archive_status: "ARCHIVE_FAILED",
        quality_status: "AUDIO_OK",
        quality_issue_codes: [],
        result_asset_id: "asset-done",
        prompt_snapshot: null,
      },
      {
        id: "task-running",
        status: "RUNNING",
        archive_status: "PENDING",
        quality_status: "AUDIO_QUALITY_FAILED",
        quality_issue_codes: ["AUDIO_QUALITY_FAILED"],
        result_asset_id: null,
        prompt_snapshot: null,
      },
    ],
    ...overrides,
  };
}

describe("App", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it("starts on the internal login screen", () => {
    vi.stubGlobal("fetch", vi.fn());

    render(<App />);

    expect(
      screen.getByRole("heading", { name: "短视频复刻工作台" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "进入工作台" }),
    ).toBeInTheDocument();
  });

  it("navigates from login to the project page and loads local API health", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", service: "video-replica-api" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(screen.getByRole("heading", { name: "项目" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("本地服务已连接")).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8000/health", {
      signal: expect.any(AbortSignal),
    });
  });

  it("loads a pasted batch id and renders progress, task stages, results, and attention hints", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({
          ok: true,
          json: async () => healthResponse,
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => batchResponse(),
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    fireEvent.change(screen.getByLabelText("Batch ID"), {
      target: { value: " batch-1 " },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    expect(await screen.findByText("50%")).toBeInTheDocument();
    expect(screen.getByText("已完成 1 / 2")).toBeInTheDocument();
    expect(screen.getAllByText("需要处理 1")).toHaveLength(2);
    expect(screen.getByText("task-done")).toBeInTheDocument();
    expect(screen.getByText("阶段：归档失败")).toBeInTheDocument();
    expect(screen.getByText("结果已归档")).toBeInTheDocument();
    expect(screen.getByText("task-running")).toBeInTheDocument();
    expect(screen.getAllByText("需要处理")).toHaveLength(2);
    expect(window.localStorage.getItem("generation.batchId")).toBe("batch-1");
  });

  it("polls running batches every two seconds and stops after the terminal state", async () => {
    vi.useFakeTimers();
    const runningBatch = batchResponse({
      status: "RUNNING",
      progress: {
        ...batchResponse().progress,
        terminal_count: 0,
        progress_percent: 0,
        counts: {
          ...batchResponse().progress.counts,
          running: 2,
          succeeded: 0,
        },
      },
    });
    const doneBatch = batchResponse({
      status: "SUCCEEDED",
      progress: {
        ...batchResponse().progress,
        terminal_count: 2,
        progress_percent: 100,
        counts: {
          ...batchResponse().progress.counts,
          running: 0,
          succeeded: 2,
        },
      },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => healthResponse })
      .mockResolvedValueOnce({ ok: true, json: async () => runningBatch })
      .mockResolvedValueOnce({ ok: true, json: async () => doneBatch });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    fireEvent.change(screen.getByLabelText("Batch ID"), {
      target: { value: "batch-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("0%")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(screen.getByText("已完成 2 / 2")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("stops polling when the batch needs manual handling", async () => {
    vi.useFakeTimers();
    const attentionBatch = batchResponse({ status: "NEEDS_ATTENTION" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => healthResponse })
      .mockResolvedValueOnce({ ok: true, json: async () => attentionBatch });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    fireEvent.change(screen.getByLabelText("Batch ID"), {
      target: { value: "batch-attention" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getAllByText("需要处理")).not.toHaveLength(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("backs off after poll failures while keeping the last running batch visible", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => healthResponse })
      .mockResolvedValueOnce({
        ok: true,
        json: async () =>
          batchResponse({
            progress: {
              ...batchResponse().progress,
              terminal_count: 0,
              progress_percent: 0,
            },
          }),
      })
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce({ ok: true, json: async () => batchResponse() });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    fireEvent.change(screen.getByLabelText("Batch ID"), {
      target: { value: "batch-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("0%")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByText("网络连接失败，4 秒后重试")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_999);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("restores the last batch id from localStorage when reopening the workspace", async () => {
    window.localStorage.setItem("generation.batchId", "batch-restored");
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({
          ok: true,
          json: async () => healthResponse,
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => batchResponse({ id: "batch-restored" }),
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(
      await screen.findByDisplayValue("batch-restored"),
    ).toBeInTheDocument();
    expect(await screen.findByText("batch-restored")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/generation-batches/batch-restored",
      { signal: expect.any(AbortSignal) },
    );
  });
});
