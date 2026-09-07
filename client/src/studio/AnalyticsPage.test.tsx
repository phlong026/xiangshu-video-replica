import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StudioAnalytics } from "../api";
import { createReviewData, createReviewState, reviewUser } from "./fixtures";
import type { StudioContextValue, StudioData } from "./types";

const { getStudioAnalytics, getStudioStats, useStudio } = vi.hoisted(() => ({
  getStudioAnalytics: vi.fn(),
  getStudioStats: vi.fn(),
  useStudio: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio }));
vi.mock("../api", () => ({ getStudioAnalytics, getStudioStats }));

import { AnalyticsPage } from "./AnalyticsPage";

function studio(
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: createReviewState("analytics"),
    data: createReviewData(),
    review: true,
    user: reviewUser,
    navigate: vi.fn(),
    patchDraft: vi.fn(),
    patchState: vi.fn(),
    updateData: vi.fn(),
    notify: vi.fn(),
    openPicker: vi.fn(),
    openLive: vi.fn(),
    requestGeneration: vi.fn(),
    saveDraft: vi.fn(),
    confirmFinalDraft: vi.fn(),
    extractScriptFromUpload: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  };
}

function productionData(overrides: Partial<StudioData> = {}): StudioData {
  return {
    people: [],
    assets: [],
    materials: [],
    videos: [],
    tasks: [],
    projects: [],
    errors: [],
    loading: false,
    stats: null,
    analytics7: null,
    analytics30: null,
    ...overrides,
  };
}

const sampleAnalytics: StudioAnalytics = {
  range_days: 7,
  generated_at: "2026-09-07T08:00:00+00:00",
  today_completed: 3,
  range_completed: 9,
  total_completed: 42,
  today_completed_outputs: 3,
  range_completed_outputs: 9,
  total_completed_outputs: 42,
  today_generation_batches: 2,
  range_generation_batches: 6,
  total_generation_batches: 30,
  range_generation_outputs: 7,
  range_oral_outputs: 2,
  daily: [
    { day: "2026-09-01", completed: 1, failed: 0 },
    { day: "2026-09-02", completed: 0, failed: 1 },
    { day: "2026-09-03", completed: 2, failed: 0 },
    { day: "2026-09-04", completed: 1, failed: 0 },
    { day: "2026-09-05", completed: 2, failed: 0 },
    { day: "2026-09-06", completed: 3, failed: 1 },
    { day: "2026-09-07", completed: 0, failed: 0 },
  ],
  kind_breakdown: [
    { kind: "replica", completed: 6 },
    { kind: "independent", completed: 3 },
  ],
  recent_works: [
    {
      task_id: "t-1",
      task_kind: "generation",
      batch_id: "b-1",
      project_id: "p-1",
      title: "庭院黄昏实拍",
      creation_kind: "replica",
      completed_at: "2026-09-06 10:00:00",
      cost_credits: 8,
    },
    {
      task_id: "t-2",
      task_kind: "generation",
      batch_id: "b-2",
      project_id: "p-1",
      title: "户型讲解口播",
      creation_kind: "independent",
      completed_at: "2026-09-05 09:00:00",
      cost_credits: null,
    },
  ],
};

