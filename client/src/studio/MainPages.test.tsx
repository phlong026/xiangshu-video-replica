import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useLayoutEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Project } from "../api";
import { createState, patchStudioDraft } from "./state";
import type {
  StudioAsset,
  StudioContextValue,
  StudioData,
  StudioTask,
  StudioVideo,
} from "./types";

type WorkbenchUploadResult = {
  projectId: string;
  assetId: string;
  project?: Project;
  asset?: StudioAsset;
};

const {
  useStudio,
  loadTaskPreview,
  uploadWorkbenchSourceVideo,
  studioVideoFromViral,
  cancelStudioTask,
  downloadStudioTaskResult,
  retryStudioTask,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  createViralImportTask,
  resolveViralLink,
  getViralImportTask,
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
} = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
  loadTaskPreview: vi.fn(),
  uploadWorkbenchSourceVideo:
    vi.fn<
      (
        file: File,
        onProgress: (percent: number) => void,
        signal?: AbortSignal,
      ) => Promise<WorkbenchUploadResult>
    >(),
  studioVideoFromViral: vi.fn((item: import("../api").ViralVideoItem) => ({
    id: `${item.platform}-${item.videoId}`,
    nativeId: item.videoId,
    platformKey: item.platform,
    title: item.title,
    author: item.author,
    platform: "抖音",
    category: item.category,
    duration: "00:30",
    likes: item.likes,
    collections: item.collects,
    shares: item.shares,
    description: item.sourceDescription ?? item.title,
  })),
  cancelStudioTask: vi.fn(),
  downloadStudioTaskResult: vi.fn(),
  retryStudioTask: vi.fn(),
  loadMoreGenerationTasks: vi.fn(),
  loadMoreOralTasks: vi.fn(),
  loadStudioTaskDetail: vi.fn(),
  createViralImportTask: vi.fn(),
  resolveViralLink: vi.fn(),
  getViralImportTask: vi.fn(),
  getStudioNotificationPreferences: vi.fn(),
  updateStudioNotificationPreferences: vi.fn(),
}));

vi.mock("./context", () => ({ useStudio }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createViralImportTask,
  resolveViralLink,
  getViralImportTask,
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
}));
vi.mock("./live", () => ({
  loadTaskPreview,
  uploadWorkbenchSourceVideo,
  studioVideoFromViral,
  cancelStudioTask,
  downloadStudioTaskResult,
  retryStudioTask,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
}));

import {
  ProfilePage,
  TaskDetailPage,
  TasksPage,
  WorkbenchPage,
} from "./MainPages";
import { formatTaskTime } from "./ui";

const taskA: StudioTask = {
  id: "task-a",
  title: "张工建房预算",
  type: "数字人口播",
  status: "completed",
  submitted: "2026-09-05 10:00",
  batchId: "batch-a",
};

const taskB: StudioTask = {
  ...taskA,
  id: "task-b",
  title: "李总项目巡检",
  batchId: "batch-b",
};

const runningTask: StudioTask = {
  id: "batch-9",
  batchId: "batch-9",
  title: "张工 · 建房预算",
  type: "数字人口播",
  status: "running",
  progress: 68,
  submitted: "2026-09-06T09:30:00",
};

function data(
  tasks: StudioTask[] = [taskA],
  videos: StudioVideo[] = [],
): StudioData {
  return {
    people: [],
    assets: [],
    videos,
    tasks,
    projects: [],
    errors: [],
    materials: [],
    loading: false,
    stats: null,
    analytics7: null,
    analytics30: null,
  };
}

