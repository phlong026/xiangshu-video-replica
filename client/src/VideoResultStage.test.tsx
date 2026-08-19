import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GenerationBatch, GenerationTask } from "./api";
import { VideoResultStage } from "./VideoResultStage";

function task(overrides: Partial<GenerationTask> = {}): GenerationTask {
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

function batch(overrides: Partial<GenerationBatch> = {}): GenerationBatch {
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

// 后端时间是 "YYYY-MM-DD HH:mm:ss"（UTC）；按偏移生成以驱动进度插值。
function serverTime(offsetSeconds: number): string {
  return new Date(Date.now() - offsetSeconds * 1000)
    .toISOString()
    .slice(0, 19)
    .replace("T", " ");
}

function renderStage(
  overrides: Partial<Parameters<typeof VideoResultStage>[0]> = {},
) {
  const props = {
    activeResultAction: "",
    activeTaskAction: "",
    batch: batch(),
    canOperate: true,
    onDownload: vi.fn(),
    onOpenOpsDetail: vi.fn(),
    onRegenerate: vi.fn(),
    onRequestPreview: vi.fn(),
    previewUrls: {} as Record<string, string>,
    resultErrors: {} as Record<string, string>,
    ...overrides,
    onPreviewSourceError: overrides.onPreviewSourceError ?? vi.fn(),
  };
  render(<VideoResultStage {...props} />);
  return props;
}

describe("VideoResultStage", () => {
  it("renders the stage progress narrative with reassurance while rendering", () => {
    renderStage({
      batch: batch({
        status: "QUEUED",
        quantity: 1,
        progress: {
          total_count: 1,
          terminal_count: 0,
          progress_percent: 0,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 1,
            archiving: 0,
            succeeded: 0,
            failed: 0,
            cancelled: 0,
            needs_attention: 0,
          },
        },
        tasks: [
          task({
            status: "RUNNING",
            stage: "RUNNING",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            submitted_at: serverTime(65),
            started_at: serverTime(60),
            completed_at: null,
            duration_seconds: null,
          }),
        ],
      }),
    });

    // 进度环：RUNNING 60s 处于时间插值区间（25%–85%），非精确值断言。
    const ring = screen.getByRole("progressbar", { name: "生成进度" });
    const percent = Number(ring.getAttribute("aria-valuenow"));
    expect(percent).toBeGreaterThanOrEqual(49);
    expect(percent).toBeLessThanOrEqual(85);

    // 阶段叙事：步骤条 + 阶段文案 + 时间预期 + 轮换安抚内容。
    expect(screen.getByText("渲染中").closest("li")).toHaveClass(
      "video-stage-step--active",
    );
    expect(screen.getByText("已提交").closest("li")).toHaveClass(
      "video-stage-step--done",
    );
    expect(
      screen.getByText("AI 正在基于你的首帧渲染画面、动作与口型…"),
    ).toBeInTheDocument();
    expect(screen.getByText(/已用时 1 分/)).toBeInTheDocument();
    expect(
      screen.getByText(/渲染在云端进行，离开此页面不会中断任务/),
    ).toBeInTheDocument();
  });

  it("shows an explicit not-lost reassurance when rendering runs long", () => {
    renderStage({
      batch: batch({
        tasks: [
          task({
            status: "RUNNING",
            stage: "RUNNING",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            submitted_at: serverTime(500),
            started_at: serverTime(400),
          }),
        ],
      }),
    });

    expect(
      screen.getByText("仍在渲染中，任务没有丢失，请放心等待。"),
    ).toBeInTheDocument();
  });

  it("streams the finished result automatically with an info bar", () => {
    const onRequestPreview = vi.fn();
    renderStage({
      onRequestPreview,
      previewUrls: { "task-ok": "https://stage-preview/asset-ok" },
    });

    const video = screen.getByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute("src", "https://stage-preview/asset-ok");
    expect(video).toHaveAttribute("preload", "auto");
    expect(screen.getByText("MiniMax-H3")).toBeInTheDocument();
    expect(screen.getByText("音频质检通过")).toBeInTheDocument();
    expect(screen.getByText("¥1.50")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载 MP4" }),
    ).toBeInTheDocument();
    // 已有播放地址时不重复签发。
    expect(onRequestPreview).not.toHaveBeenCalled();
  });

  it("waits for a click with sound on: no autoplay, custom controls", () => {
    renderStage({
      previewUrls: { "task-ok": "https://stage-preview/asset-ok" },
    });

    const video = screen.getByLabelText("结果预览 task-ok");
    // 不自动播放、不静音：有声播放由用户点击开启。
    expect(video).not.toHaveAttribute("autoplay");
    expect(video).not.toHaveAttribute("muted");
    expect(video).not.toHaveAttribute("loop");
    expect(video).not.toHaveAttribute("controls");
    // 首帧等待点击：中央大播放按钮 + 常驻控制条。
    expect(
      screen.getByRole("button", { name: "播放 结果预览 task-ok" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "播放" })).toBeInTheDocument();
    expect(screen.getByLabelText("播放进度")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "静音" })).toBeInTheDocument();
    expect(screen.getByLabelText("音量")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全屏" })).toBeInTheDocument();
    expect(screen.getByText("0:00 / 0:00")).toBeInTheDocument();
  });

  it("requests a streaming url automatically for archived results", () => {
    const onRequestPreview = vi.fn();
    renderStage({ onRequestPreview });

    expect(onRequestPreview).toHaveBeenCalledWith(
      expect.objectContaining({ id: "task-ok" }),
    );
  });

  it("stops auto-retrying a failed streaming request until manual retry", () => {
    const onRequestPreview = vi.fn();
    renderStage({
      onRequestPreview,
      resultErrors: { "task-ok": "预览链接获取失败，请重试。" },
    });

    expect(onRequestPreview).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "重试加载" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试加载" }));
    expect(onRequestPreview).toHaveBeenCalledTimes(1);
  });

  it("switches the active result from the filmstrip", () => {
    const onRequestPreview = vi.fn();
    renderStage({
      onRequestPreview,
      previewUrls: { "task-ok": "https://stage-preview/asset-ok" },
    });

    expect(screen.getByLabelText("结果预览 task-ok")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "查看结果 2：task-audio-failed" }),
    );

    // 质检未过的结果仍可对比查看，并显示警示条。
    expect(
      screen.getByText("音频质检未通过：该结果仅供对比查看，建议再次生成。"),
    ).toBeInTheDocument();
    expect(onRequestPreview).toHaveBeenCalledWith(
      expect.objectContaining({ id: "task-audio-failed" }),
    );
  });

  it("requires a preset reason and payment confirmation for paid regeneration", () => {
    const onRegenerate = vi.fn();
    renderStage({
      onRegenerate,
      batch: batch({
        quantity: 1,
        tasks: [task({ available_actions: ["REGENERATE"] })],
      }),
    });

    fireEvent.click(screen.getByRole("button", { name: "再次生成" }));
    const submit = screen.getByRole("button", { name: "确认再次生成" });
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: "画面质量不佳" }));
    expect(submit).toBeDisabled();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "确认为任务 task-ok 新增一次付费生成",
      }),
    );
    fireEvent.change(screen.getByLabelText("再次生成补充说明 task-ok"), {
      target: { value: "人物手部细节问题" },
    });
    fireEvent.click(submit);

    expect(onRegenerate).toHaveBeenCalledWith(
      expect.objectContaining({ id: "task-ok" }),
      "画面质量不佳：人物手部细节问题",
    );
  });

  it("keeps auditors read-only without streaming requests", () => {
    const onRequestPreview = vi.fn();
    renderStage({ canOperate: false, onRequestPreview });

    expect(
      screen.getByText("审计只读，不可预览或下载结果"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "下载 MP4" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "再次生成" }),
    ).not.toBeInTheDocument();
    expect(onRequestPreview).not.toHaveBeenCalled();
  });

  it("guides attention tasks to the ops view", () => {
    const onOpenOpsDetail = vi.fn();
    renderStage({
      onOpenOpsDetail,
      batch: batch({
        tasks: [
          task({
            id: "task-uncertain",
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            available_actions: ["RECONCILE"],
          }),
        ],
      }),
    });

    expect(
      screen.getByText(/该任务需要人工处理（对账、归档重试或账单确认）/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看处理方式" }));
    expect(onOpenOpsDetail).toHaveBeenCalledTimes(1);
  });
});
