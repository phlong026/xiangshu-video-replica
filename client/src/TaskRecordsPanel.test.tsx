import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { TaskRecordsPanel } from "./TaskRecordsPanel";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getGenerationBatch: vi.fn(),
    getGenerationResultDownloadUrl: vi.fn(),
    listGenerationBatches: vi.fn(),
    retryGenerationTask: vi.fn(),
    confirmGenerationTaskNotCharged: vi.fn(),
    reconcileUncertainTask: vi.fn(),
  };
});

function task(overrides: Partial<api.GenerationTask> = {}): api.GenerationTask {
  return {
    id: "task-ok",
    status: "SUCCEEDED",
    archive_status: "ARCHIVED",
    quality_status: "AUDIO_OK",
    quality_issue_codes: [],
    result_asset_id: "asset-ok",
    stage: "COMPLETED",
    provider: "fake_h3",
    model: "MiniMax-H3",
    provider_task_id_tail: "34567890",
    attempt: 1,
    archive_retry_count: 0,
    estimated_cost: 1.25,
    actual_cost: 1.5,
    error_code: null,
    error_message_redacted: null,
    submitted_at: "2026-08-16 10:00:00",
    started_at: "2026-08-16 10:00:01",
    completed_at: "2026-08-16 10:00:06",
    duration_seconds: 5,
    retry_of_task_id: null,
    superseded_by_task_id: null,
    superseded_at: null,
    retry_reason: null,
    retry_requested_at: null,
    available_actions: [],
    prompt_snapshot: null,
    ...overrides,
  };
}

function batch(
  overrides: Partial<api.GenerationBatch> = {},
): api.GenerationBatch {
  return {
    id: "batch-1",
    project_id: "project-1",
    prompt_version_id: "prompt-1",
    status: "NEEDS_ATTENTION",
    quantity: 2,
    stale: false,
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
        needs_attention: 1,
      },
    },
    tasks: [
      task(),
      task({
        id: "task-audio-failed",
        quality_status: "AUDIO_QUALITY_FAILED",
        quality_issue_codes: ["AUDIO_QUALITY_FAILED"],
        result_asset_id: "asset-audio-failed",
        stage: "QUALITY_FAILED",
      }),
    ],
    ...overrides,
  };
}

function listItem(
  overrides: Partial<api.GenerationBatchListItem> = {},
): api.GenerationBatchListItem {
  const detail = batch();
  return {
    id: detail.id,
    project_id: detail.project_id,
    project_name: "夏日咖啡馆口播复刻",
    created_by_user_id: "employee_1",
    created_by_display_name: "林夏",
    prompt_version_id: detail.prompt_version_id,
    status: detail.status,
    quantity: detail.quantity,
    created_at: "2026-08-16 10:00:00",
    updated_at: "2026-08-16 10:00:06",
    progress: detail.progress,
    total_estimated_cost: 2.5,
    total_actual_cost: 3,
    needs_attention_count: 1,
    has_results: true,
    tasks: detail.tasks.map(
      ({ prompt_snapshot: _promptSnapshot, ...item }) => item,
    ),
    ...overrides,
  };
}