function studio(
  selectedTaskId = taskA.id,
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: { ...createState("task-detail"), selectedTaskId },
    data: data(),
    review: false,
    user: {} as StudioContextValue["user"],
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("V1.4 任务详情真实成片预览", () => {
  beforeEach(() => {
    useStudio.mockReset();
    loadTaskPreview.mockReset();
    downloadStudioTaskResult.mockReset();
    retryStudioTask.mockReset();
    loadStudioTaskDetail.mockReset();
  });

  it("首屏外任务按 URL 中的类型和后端 ID 读取，失败可受控重试", async () => {
    const loaded = {
      ...taskA,
      id: "oral-deep",
      backendKind: "oral_task" as const,
      backendId: "deep",
    };
    loadStudioTaskDetail
      .mockRejectedValueOnce(new Error("任务不存在或无权访问"))
      .mockResolvedValueOnce(loaded);
    const value = studio("oral-deep", {
      state: {
        ...createState("task-detail"),
        selectedTaskId: "oral-deep",
        selectedTaskKind: "oral_task",
        selectedTaskBackendId: "deep",
        returnTo: "analytics",
      },
      data: data([]),
      user: { id: "account-a" } as StudioContextValue["user"],
    });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    expect(await screen.findByText("任务详情暂不可用")).toBeInTheDocument();
    expect(screen.getByText("任务不存在或无权访问")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试读取任务详情" }));
    await waitFor(() => expect(loadStudioTaskDetail).toHaveBeenCalledTimes(2));
    expect(loadStudioTaskDetail).toHaveBeenLastCalledWith("oral_task", "deep");
    expect(value.updateData).toHaveBeenCalledOnce();
  });

  it("A到B再到A及账号切换时忽略所有旧详情响应", async () => {
    const oldA = deferred<StudioTask>();
    const oldB = deferred<StudioTask>();
    const currentA = deferred<StudioTask>();
    loadStudioTaskDetail
      .mockReturnValueOnce(oldA.promise)
      .mockReturnValueOnce(oldB.promise)
      .mockReturnValueOnce(currentA.promise);
    const updateData = vi.fn();
    const context = (
      id: string,
      account: string,
    ): Partial<StudioContextValue> => ({
      state: {
        ...createState("task-detail"),
        selectedTaskId: id,
        selectedTaskKind: "generation_batch",
        selectedTaskBackendId: id,
      },
      data: data([]),
      user: { id: account } as StudioContextValue["user"],
      updateData,
    });
    useStudio.mockReturnValue(studio("a", context("a", "account-a")));
    const view = render(<TaskDetailPage />);
    useStudio.mockReturnValue(studio("b", context("b", "account-a")));
    view.rerender(<TaskDetailPage />);
    useStudio.mockReturnValue(studio("a", context("a", "account-b")));
    view.rerender(<TaskDetailPage />);

    await act(async () => {
      oldA.resolve({ ...taskA, id: "a" });
      oldB.resolve({ ...taskB, id: "b" });
      await Promise.resolve();
    });
    expect(updateData).not.toHaveBeenCalled();
    await act(async () => {
      currentA.resolve({ ...taskA, id: "a" });
      await Promise.resolve();
    });
    expect(updateData).toHaveBeenCalledOnce();
  });

  it("同ID换账号时首屏隐藏旧详情且旧预览不得写入新账号", async () => {
    const preview = deferred<StudioAsset | undefined>();
    const detailB = deferred<StudioTask>();
    const updateData = vi.fn();
    const layoutSnapshots: string[] = [];
    let value = studio(taskA.id, {
      state: {
        ...createState("task-detail"),
        selectedTaskId: taskA.id,
        selectedTaskKind: "generation_batch",
        selectedTaskBackendId: taskA.id,
      },
      data: data([
        { ...taskA, backendKind: "generation_batch", backendId: taskA.id },
      ]),
      user: { id: "account-a" } as StudioContextValue["user"],
      updateData,
    });
    loadStudioTaskDetail
      .mockResolvedValueOnce({
        ...taskA,
        backendKind: "generation_batch",
        backendId: taskA.id,
      })
      .mockReturnValueOnce(detailB.promise);
    loadTaskPreview.mockReturnValue(preview.promise);
    useStudio.mockImplementation(() => value);
    function Probe({ account }: { account: string }) {
      useLayoutEffect(() => {
        layoutSnapshots.push(`${account}:${document.body.textContent ?? ""}`);
      }, [account]);
      return <TaskDetailPage />;
    }
    const view = render(<Probe account="account-a" />);
    expect(
      await screen.findByRole("heading", { name: taskA.title }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "预览成片" }));

    value = {
      ...value,
      user: { id: "account-b" } as StudioContextValue["user"],
    };
    view.rerender(<Probe account="account-b" />);
    expect(layoutSnapshots.at(-1)).not.toContain(taskA.title);
    expect(screen.getByText("正在读取任务详情")).toBeInTheDocument();
    await act(async () => {
      preview.resolve({
        id: "old-account-preview",
        name: "旧账号预览",
        kind: "video",
        url: "/old-account-preview",
        group: "任务结果",
        source: "任务中心",
        saved: true,
      });
      await Promise.resolve();
    });
    expect(updateData).toHaveBeenCalledTimes(1);
  });

  it("真实口播任务调整脚本直接带回实际正文，不落入旧任务面板", () => {
    const task = {
      ...taskA,
      backendKind: "oral_task" as const,
      backendId: "oral-1",
      driverMode: "text" as const,
      scriptText: "实际用于生成的口播稿",
      ipId: "person-a",
      avatarId: "avatar-a",
      voiceId: "voice-a",
    };
    const value = studio(task.id, { data: data([task]) });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "调整脚本" }));

    expect(value.patchState).toHaveBeenCalledWith({
      draft: expect.objectContaining({
        ipId: "person-a",
        avatarId: "avatar-a",
        voiceId: "voice-a",
        script: expect.objectContaining({
          text: task.scriptText,
          confirmed: false,
        }),
      }),
    });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      returnTo: "task-detail",
    });
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("真实音频口播再创作回到音频模式，未提供正文时禁用调整脚本", () => {
    const task = {
      ...taskA,
      backendKind: "oral_task" as const,
      backendId: "oral-audio-1",
      driverMode: "audio" as const,
      audioId: "audio-original",
      ipId: "person-a",
      avatarId: "avatar-a",
    };
    const value = studio(task.id, { data: data([task]) });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    expect(screen.getByRole("button", { name: "调整脚本" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "新建草稿" }));

    expect(value.navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: "task-detail",
    });
    expect(value.patchState).toHaveBeenCalledWith({
      draft: expect.objectContaining({ audioId: "audio-original" }),
    });
  });

  it("仅在用户点击后按需加载，并只回填发起任务的结果", async () => {
    const pending = deferred<StudioAsset | undefined>();
    const value = studio(taskA.id, { data: data([taskA, taskB]) });
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockReturnValue(pending.promise);
    render(<TaskDetailPage />);

    expect(loadTaskPreview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "预览成片" }));
    expect(loadTaskPreview).toHaveBeenCalledWith(taskA);
    expect(
      screen.getByRole("button", { name: "正在加载预览…" }),
    ).toBeDisabled();

    const asset: StudioAsset = {
      id: "result-a",
      name: "张工建房预算 · 首个可用结果",
      kind: "video",
      url: "/signed/result-a",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
    pending.resolve(asset);

    await waitFor(() => expect(value.updateData).toHaveBeenCalledOnce());
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    const next = update(value.data);
    expect(next.assets).toContainEqual(asset);
    expect(next.tasks.find((item) => item.id === taskA.id)?.resultId).toBe(
      asset.id,
    );
    expect(next.tasks.find((item) => item.id === taskB.id)?.resultId).toBe(
      undefined,
    );
  });

  it("显示无结果状态，并允许重新尝试", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockResolvedValue(undefined);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "预览成片" }));
    expect(
      await screen.findByText("该批次暂时没有可预览的成功结果。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新尝试" }));
    expect(loadTaskPreview).toHaveBeenCalledTimes(2);
  });

  it("失败后显示错误并可重试", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockRejectedValue(new Error("preview unavailable"));
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "预览成片" }));
    expect(
      await screen.findByText("预览加载失败，请重试。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
    expect(loadTaskPreview).toHaveBeenCalledTimes(2);
  });

  it("切换任务后忽略旧请求的异步结果", async () => {
    const pending = deferred<StudioAsset | undefined>();
    const updateData = vi.fn();
    let value = studio(taskA.id, {
      data: data([taskA, taskB]),
      updateData,
    });
    useStudio.mockImplementation(() => value);
    loadTaskPreview.mockReturnValue(pending.promise);
    const view = render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "预览成片" }));
    value = studio(taskB.id, {
      data: data([taskA, taskB]),
      updateData,
    });
    view.rerender(<TaskDetailPage />);
    expect(
      screen.getByRole("heading", { name: taskB.title }),
    ).toBeInTheDocument();

    pending.resolve({
      id: "stale-result",
      name: "旧任务结果",
      kind: "video",
      url: "/signed/stale",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    });
    await pending.promise;
    await Promise.resolve();
    expect(updateData).not.toHaveBeenCalled();
  });

  it("审核示例不请求真实预览接口", () => {
    useStudio.mockReturnValue(studio(taskA.id, { review: true }));
    render(<TaskDetailPage />);

    expect(
      screen.queryByRole("button", { name: "预览成片" }),
    ).not.toBeInTheDocument();
    expect(loadTaskPreview).not.toHaveBeenCalled();
  });

  it("口播成片下载直接使用结果资产，不再打开旧任务面板", async () => {
    const oralTask: StudioTask = {
      ...taskA,
      id: "oral-visible-id",
      backendKind: "oral_task",
      backendId: "oral-backend-id",
      resultId: "oral-result-asset",
      batchId: undefined,
    };
    const value = studio(oralTask.id, { data: data([oralTask]) });
    useStudio.mockReturnValue(value);
    downloadStudioTaskResult.mockResolvedValue(undefined);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "下载成片" }));

    await waitFor(() =>
      expect(downloadStudioTaskResult).toHaveBeenCalledWith(oralTask),
    );
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("提交结果不确定时只提供状态核对入口", () => {
    const uncertain: StudioTask = {
      ...taskA,
      id: "oral-uncertain",
      backendKind: "oral_task",
      backendId: "oral-uncertain",
      backendStatus: "SUBMISSION_UNCERTAIN",
      status: "uncertain",
      retryAction: undefined,
      resultId: undefined,
    };
    const value = studio(uncertain.id, { data: data([uncertain]) });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "核对任务状态" }));

    expect(value.openLive).toHaveBeenCalledWith("tasks");
    expect(retryStudioTask).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "重试提交" }),
    ).not.toBeInTheDocument();
  });
});