describe("V1.4 数据看板", () => {
  beforeEach(() => {
    useStudio.mockReset();
    getStudioAnalytics.mockReset();
    getStudioAnalytics.mockImplementation(() => new Promise(() => {}));
    getStudioStats.mockReset();
    getStudioStats.mockResolvedValue({
      today_completed: 3,
      running: 1,
      queued: 2,
      needs_attention: 1,
      total_completed: 42,
    });
  });

  it("审核模式呈现时间筛选、分单位指标、成片趋势与最近成片表", () => {
    useStudio.mockReturnValue(studio());
    render(<AnalyticsPage />);

    expect(screen.getByLabelText("时间筛选")).toHaveValue("7");
    expect(screen.getByText("示例数据")).toBeInTheDocument();
    expect(screen.getByText("期间成片（产出项）")).toBeInTheDocument();
    expect(screen.getByText("20 个")).toBeInTheDocument();
    expect(screen.getByText("今日成片（产出项）")).toBeInTheDocument();
    expect(screen.getByText("8 个")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "近7天成片趋势" }),
    ).toBeInTheDocument();
    // 类型占比图例覆盖三个创作通道（类型文案也会出现在作品表列）。
    expect(screen.getAllByText("视频复刻").length).toBeGreaterThan(0);
    expect(screen.getAllByText("人物置换").length).toBeGreaterThan(0);
    // 趋势图例区分成片/失败曲线；作品表含消耗列（示例：12 积分 / —）。
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("消耗")).toBeInTheDocument();
    expect(screen.getByText("12 积分")).toBeInTheDocument();
    // 播放/互动等外部平台指标不在看板范畴（C6 不伪造红线）。
    expect(screen.queryByText("播放量")).not.toBeInTheDocument();
    expect(screen.queryByText("互动量")).not.toBeInTheDocument();
    // 表头一行 + 3 条最近成片。
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });

  it("时间筛选切换 7/30 天会更换数据窗口", () => {
    useStudio.mockReturnValue(studio());
    render(<AnalyticsPage />);

    fireEvent.change(screen.getByLabelText("时间筛选"), {
      target: { value: "30" },
    });
    expect(screen.getByText("38 个")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "近30天成片趋势" }),
    ).toBeInTheDocument();
    // 30 天样例比 7 天多出窗口更早的第 4 条成片。
    expect(screen.getByText("三代同堂的家这样设计")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(5);
  });

  it("查看成片进入该行对应的任务详情", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<AnalyticsPage />);

    const row = screen.getByRole("row", { name: /张工 · 建房预算/ });
    fireEvent.click(within(row).getByRole("button", { name: "查看视频" }));
    expect(value.navigate).toHaveBeenCalledWith("task-detail", {
      selectedTaskId: "batch-review-1",
      selectedTaskKind: "generation_batch",
      selectedTaskBackendId: "batch-review-1",
      returnTo: "analytics",
    });
  });

  it("正式工作区统计未就绪时显示空态且不出现示例数值", () => {
    useStudio.mockReturnValue(
      studio({ review: false, data: productionData() }),
    );
    render(<AnalyticsPage />);
    expect(screen.getByText("统计数据尚未就绪")).toBeInTheDocument();
    expect(screen.queryByText("示例数据")).not.toBeInTheDocument();
    expect(screen.queryByText("20 个")).not.toBeInTheDocument();
  });

  it("正式工作区渲染真实聚合：指标、趋势、类型占比与作品表", () => {
    getStudioAnalytics.mockResolvedValue(sampleAnalytics);
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: productionData({
          stats: {
            today_completed: 3,
            running: 1,
            queued: 2,
            needs_attention: 1,
            total_completed: 42,
          },
          analytics7: sampleAnalytics,
          analytics30: { ...sampleAnalytics, range_days: 30 },
        }),
      }),
    );
    render(<AnalyticsPage />);

    expect(screen.getByText("9 个")).toBeInTheDocument();
    expect(screen.getByText("6 批")).toBeInTheDocument();
    expect(screen.getByText("普通生成 7 个 · 口播 2 个")).toBeInTheDocument();
    // 今日成片 3 与成片队列 3（running 1 + queued 2）文案相同，取并集断言。
    expect(screen.getAllByText("3 个").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("成片队列")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "近7天成片趋势" }),
    ).toBeInTheDocument();
    expect(screen.getByText("庭院黄昏实拍")).toBeInTheDocument();
    expect(screen.getByText("8 积分")).toBeInTheDocument();
    expect(screen.queryByText("示例数据")).not.toBeInTheDocument();
  });

  it("正式工作区所选窗口缺失聚合时回退空态", () => {
    getStudioAnalytics.mockRejectedValue(new Error("analytics unavailable"));
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: productionData({ analytics7: sampleAnalytics }),
      }),
    );
    render(<AnalyticsPage />);

    expect(screen.getByText("9 个")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("时间筛选"), {
      target: { value: "30" },
    });
    expect(screen.getByText("统计数据尚未就绪")).toBeInTheDocument();
    expect(screen.queryByText("9 个")).not.toBeInTheDocument();
  });

  it("进页读取并允许手动刷新，成功后更新当前窗口与更新时间", async () => {
    const refreshed = {
      ...sampleAnalytics,
      generated_at: "2026-09-07T09:30:00+00:00",
      range_completed_outputs: 12,
      range_completed: 12,
    };
    getStudioAnalytics.mockResolvedValue(refreshed);
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: productionData({ analytics7: sampleAnalytics }),
      }),
    );

    render(<AnalyticsPage />);
    await vi.waitFor(() => expect(getStudioAnalytics).toHaveBeenCalledWith(7));
    expect(await screen.findByText("12 个")).toBeInTheDocument();
    expect(screen.getByText(/更新时间/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "刷新报表" }));
    await vi.waitFor(() => expect(getStudioAnalytics).toHaveBeenCalledTimes(2));
  });

  it("刷新失败保留旧数据并标记已过期，重试仍使用当前窗口", async () => {
    getStudioAnalytics.mockRejectedValue(new Error("network down"));
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: productionData({ analytics7: sampleAnalytics }),
      }),
    );

    render(<AnalyticsPage />);

    expect(
      await screen.findByText("数据可能已过期，请重试刷新。"),
    ).toBeInTheDocument();
    expect(screen.getByText("9 个")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试刷新" }));
    await vi.waitFor(() => expect(getStudioAnalytics).toHaveBeenCalledTimes(2));
    expect(getStudioAnalytics).toHaveBeenLastCalledWith(7);
  });

  it("口播成片从报表进入口播任务详情", () => {
    const value = studio({
      review: false,
      data: productionData({
        analytics7: {
          ...sampleAnalytics,
          recent_works: [
            {
              task_id: "oral-1",
              task_kind: "oral",
              batch_id: null,
              project_id: null,
              title: "院落口播",
              creation_kind: "oral",
              completed_at: "2026-09-07 09:00:00",
              cost_credits: 3,
            },
          ],
        },
      }),
    });
    useStudio.mockReturnValue(value);
    getStudioAnalytics.mockImplementation(() => new Promise(() => {}));

    render(<AnalyticsPage />);
    fireEvent.click(screen.getByRole("button", { name: "查看视频" }));
    expect(value.navigate).toHaveBeenCalledWith("task-detail", {
      selectedTaskId: "oral-1",
      selectedTaskKind: "oral_task",
      selectedTaskBackendId: "oral-1",
      returnTo: "analytics",
    });
  });

  it("账号切换后立即隐藏旧账号报表并忽略旧请求结果", async () => {
    let resolveOld: ((value: StudioAnalytics) => void) | undefined;
    const oldRequest = new Promise<StudioAnalytics>((resolve) => {
      resolveOld = resolve;
    });
    const newAnalytics = {
      ...sampleAnalytics,
      range_completed_outputs: 2,
      range_completed: 2,
      recent_works: [],
    };
    getStudioAnalytics
      .mockReturnValueOnce(oldRequest)
      .mockResolvedValueOnce(newAnalytics);
    let value = studio({
      review: false,
      user: { ...reviewUser, id: "account-a" },
      data: productionData({ analytics7: sampleAnalytics }),
    });
    useStudio.mockImplementation(() => value);
    const rendered = render(<AnalyticsPage />);

    value = studio({
      review: false,
      user: { ...reviewUser, id: "account-b" },
      data: productionData({ analytics7: sampleAnalytics }),
    });
    rendered.rerender(<AnalyticsPage />);
    expect(screen.queryByText("庭院黄昏实拍")).not.toBeInTheDocument();

    resolveOld?.({ ...sampleAnalytics, range_completed_outputs: 99 });
    expect(await screen.findByText("2 个")).toBeInTheDocument();
    expect(screen.queryByText("99 个")).not.toBeInTheDocument();
  });
});