describe("TaskRecordsPanel", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem()],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(batch());
    vi.mocked(api.getGenerationResultDownloadUrl)
      .mockResolvedValueOnce({ url: "https://signed.example/preview-1.mp4" })
      .mockResolvedValueOnce({ url: "https://signed.example/preview-2.mp4" })
      .mockResolvedValueOnce({ url: "https://signed.example/download.mp4" });
    vi.mocked(api.retryGenerationTask).mockImplementation(async (_taskId) =>
      task(),
    );
    vi.mocked(api.confirmGenerationTaskNotCharged).mockImplementation(
      async (_taskId) => task(),
    );
    vi.mocked(api.reconcileUncertainTask).mockImplementation(async (_taskId) =>
      task(),
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it("opens the newest project batch and renders quality, preview, and fresh download actions", async () => {
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    expect(await screen.findByText("夏日咖啡馆口播复刻")).toBeInTheDocument();
    expect(api.listGenerationBatches).toHaveBeenCalledWith({ limit: 20 });
    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledWith("batch-1"),
    );
    expect(screen.getByText("音频质检通过")).toBeInTheDocument();
    expect(screen.getByText("音频质检失败")).toBeInTheDocument();
    expect(
      screen.getByText("该结果不能作为合格交付，后续只能重新生成视频。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /音频修复/ }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("Provider 尾号 34567890")).toHaveLength(2);
    expect(
      screen.getByRole("region", { name: "结果播放器 task-ok" }),
    ).toHaveTextContent("点击加载预览");
    expect(
      screen
        .getByText("兼容查询：通过 Batch ID 查找历史记录")
        .closest("details"),
    ).not.toHaveAttribute("open");

    fireEvent.click(screen.getByRole("button", { name: "加载预览 task-ok" }));
    const video = await screen.findByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute(
      "src",
      "https://signed.example/preview-1.mp4",
    );

    fireEvent.click(screen.getByRole("button", { name: "刷新预览 task-ok" }));
    await waitFor(() =>
      expect(video).toHaveAttribute(
        "src",
        "https://signed.example/preview-2.mp4",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4 task-ok" }));

    await waitFor(() =>
      expect(api.getGenerationResultDownloadUrl).toHaveBeenNthCalledWith(
        3,
        "asset-ok",
      ),
    );
    expect(anchorClick).toHaveBeenCalledOnce();
  });

  it("appends cursor pages without duplicating an existing batch", async () => {
    vi.mocked(api.listGenerationBatches)
      .mockResolvedValueOnce({ items: [listItem()], next_cursor: "cursor-2" })
      .mockResolvedValueOnce({
        items: [
          listItem(),
          listItem({
            id: "batch-2",
            project_id: "project-2",
            project_name: "庭院改造复刻",
          }),
        ],
        next_cursor: null,
      });

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多任务记录" }),
    );

    await waitFor(() =>
      expect(api.listGenerationBatches).toHaveBeenNthCalledWith(2, {
        limit: 20,
        cursor: "cursor-2",
      }),
    );
    expect(
      screen.getAllByRole("button", { name: "打开批次 batch-1" }),
    ).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "打开批次 batch-2" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "加载更多任务记录" }),
    ).not.toBeInTheDocument();
  });

  it("keeps result metadata readable for auditors without preview, download, or reconcile actions", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            available_actions: ["RECONCILE"],
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="auditor"
      />,
    );

    expect(
      await screen.findByText("审计只读，不可预览或下载结果"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /预览/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /下载 MP4/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /对账/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /安全重试|重试归档|确认未计费/ }),
    ).not.toBeInTheDocument();
    expect(api.getGenerationResultDownloadUrl).not.toHaveBeenCalled();
  });

  it("does not describe pending quality checks as passed", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            id: "task-pending-quality",
            quality_status: "PENDING",
            result_asset_id: null,
            stage: "RUNNING",
            status: "RUNNING",
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    expect(await screen.findByText("质检待完成")).toBeInTheDocument();
    expect(
      screen.getByText("结果归档后将自动执行音频质检。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("音频正常，结果可进入人工确认。"),
    ).not.toBeInTheDocument();
  });

  it("does not write a completed reconcile response into a newly selected batch", async () => {
    let finishReconcile: ((value: api.GenerationTask) => void) | undefined;
    vi.mocked(api.reconcileUncertainTask).mockReturnValue(
      new Promise((resolve) => {
        finishReconcile = resolve;
      }),
    );
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [
        listItem({
          tasks: [
            task({
              id: "task-a",
              status: "SUBMISSION_UNCERTAIN",
              stage: "SUBMISSION_UNCERTAIN",
              available_actions: ["RECONCILE"],
            }),
          ],
        }),
        listItem({
          id: "batch-2",
          project_id: "project-2",
          project_name: "庭院改造复刻",
          tasks: [task({ id: "task-b" })],
        }),
      ],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockImplementation(async (batchId) =>
      batchId === "batch-2"
        ? batch({ id: "batch-2", tasks: [task({ id: "task-b" })] })
        : batch({
            tasks: [
              task({
                id: "task-a",
                status: "SUBMISSION_UNCERTAIN",
                stage: "SUBMISSION_UNCERTAIN",
                available_actions: ["RECONCILE"],
              }),
            ],
          }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "对账 task-a" }));
    fireEvent.click(screen.getByRole("button", { name: "打开批次 batch-2" }));
    expect(await screen.findByText("task-b")).toBeInTheDocument();

    await act(async () => {
      finishReconcile?.(task({ id: "task-a" }));
      await Promise.resolve();
    });

    expect(api.reconcileUncertainTask).toHaveBeenCalledWith(
      "task-a",
      expect.objectContaining({ idempotency_key: expect.any(String) }),
    );
    expect(screen.getByText("task-b")).toBeInTheDocument();
    expect(screen.queryByText("task-a")).not.toBeInTheDocument();
  });

  it("offers only server-approved retry, reconcile, and admin confirmation actions", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        quantity: 3,
        progress: {
          total_count: 3,
          terminal_count: 1,
          progress_percent: 33,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 0,
            archiving: 0,
            succeeded: 0,
            failed: 0,
            cancelled: 0,
            needs_attention: 3,
          },
        },
        tasks: [
          task({
            id: "task-archive",
            archive_status: "ARCHIVE_FAILED",
            stage: "ARCHIVE_FAILED",
            result_asset_id: null,
            available_actions: ["RETRY"],
          }),
          task({
            id: "task-reconcile",
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            result_asset_id: null,
            available_actions: ["RECONCILE"],
          }),
          task({
            id: "task-confirm",
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            result_asset_id: null,
            available_actions: ["CONFIRM_NOT_CHARGED"],
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="admin"
      />,
    );

    const retryButton = await screen.findByRole("button", {
      name: "重试归档 task-archive",
    });
    expect(retryButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("处理原因 task-archive"), {
      target: { value: "对象存储已恢复" },
    });
    fireEvent.click(retryButton);
    await waitFor(() =>
      expect(api.retryGenerationTask).toHaveBeenCalledWith(
        "task-archive",
        expect.objectContaining({
          idempotency_key: expect.any(String),
          retry_reason: "对象存储已恢复",
        }),
      ),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "对账 task-reconcile" }),
    );
    await waitFor(() =>
      expect(api.reconcileUncertainTask).toHaveBeenCalledWith(
        "task-reconcile",
        expect.objectContaining({ idempotency_key: expect.any(String) }),
      ),
    );

    const confirmButton = screen.getByRole("button", {
      name: "确认未计费 task-confirm",
    });
    expect(confirmButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("处理原因 task-confirm"), {
      target: { value: "供应商账单已核对" },
    });
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(api.confirmGenerationTaskNotCharged).toHaveBeenCalledWith(
        "task-confirm",
        expect.objectContaining({
          idempotency_key: expect.any(String),
          reason: "供应商账单已核对",
        }),
      ),
    );
  });
});