describe("V1.4 工作台正在进行行", () => {
  const clock = new Date("2026-09-06T10:00:00");

  beforeEach(() => {
    useStudio.mockReset();
    // Fake only the clock: waitFor relies on real timers.
    vi.useFakeTimers({ now: clock, toFake: ["Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function workbench(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("workbench") },
      data: data([runningTask]),
      ...overrides,
    });
  }

  it("爆款来源未上传时在工作台展示原视频上传引导", () => {
    const state = createState("workbench");
    state.draft.sourceId = "viral-source";
    const videos: StudioVideo[] = [
      {
        id: "viral-source",
        title: "建房预算参考",
        author: "作者",
        platform: "抖音",
        category: "建房预算",
        poster: "/studio/source-preview.jpg",
        duration: "00:30",
        likes: 0,
        collections: null,
        shares: null,
        description: "参考标题",
      },
    ];
    useStudio.mockReturnValue(workbench({ state, data: data([], videos) }));
    render(<WorkbenchPage />);

    expect(screen.getByText(/已选参考：建房预算参考/)).toBeInTheDocument();
    expect(
      screen.getByText(/请上传该视频的 MP4 或 MOV 文件/),
    ).toBeInTheDocument();
  });

  it("每行提供更多操作菜单：打开任务中心与复制任务编号", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(<WorkbenchPage />);

    const trigger = screen.getByRole("button", {
      name: "更多操作：张工 · 建房预算",
    });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("menuitem", { name: "打开任务中心" }));
    expect(value.navigate).toHaveBeenCalledWith("tasks");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: "复制任务编号" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("batch-9"));
    expect(value.notify).toHaveBeenCalledWith("任务编号已复制");
  });

  it("复制失败时给出可感知的失败提示", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "更多操作：张工 · 建房预算" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "复制任务编号" }));
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "复制失败，请手动复制任务编号。",
      ),
    );
    expect(writeText).toHaveBeenCalledWith("batch-9");
  });

  it("任务动态与正在进行行不显示原始 ISO 时间", () => {
    useStudio.mockReturnValue(workbench());
    render(<WorkbenchPage />);

    expect(screen.queryByText("2026-09-06T09:30:00")).not.toBeInTheDocument();
    expect(screen.getByText("今天 09:30")).toBeInTheDocument();
    expect(screen.getByText("68%")).toBeInTheDocument();
  });
});

describe("V1.4 工作台新版首页布局", () => {
  const videos: StudioVideo[] = Array.from({ length: 7 }, (_, index) => ({
    id: `video-${index + 1}`,
    title: `灵感视频 ${index + 1}`,
    author: `作者 ${index + 1}`,
    platform: index % 2 === 0 ? "抖音" : "视频号",
    category: "乡墅",
    poster: `/poster-${index + 1}.jpg`,
    duration: "00:56",
    likes: 128000 - index * 1000,
    collections: 100,
    shares: 20,
    description: "乡墅爆款案例",
  }));

  beforeEach(() => {
    window.sessionStorage.clear();
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
  });

  it("展示新版主标题、居中辅助文案、五个竖屏爆款与四个快捷入口", () => {
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([runningTask], videos),
    });
    useStudio.mockReturnValue(value);

    render(<WorkbenchPage />);

    expect(
      screen.getByRole("heading", {
        name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/链接解析当前支持抖音视频/)).toHaveClass(
      "studio-start-helper",
    );
    expect(
      screen.getByRole("heading", { name: "爆款视频精选" }),
    ).toBeInTheDocument();
    expect(screen.getByText("灵感视频 5")).toBeInTheDocument();
    expect(screen.queryByText("灵感视频 6")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /用它复刻/ })).toHaveLength(5);
    expect(
      screen.getByRole("button", { name: "查看全部爆款" }),
    ).toBeInTheDocument();
    for (const name of ["文案工坊", "人物库", "素材库", "数据看板"]) {
      expect(
        screen.getByRole("button", { name: `快捷入口：${name}` }),
      ).toBeInTheDocument();
    }
  });

  it("审核首页从爆款卡片开始复刻时复用既有草稿与导航流程", () => {
    const value = studio(undefined, {
      review: true,
      state: createState("workbench"),
      data: data([], videos),
    });
    useStudio.mockReturnValue(value);

    render(<WorkbenchPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );

    expect(value.patchDraft).toHaveBeenCalledWith({ sourceId: "video-1" });
    expect(value.navigate).toHaveBeenCalledWith("replica", {
      selectedVideoId: "video-1",
      returnTo: "workbench",
    });
  });

  it("首页精选按热度和稳定 ID 确定排序，不依赖接口数组顺序", () => {
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([], [...videos].reverse()),
    });
    useStudio.mockReturnValue(value);

    render(<WorkbenchPage />);

    expect(screen.getByText("灵感视频 1")).toBeInTheDocument();
    expect(screen.getByText("灵感视频 5")).toBeInTheDocument();
    expect(screen.queryByText("灵感视频 6")).not.toBeInTheDocument();
  });

  it("真实爆款从首页复刻时先导入项目素材", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "import-home",
      status: "SUCCEEDED",
      projectId: "project-home",
      sourceAssetId: "asset-home",
      canAnalyze: true,
    });
    const source = {
      ...videos[0],
      platformKey: "douyin" as const,
      nativeId: "native-home",
    };
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-home",
        sourceId: "asset-home",
        sourceAssetId: "asset-home",
      }),
    );
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-home",
      "replica",
      expect.any(String),
    );
  });

  it("首页爆款永久失败后使用新幂等键重试", async () => {
    createViralImportTask
      .mockResolvedValueOnce({
        id: "home-expired",
        status: "FAILED",
        retryable: false,
        errorMessage: "首页来源已失效",
      })
      .mockResolvedValueOnce({
        id: "home-replacement",
        status: "SUCCEEDED",
        projectId: "home-project-new",
        sourceAssetId: "home-asset-new",
        canAnalyze: true,
      });
    const source = {
      ...videos[0],
      platformKey: "douyin" as const,
      nativeId: "native-home-expired",
    };
    const value = studio(undefined, {
      user: {
        id: "customer-home",
        username: "customer-home",
        display_name: "首页客户",
        role: "customer",
      },
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("首页来源已失效"),
    );
    const failedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).not.toBe(failedKey);
  });

  it("首页爆款畸形成功后不写草稿且使用新幂等键重试", async () => {
    createViralImportTask.mockResolvedValue({
      id: "home-malformed",
      status: "SUCCEEDED",
      projectId: "home-project",
      sourceAssetId: "home-audio",
      canAnalyze: false,
    });
    const source = {
      ...videos[0],
      platformKey: "douyin" as const,
      nativeId: "native-home-malformed",
    };
    const value = studio(undefined, {
      user: {
        id: "customer-home-malformed",
        username: "customer-home-malformed",
        display_name: "首页客户",
        role: "customer",
      },
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("该来源暂不支持视频复刻"),
    );
    const malformedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).not.toBe(malformedKey);
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("正式首页爆款缺少平台原生 ID 时不创建伪复刻项目", () => {
    const source = {
      ...videos[0],
      platformKey: undefined,
      nativeId: undefined,
    };
    const value = studio(undefined, {
      review: false,
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "用它复刻：灵感视频 1" }),
    );

    expect(value.notify).toHaveBeenCalledWith("该视频缺少可导入的平台标识");
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });
});

describe("formatTaskTime", () => {
  const now = new Date("2026-09-06T10:00:00");

  it("当天显示“今天 HH:mm”", () => {
    expect(formatTaskTime("2026-09-06T09:30:00", now)).toBe("今天 09:30");
    expect(formatTaskTime("2026-09-06 00:05", now)).toBe("今天 00:05");
  });

  it("前一天显示“昨天 HH:mm”", () => {
    expect(formatTaskTime("2026-09-05T23:58:00", now)).toBe("昨天 23:58");
  });

  it("同年更早显示“MM-DD HH:mm”，跨年带年份", () => {
    expect(formatTaskTime("2026-09-03T14:00:00", now)).toBe("09-03 14:00");
    expect(formatTaskTime("2025-12-31T08:05:00", now)).toBe("2025-12-31 08:05");
  });

  it("无法解析或已是友好文案时原样返回", () => {
    expect(formatTaskTime("今天 09:30", now)).toBe("今天 09:30");
    expect(formatTaskTime("", now)).toBe("");
  });
});

describe("V1.4 工作台上传与创作入口", () => {
  beforeEach(() => {
    useStudio.mockReset();
    uploadWorkbenchSourceVideo.mockReset();
    resolveViralLink.mockReset();
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
  });

  function workbench(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("workbench") },
      data: data([]),
      ...overrides,
    });
  }

  function changeFile(name: string) {
    const input = screen.getByLabelText("选择视频文件");
    fireEvent.change(input, {
      target: { files: [new File(["video"], name, { type: "video/mp4" })] },
    });
  }

  it("审核模式点击上传图标只提示，不进入真实上传", () => {
    const value = workbench({ review: true });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(screen.getByRole("button", { name: "上传视频" }));
    expect(value.notify).toHaveBeenCalledWith("审核示例不执行真实上传。");
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("非 MP4/MOV 文件被拒收并提示", () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    changeFile("相册导出.avi");
    expect(value.notify).toHaveBeenCalledWith(
      "目前仅支持 MP4 / MOV 视频文件。",
    );
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("上传成功：同步项目、来源资产与当前草稿", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const pending: { resolve?: (value: WorkbenchUploadResult) => void } = {};
    let reportProgress: ((percent: number) => void) | undefined;
    uploadWorkbenchSourceVideo.mockImplementation(
      (_file: File, onProgress: (percent: number) => void) =>
        new Promise<{ projectId: string; assetId: string }>((resolve) => {
          pending.resolve = resolve;
          reportProgress = onProgress;
          onProgress(40);
        }),
    );
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce();
    expect(screen.getByText("正在上传 乡墅案例.mp4… 40%")).toBeInTheDocument();
    act(() => reportProgress?.(100));
    expect(screen.getByText("正在上传 乡墅案例.mp4… 100%")).toBeInTheDocument();
    expect(screen.queryByText("已上传云存储：乡墅案例.mp4")).toBeNull();

    pending.resolve?.({
      projectId: "proj-1",
      assetId: "asset-1",
      project: {
        id: "proj-1",
        owner_user_id: "user-1",
        name: "乡墅案例",
        status: "DRAFT",
        reference_asset_id: "asset-1",
        reference_upload_status: "READY",
        analysis_status: "NOT_READY",
      },
      asset: {
        id: "asset-1",
        name: "乡墅案例 · 来源视频",
        kind: "video",
        group: "乡墅案例",
        source: "项目上传",
        saved: true,
      },
    });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "proj-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      }),
    );
    expect(value.updateData).toHaveBeenCalledOnce();
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    expect(update(value.data)).toMatchObject({
      projects: [
        expect.objectContaining({
          id: "proj-1",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
        }),
      ],
      assets: [
        expect.objectContaining({
          id: "asset-1",
          kind: "video",
          saved: true,
        }),
      ],
    });
    expect(screen.getByText("已上传云存储：乡墅案例.mp4")).toBeInTheDocument();
    expect(value.notify).toHaveBeenCalledWith(
      "视频已上传云存储，来源已加入当前创作。",
    );
  });

  it("A 项目编辑后从工作台上传 B，进入复刻前清除 A 的文本归属", async () => {
    const state = createState("workbench");
    state.draft = {
      ...state.draft,
      projectId: "project-a",
      prompt: "A Prompt",
      promptEdited: true,
      script: {
        ...state.draft.script,
        title: "A 标题",
        original: "A 原文",
        text: "A 文案",
      },
      scriptEdited: true,
    };
    const value = workbench({ state });
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "project-b",
      assetId: "asset-b",
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    changeFile("来源B.mp4");
    await waitFor(() => expect(value.patchDraft).toHaveBeenCalledOnce());
    const patch = vi.mocked(value.patchDraft).mock.calls[0][0];
    const next = patchStudioDraft(state.draft, patch);

    expect(next.projectId).toBe("project-b");
    expect(next.prompt).toBe("");
    expect(next.promptEdited).toBe(false);
    expect(next.script).toMatchObject({ title: "", original: "", text: "" });
    expect(next.scriptEdited).toBe(false);
  });

  it("忽略被后一次上传取代的迟到结果与错误", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const first = deferred<WorkbenchUploadResult>();
    const second = deferred<WorkbenchUploadResult>();
    uploadWorkbenchSourceVideo
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    render(<WorkbenchPage />);

    changeFile("来源A.mp4");
    changeFile("来源B.mp4");
    expect(uploadWorkbenchSourceVideo.mock.calls[0]?.[2]?.aborted).toBe(true);

    second.resolve({ projectId: "project-b", assetId: "asset-b" });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-b",
        sourceId: "asset-b",
        sourceAssetId: "asset-b",
      }),
    );

    first.reject(new Error("迟到失败"));
    await first.promise.catch(() => undefined);
    await Promise.resolve();
    expect(value.patchDraft).toHaveBeenCalledTimes(1);
    expect(screen.getByText("已上传云存储：来源B.mp4")).toBeInTheDocument();
    expect(screen.queryByText(/迟到失败/)).toBeNull();
  });

  it("上传完成后开始复刻直接进分镜工作区，不再打开项目面板", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "proj-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    expect(value.navigate).toHaveBeenCalledWith("replica");
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("上传后的提取文案走 script-from-audio 管线，不再打开旧项目面板", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "proj-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(value.extractScriptFromUpload).toHaveBeenCalledTimes(1);
    expect(value.openLive).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("已恢复草稿含来源时，提取文案同样走管线", () => {
    const state = createState("workbench");
    state.draft.sourceId = "proj-9";
    state.draft.projectId = "proj-9";
    const value = workbench({ state });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(value.extractScriptFromUpload).toHaveBeenCalledTimes(1);
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("抖音链接解析后复用爆款导入状态流进入复刻且不重复提交", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const resolution = deferred<import("../api").ViralLinkResolution>();
    resolveViralLink.mockReturnValue(resolution.promise);
    createViralImportTask.mockResolvedValue({
      id: "link-import-1",
      status: "SUCCEEDED",
      projectId: "link-project-1",
      sourceAssetId: "link-asset-1",
      canAnalyze: true,
    });
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: {
        value: "3.28 复制打开抖音 https://v.douyin.com/shareCode/",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    expect(
      screen.getByRole("button", { name: "正在解析链接…" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "正在解析链接…" }));
    expect(resolveViralLink).toHaveBeenCalledWith(
      "3.28 复制打开抖音 https://v.douyin.com/shareCode/",
      "replica",
      expect.any(String),
    );

    resolution.resolve({
      importIdempotencyKey: "stable-link-import-key",
      item: {
        platform: "douyin",
        videoId: "native-link-1",
        category: "链接导入",
        title: "链接乡墅案例",
        author: "作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 30_000,
        likes: 0,
        comments: null,
        shares: null,
        collects: null,
        publishedAt: null,
        publishedDisplay: null,
        likeDisplay: null,
        tags: [],
        hasPlayableAudio: true,
        playUrl: "https://cdn.example/video.mp4",
      },
    });

    await waitFor(() =>
      expect(createViralImportTask).toHaveBeenCalledWith(
        "douyin",
        "native-link-1",
        "replica",
        "stable-link-import-key",
      ),
    );
    expect(value.patchDraft).toHaveBeenCalledWith({
      projectId: "link-project-1",
      sourceId: "link-asset-1",
      sourceAssetId: "link-asset-1",
    });
    expect(value.navigate).toHaveBeenCalledWith("replica", {
      selectedVideoId: "douyin-native-link-1",
      returnTo: "workbench",
    });
  });

  it("链接输入切换后忽略上一请求的迟到响应", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const resolution = deferred<import("../api").ViralLinkResolution>();
    resolveViralLink.mockReturnValue(resolution.promise);
    render(<WorkbenchPage />);

    const input = screen.getByLabelText("视频链接");
    fireEvent.change(input, {
      target: { value: "https://v.douyin.com/old-link/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.change(input, {
      target: { value: "https://v.douyin.com/new-link/" },
    });

    resolution.resolve({
      importIdempotencyKey: "stale-import-key",
      item: {
        platform: "douyin",
        videoId: "stale-link",
        category: "链接导入",
        title: "迟到响应",
        author: "作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 30_000,
        likes: 0,
        comments: null,
        shares: null,
        collects: null,
        publishedAt: null,
        publishedDisplay: null,
        likeDisplay: null,
        tags: [],
        hasPlayableAudio: true,
        playUrl: "https://cdn.example/stale.mp4",
      },
    });
    await act(async () => Promise.resolve());

    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledOnce());
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("账号在 effect 前切换时阻断旧链接响应和后续导入", async () => {
    const accountA = workbench({
      user: { id: "customer-a" } as StudioContextValue["user"],
    });
    const accountB = workbench({
      user: { id: "customer-b" } as StudioContextValue["user"],
    });
    let current = accountA;
    useStudio.mockImplementation(() => current);
    const resolution = deferred<import("../api").ViralLinkResolution>();
    resolveViralLink.mockReturnValue(resolution.promise);
    const view = render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://v.douyin.com/account-a/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    current = accountB;
    view.rerender(<WorkbenchPage />);
    resolution.resolve({
      importIdempotencyKey: "account-a-import",
      item: {
        platform: "douyin",
        videoId: "7345678901234567890",
        category: "链接导入",
        title: "账号 A 链接",
        author: "作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 30_000,
        likes: 0,
        comments: null,
        shares: null,
        collects: null,
        publishedAt: null,
        publishedDisplay: null,
        likeDisplay: null,
        tags: [],
        hasPlayableAudio: true,
        playUrl: "https://cdn.example/account-a.mp4",
      },
    });
    await act(async () => Promise.resolve());

    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledOnce());
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(accountA.updateData).not.toHaveBeenCalled();
    expect(accountB.updateData).not.toHaveBeenCalled();
    expect(accountA.notify).not.toHaveBeenCalled();
    expect(accountB.notify).not.toHaveBeenCalled();
  });

  it("切换账号后立即隐藏旧链接且不能以新账号提交", () => {
    const accountA = workbench({
      user: { id: "customer-a" } as StudioContextValue["user"],
    });
    const accountB = workbench({
      user: { id: "customer-b" } as StudioContextValue["user"],
    });
    let current = accountA;
    useStudio.mockImplementation(() => current);
    const view = render(<WorkbenchPage />);
    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://v.douyin.com/account-a/" },
    });

    current = accountB;
    view.rerender(<WorkbenchPage />);
    expect(screen.getByLabelText("视频链接")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));

    expect(resolveViralLink).not.toHaveBeenCalled();
    expect(accountB.navigate).not.toHaveBeenCalled();
    expect(accountB.extractScriptFromUpload).not.toHaveBeenCalled();
    expect(accountB.openLive).toHaveBeenCalledWith("projects");

    current = accountA;
    view.rerender(<WorkbenchPage />);
    expect(screen.getByLabelText("视频链接")).toHaveValue("");
  });

  it("切换账号后隐藏旧上传且迟到上传不会写入新账号", async () => {
    const accountA = workbench({
      user: { id: "customer-a" } as StudioContextValue["user"],
    });
    const accountB = workbench({
      user: { id: "customer-b" } as StudioContextValue["user"],
    });
    let current = accountA;
    useStudio.mockImplementation(() => current);
    const upload = deferred<WorkbenchUploadResult>();
    uploadWorkbenchSourceVideo.mockReturnValue(upload.promise);
    const view = render(<WorkbenchPage />);
    changeFile("账号A.mp4");
    expect(screen.getByText(/正在上传 账号A\.mp4/)).toBeInTheDocument();

    current = accountB;
    view.rerender(<WorkbenchPage />);
    expect(screen.queryByText(/账号A\.mp4/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(accountB.extractScriptFromUpload).not.toHaveBeenCalled();
    expect(accountB.openLive).toHaveBeenCalledWith("projects");

    upload.resolve({ projectId: "project-a", assetId: "asset-a" });
    await act(async () => upload.promise);
    expect(accountA.patchDraft).not.toHaveBeenCalled();
    expect(accountB.patchDraft).not.toHaveBeenCalled();
    expect(accountA.notify).not.toHaveBeenCalled();
    expect(accountB.notify).not.toHaveBeenCalled();
  });

  it.each([
    "视频号链接暂不支持解析，请上传 MP4 或 MOV 文件。",
    "视频链接已过期，请重新复制链接或上传 MP4/MOV 文件。",
    "链接中没有可用视频，请上传 MP4 或 MOV 文件。",
  ])("链接失败显示上传回退且不创建导入任务：%s", async (message) => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    resolveViralLink.mockRejectedValue(new Error(message));
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://channels.weixin.qq.com/example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("解析结果未知时保留同一幂等键供安全重放", async () => {
    useStudio.mockReturnValue(workbench());
    const error = Object.assign(new Error("视频链接解析结果未知"), {
      status: 503,
      code: "VIRAL_LINK_SUBMISSION_UNCERTAIN",
    });
    resolveViralLink.mockRejectedValue(error);
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://v.douyin.com/retain-key/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledTimes(2));

    expect(resolveViralLink.mock.calls[1]?.[2]).toBe(
      resolveViralLink.mock.calls[0]?.[2],
    );
    expect(createViralImportTask).not.toHaveBeenCalled();
  });

  it("工作台明确说明支持范围、上传格式和解析超时", () => {
    useStudio.mockReturnValue(workbench());
    render(<WorkbenchPage />);

    expect(
      screen.getByText(
        "链接解析当前支持抖音视频；视频号及其他平台请上传 MP4/MOV 文件。解析最长约 60 秒。",
      ),
    ).toBeInTheDocument();
  });
});

describe("V1.4 任务中心列表", () => {
  const clock = new Date("2026-09-06T10:00:00");
  const queuedTask: StudioTask = {
    id: "t-queued",
    batchId: "b-queued",
    title: "三层新中式乡墅",
    type: "视频复刻",
    status: "queued",
    submitted: "2026-09-06T09:32:00",
  };
  const failedTask: StudioTask = {
    id: "t-failed",
    batchId: "b-failed",
    title: "张工 · 庭院讲解首帧",
    type: "人物置换",
    status: "failed",
    submitted: "2026-09-06T09:28:00",
  };
  const doneTask: StudioTask = {
    id: "t-done",
    batchId: "b-done",
    title: "张工 · 建房预算-已确认版",
    type: "数字人口播",
    status: "completed",
    submitted: "2026-09-06T09:25:00",
  };

  beforeEach(() => {
    useStudio.mockReset();
    cancelStudioTask.mockReset();
    loadMoreGenerationTasks.mockReset();
    loadMoreOralTasks.mockReset();
    vi.useFakeTimers({ now: clock, toFake: ["Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function tasksPage(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("tasks") },
      data: data([runningTask, queuedTask, failedTask, doneTask]),
      ...overrides,
    });
  }

  it("状态页签展示进行中/待处理计数", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByRole("tab", { name: "进行中 2" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "待处理 1" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已完成" })).toBeInTheDocument();
  });

  it("普通批次和口播任务使用独立历史游标且按 id 去重追加", async () => {
    const value = tasksPage({
      data: {
        ...data([runningTask, { ...doneTask, backendKind: "oral_task" }]),
        pagination: {
          generationTasks: { nextCursor: "batch-next", total: 21 },
          oralTasks: { loaded: 20, total: 21 },
        },
      },
    });
    useStudio.mockReturnValue(value);
    loadMoreGenerationTasks.mockResolvedValue({
      items: [runningTask, queuedTask],
      nextCursor: null,
      total: 21,
    });
    loadMoreOralTasks.mockResolvedValue({
      items: [{ ...doneTask, id: "oral-deep", backendKind: "oral_task" }],
      loaded: 21,
      total: 21,
    });
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "加载更多普通批次" }));
    await waitFor(() =>
      expect(loadMoreGenerationTasks).toHaveBeenCalledWith("batch-next"),
    );
    const generationUpdate = vi.mocked(value.updateData).mock.calls[0]?.[0];
    const generationData = generationUpdate?.(value.data);
    expect(generationData?.tasks.map((task) => task.id)).toEqual([
      runningTask.id,
      doneTask.id,
      queuedTask.id,
    ]);

    fireEvent.click(screen.getByRole("button", { name: "加载更多口播任务" }));
    await waitFor(() => expect(loadMoreOralTasks).toHaveBeenCalledWith(20));
  });

  it("状态列带子文案：排队中→等待开始、待处理→生成失败", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByText("等待开始")).toBeInTheDocument();
    expect(screen.getByText("生成失败")).toBeInTheDocument();
  });

  it("提交时间本地化，不显示原始 ISO 串", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByText("今天 09:32")).toBeInTheDocument();
    expect(screen.queryByText("2026-09-06T09:32:00")).not.toBeInTheDocument();
  });

  it("排队行提供取消任务：成功后提示并刷新列表", async () => {
    const value = tasksPage();
    useStudio.mockReturnValue(value);
    cancelStudioTask.mockResolvedValue({ billingStatus: "PENDING" });
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    expect(cancelStudioTask).toHaveBeenCalledWith(queuedTask);
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "任务已取消，计费状态处理中，请稍后刷新核对。",
      ),
    );
    expect(value.refresh).toHaveBeenCalledOnce();
  });

  it("取消失败时给出可感知的错误提示", async () => {
    const value = tasksPage();
    useStudio.mockReturnValue(value);
    cancelStudioTask.mockRejectedValue(new Error("任务已在提交中"));
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("任务已在提交中"),
    );
    expect(value.refresh).not.toHaveBeenCalled();
  });

  it("审核模式点击取消只提示，不调用接口", () => {
    const value = tasksPage({ review: true });
    useStudio.mockReturnValue(value);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    expect(cancelStudioTask).not.toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("审核示例不执行真实取消。");
  });

  it("非排队行不出现取消入口", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getAllByRole("button", { name: "取消任务" })).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "查看结果" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "查看详情" })).toHaveLength(2);
  });

  it("口播任务只有后端 QUEUED 状态可取消", () => {
    const submittingOral: StudioTask = {
      ...queuedTask,
      id: "oral-submitting",
      backendKind: "oral_task",
      backendId: "oral-submitting",
      backendStatus: "SUBMITTING",
      type: "数字人口播",
    };
    useStudio.mockReturnValue(
      tasksPage({ data: data([submittingOral, queuedTask]) }),
    );
    render(<TasksPage />);

    expect(screen.getAllByRole("button", { name: "取消任务" })).toHaveLength(1);
  });

  it("类型筛选收进单行下拉，菜单项计数与状态筛选联动", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(
      screen.queryByRole("tab", { name: "视频复刻" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    expect(
      screen.getByRole("option", { name: "✓ 全部类型 4" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "数字人口播 2" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "视频复刻 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "人物置换 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "视频生成 0" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "进行中 2" }));
    expect(
      screen.getByRole("option", { name: "数字人口播 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "视频复刻 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "人物置换 0" }),
    ).toBeInTheDocument();
  });

  it("选择类型后过滤表格、高亮触发按钮并更新结果计数", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByText("共 4 条任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    fireEvent.click(screen.getByRole("option", { name: "数字人口播 2" }));

    const trigger = screen.getByRole("button", { name: "类型：数字人口播" });
    expect(trigger).toHaveClass("is-active");
    expect(screen.getByText("共 2 条任务")).toBeInTheDocument();
    expect(screen.getByText("张工 · 建房预算")).toBeInTheDocument();
    expect(screen.queryByText("三层新中式乡墅")).not.toBeInTheDocument();
  });

  it("筛选无结果时提供清除筛选出口，一键复位两个维度", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("tab", { name: "待处理 1" }));
    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    fireEvent.click(screen.getByRole("option", { name: "视频复刻 0" }));

    expect(screen.getByText("当前筛选下暂无任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));

    expect(
      screen.getByRole("tab", { name: "全部", selected: true }),
    ).toBeInTheDocument();
    expect(screen.getByText("共 4 条任务")).toBeInTheDocument();
    expect(screen.getByText("三层新中式乡墅")).toBeInTheDocument();
  });

  it("类型下拉支持 Escape 与点选外部关闭", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    expect(screen.getByRole("listbox", { name: "类型" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(
      screen.queryByRole("listbox", { name: "类型" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    fireEvent.pointerDown(screen.getByRole("heading", { name: "任务中心" }));
    expect(
      screen.queryByRole("listbox", { name: "类型" }),
    ).not.toBeInTheDocument();
  });
});

describe("V1.4 个人中心通知偏好（C10b）", () => {
  beforeEach(() => {
    useStudio.mockReset();
    getStudioNotificationPreferences.mockReset();
    updateStudioNotificationPreferences.mockReset();
  });

  it("生产模式拉取偏好并保存开关状态", async () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    updateStudioNotificationPreferences.mockResolvedValue({ enabled: false });
    useStudio.mockReturnValue(studio());
    render(<ProfilePage />);

    const toggle = await screen.findByRole("button", { name: "通知偏好" });
    await waitFor(() => expect(toggle).toHaveTextContent("开启"));
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(updateStudioNotificationPreferences).toHaveBeenCalledWith(false),
    );
    await waitFor(() => expect(toggle).toHaveTextContent("关闭"));
  });

  it("账户概览区分真实零余额、未知状态和读取失败", () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    useStudio.mockReturnValue(
      studio(undefined, {
        user: {
          id: "customer-1",
          username: "customer-1",
          display_name: "客户一",
          role: "customer",
        },
      }),
    );
    const retryWallet = vi.fn();
    const { rerender } = render(
      <ProfilePage
        accountSummary={{
          walletStatus: "ready",
          availableCredits: 0,
          retryWallet,
          profile: null,
          profileLoadError: "",
        }}
      />,
    );
    expect(screen.getByText("0 秒")).toBeInTheDocument();

    rerender(
      <ProfilePage
        accountSummary={{
          walletStatus: "unknown",
          availableCredits: null,
          retryWallet,
          profile: null,
          profileLoadError: "",
        }}
      />,
    );
    expect(screen.getAllByText("未查询")).toHaveLength(2);

    rerender(
      <ProfilePage
        accountSummary={{
          walletStatus: "error",
          availableCredits: null,
          retryWallet,
          profile: null,
          profileLoadError: "资料读取失败",
        }}
      />,
    );
    expect(screen.getByText("读取失败")).toBeInTheDocument();
    expect(screen.getByText("资料读取失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试余额查询" }));
    expect(retryWallet).toHaveBeenCalledOnce();
  });

  it("资料读取失败可重试，并在资料刷新成功后移除错误", () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    useStudio.mockReturnValue(studio());
    const retryProfile = vi.fn();
    const accountSummary = {
      walletStatus: "ready" as const,
      availableCredits: 8,
      retryWallet: vi.fn(),
      retryProfile,
      profile: null,
      profileLoadError: "账号资料加载失败，请稍后重试。",
    };
    const view = render(<ProfilePage accountSummary={accountSummary} />);

    fireEvent.click(screen.getByRole("button", { name: "重试资料查询" }));
    expect(retryProfile).toHaveBeenCalledOnce();

    view.rerender(
      <ProfilePage
        accountSummary={{ ...accountSummary, profileLoadError: "" }}
      />,
    );
    expect(screen.queryByText("账号资料加载失败，请稍后重试。")).toBeNull();
    expect(screen.queryByRole("button", { name: "重试资料查询" })).toBeNull();
  });

  it("保存失败时回退开关状态并提示", async () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    updateStudioNotificationPreferences.mockRejectedValue(
      new Error("保存通知偏好失败"),
    );
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ProfilePage />);

    const toggle = await screen.findByRole("button", { name: "通知偏好" });
    await waitFor(() => expect(toggle).toHaveTextContent("开启"));
    fireEvent.click(toggle);

    await waitFor(() => expect(value.notify).toHaveBeenCalled());
    expect(toggle).toHaveTextContent("开启");
  });

  it("加载失败时开关置灰为 —，审核模式点击只提示不保存", async () => {
    getStudioNotificationPreferences.mockRejectedValue(new Error("网络错误"));
    useStudio.mockReturnValue(studio());
    render(<ProfilePage />);
    const toggle = await screen.findByRole("button", { name: "通知偏好" });
    await waitFor(() => expect(toggle).toHaveTextContent("—"));
    expect(toggle).toBeDisabled();

    useStudio.mockReturnValue(studio(undefined, { review: true }));
    const { unmount } = render(<ProfilePage />);
    const reviewToggle = screen
      .getAllByRole("button", { name: "通知偏好" })
      .at(-1);
    expect(reviewToggle).toBeDefined();
    if (!reviewToggle) throw new Error("审核模式缺少通知偏好开关");
    await waitFor(() => expect(reviewToggle).toHaveTextContent("开启"));
    fireEvent.click(reviewToggle);
    expect(updateStudioNotificationPreferences).not.toHaveBeenCalled();
    unmount();
  });
});
