import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode, useLayoutEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Project } from "../api";
import { saveReplicaPreference } from "./AdaptiveReplicaSetup";
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
  durationSeconds?: number;
  project?: Project;
  asset?: StudioAsset;
};

const {
  useStudio,
  loadTaskPreview,
  loadViralVideos,
  saveViralFavorite,
  removeViralFavorite,
  uploadWorkbenchSourceVideo,
  readVideoDuration,
  studioVideoFromViral,
  cancelStudioTask,
  downloadStudioTaskResult,
  retryStudioTask,
  renameStudioGenerationTask,
  removeStudioGenerationTask,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  createViralImportTask,
  resolveViralLink,
  getViralImportTask,
  getMaterialBatchPreviews,
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
  PUBLISH_PLATFORM_LABELS,
  connectPublishAccount,
  loadPublishAccounts,
  removePublishAccount,
  requestPublishAccountVerify,
  extractLocalVideoAudio,
  findActiveReplicaPipeline,
  initiateUploadedReplicaPipeline,
  listReplicaPipelineCharacters,
  fetchReplicaPipeline,
} = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
  findActiveReplicaPipeline: vi.fn(async () => ({ pipelineId: null })),
  initiateUploadedReplicaPipeline: vi.fn(async () => ({
    id: "pipeline-upload",
    projectId: "proj-1",
    status: "ENQUEUED",
    replay: false,
  })),
  listReplicaPipelineCharacters: vi.fn(async () => ({
    characters: [
      { characterVersionId: "cv-upload", name: "默认人物", versionNumber: 1 },
    ],
  })),
  fetchReplicaPipeline: vi.fn(async () => ({
    id: "pipeline-upload",
    projectId: "proj-1",
    status: "ANALYZING",
    stage: "analyze",
  })),
  loadTaskPreview: vi.fn(),
  loadViralVideos: vi.fn<typeof import("./live").loadViralVideos>(async () => ({
    videos: [],
    errors: [],
  })),
  saveViralFavorite: vi.fn(async () => ({ isFavorite: true })),
  removeViralFavorite: vi.fn(async () => ({ isFavorite: false })),
  uploadWorkbenchSourceVideo:
    vi.fn<
      (
        file: File,
        onProgress: (percent: number) => void,
        signal?: AbortSignal,
        purpose?: "replica" | "script",
      ) => Promise<WorkbenchUploadResult>
    >(),
  readVideoDuration: vi.fn<(file: File) => Promise<number>>(),
  extractLocalVideoAudio: vi.fn<(file: File) => Promise<File>>(),
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
  renameStudioGenerationTask: vi.fn(),
  removeStudioGenerationTask: vi.fn(),
  loadMoreGenerationTasks: vi.fn(),
  loadMoreOralTasks: vi.fn(),
  loadStudioTaskDetail: vi.fn(),
  createViralImportTask: vi.fn(),
  resolveViralLink: vi.fn(),
  getViralImportTask: vi.fn(),
  getMaterialBatchPreviews: vi.fn(async () => ({
    previews: {},
    thumbnails: {},
  })),
  getStudioNotificationPreferences: vi.fn(),
  updateStudioNotificationPreferences: vi.fn(),
  // C5 发布账号（第一阶段）：live.ts 的这几个导出在测试里由替身接管。
  // PUBLISH_PLATFORM_LABELS 是常量映射而非函数，给出同形状字面量即可。
  PUBLISH_PLATFORM_LABELS: { douyin: "抖音", wechat_channels: "视频号" },
  connectPublishAccount: vi.fn(),
  loadPublishAccounts: vi.fn(),
  removePublishAccount: vi.fn(),
  requestPublishAccountVerify: vi.fn(),
}));

const {
  publishBrowserRequest,
  nativeInvoke,
  nativeIsTauri,
  archiveGenerationTask,
  createGenerationResultPreviewUrl,
} = vi.hoisted(() => ({
  publishBrowserRequest: vi.fn(),
  archiveGenerationTask: vi.fn(),
  createGenerationResultPreviewUrl: vi.fn(),
  nativeInvoke: vi.fn(),
  nativeIsTauri: vi.fn(() => false),
}));
vi.mock("@tauri-apps/api/core", () => ({
  invoke: nativeInvoke,
  isTauri: nativeIsTauri,
}));

vi.mock("./context", () => ({ useStudio }));
vi.mock("./viralCache", () => ({
  cacheAvailable: vi.fn(() => false),
  extractLocalVideoAudio,
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  findActiveReplicaPipeline,
  initiateUploadedReplicaPipeline,
  listReplicaPipelineCharacters,
  fetchReplicaPipeline,
  createViralImportTask,
  resolveViralLink,
  getViralImportTask,
  getMaterialBatchPreviews,
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
  publishBrowserRequest,
  archiveGenerationTask,
  createGenerationResultPreviewUrl,
  saveViralFavorite,
  removeViralFavorite,
}));
vi.mock("./live", () => ({
  loadTaskPreview,
  loadViralVideos,
  uploadWorkbenchSourceVideo,
  readVideoDuration,
  studioVideoFromViral,
  cancelStudioTask,
  downloadStudioTaskResult,
  retryStudioTask,
  renameStudioGenerationTask,
  removeStudioGenerationTask,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  PUBLISH_PLATFORM_LABELS,
  connectPublishAccount,
  loadPublishAccounts,
  removePublishAccount,
  requestPublishAccountVerify,
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
    homepageVideos: videos.filter((video) => video.homepageFeatured === true),
    tasks,
    projects: [],
    errors: [],
    materials: [],
    loading: false,
    stats: null,
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
    discardSavedDraft: vi.fn(),
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
    renameStudioGenerationTask.mockReset();
    loadStudioTaskDetail.mockReset();
  });

  it("临时成片从任务详情归档后，以返回的新素材编号进入发布", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockResolvedValue({
      id: "direct-1",
      name: "临时成片",
      kind: "video",
      url: "/signed/direct",
      source: "任务中心",
      group: "任务结果",
      delivery: "direct",
      generationTaskId: "generation-1",
      saved: false,
    });
    archiveGenerationTask.mockResolvedValueOnce({
      result_asset_id: "archived-1",
    });
    createGenerationResultPreviewUrl.mockResolvedValueOnce("/signed/archive");
    render(<TaskDetailPage />);
    const save = await screen.findByRole("button", { name: "保存成片" });
    expect(screen.getByRole("button", { name: "去发布管理" })).toBeDisabled();
    fireEvent.click(save);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "去发布管理" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "去发布管理" }));
    expect(archiveGenerationTask).toHaveBeenCalledExactlyOnceWith(
      "generation-1",
    );
    expect(value.navigate).toHaveBeenCalledWith("publishing", {
      selectedAssetId: "archived-1",
    });
  });

  it("换素材回到原任务的创作类型并打开创作选择器", () => {
    const value = studio(taskA.id, {
      data: data([
        {
          ...taskA,
          type: "视频生成",
          draftSnapshot: {
            ...createState("video").draft,
            prompt: "原提示",
            referenceIds: ["frame"],
          },
        },
      ]),
      review: true,
    });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "更换素材" }));
    expect(value.navigate).toHaveBeenCalledWith(
      "reference",
      expect.objectContaining({
        draft: expect.objectContaining({
          prompt: "原提示",
          referenceIds: ["frame"],
        }),
        returnTo: "task-detail",
      }),
    );
    expect(value.openPicker).toHaveBeenCalledWith("reference");
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
        returnTo: "workbench",
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

  it("历史音频口播仍可查看成片，再创作使用待补全文案和声音的新稿", () => {
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

    expect(loadTaskPreview).toHaveBeenCalledWith(task);
    expect(screen.getByText("完整口播音频")).toBeInTheDocument();
    expect(value.navigate).toHaveBeenCalledWith("oral", {
      returnTo: "task-detail",
    });
    expect(value.patchState).toHaveBeenCalledWith({
      draft: expect.objectContaining({
        driverMode: "text",
        ipId: "person-a",
        avatarId: "avatar-a",
        voiceId: undefined,
        script: expect.objectContaining({ text: "", confirmed: false }),
      }),
    });
  });

  it("打开已完成任务立即加载成片，并只回填发起任务的结果", async () => {
    const pending = deferred<StudioAsset | undefined>();
    const value = studio(taskA.id, { data: data([taskA, taskB]) });
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockReturnValue(pending.promise);
    render(<TaskDetailPage />);

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

  it("后台归档后轮询得到的新资产取代已加载的供应商直链", async () => {
    const direct: StudioAsset = {
      id: "direct-task-old-result",
      name: "旧直链",
      kind: "video",
      url: "/signed/provider-original",
      group: "任务结果",
      source: "任务中心",
      saved: false,
      delivery: "direct",
      generationTaskId: "provider-result-a",
    };
    const archived: StudioAsset = {
      id: "normalized-result-asset",
      name: "已归档规范化成片",
      kind: "video",
      url: "/signed/archived-normalized",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
    let value = studio();
    useStudio.mockImplementation(() => value);
    loadTaskPreview.mockResolvedValue(direct);
    const view = render(<TaskDetailPage />);
    await waitFor(() =>
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        direct.url,
      ),
    );
    // Background archive/normalization can finish independently of this page's
    // Save button. Polling now carries the authoritative physical result ID.
    value = {
      ...value,
      data: {
        ...value.data,
        tasks: [{ ...taskA, resultId: archived.id }],
        assets: [direct, archived],
      },
    };
    view.rerender(<TaskDetailPage />);
    expect(view.container.querySelector("video")).toHaveAttribute(
      "src",
      archived.url,
    );
    // IA 改版第 5 项：素材库换源后「查看素材」入口下线；归档成片仍可直接去发布。
    expect(screen.queryByRole("button", { name: "查看素材" })).toBeNull();
    expect(screen.getByRole("button", { name: "去发布管理" })).toBeEnabled();
  });

  it("轮询只返回新归档ID时忽略迟到直链并允许签发新资产", async () => {
    const pending = deferred<StudioAsset | undefined>();
    let value = studio();
    useStudio.mockImplementation(() => value);
    loadTaskPreview.mockReturnValueOnce(pending.promise).mockResolvedValueOnce({
      id: "new-archive-id",
      name: "归档成片",
      kind: "video",
      url: "/signed/new-archive",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    });
    const view = render(<TaskDetailPage />);
    value = {
      ...value,
      data: {
        ...value.data,
        tasks: [{ ...taskA, resultId: "new-archive-id" }],
      },
    };
    view.rerender(<TaskDetailPage />);
    await act(async () => {
      pending.resolve({
        id: "direct-task-old",
        name: "迟到直链",
        kind: "video",
        url: "/signed/stale-direct",
        group: "任务结果",
        source: "任务中心",
        saved: false,
      });
    });
    await waitFor(() =>
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        "/signed/new-archive",
      ),
    );
  });

  it("成片签名URL播放失败后允许重新签发预览", async () => {
    const asset: StudioAsset = {
      id: "expiring-result",
      name: "已归档成片",
      kind: "video",
      url: "/signed/expired-result",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
    useStudio.mockReturnValue(studio());
    loadTaskPreview
      .mockResolvedValueOnce(asset)
      .mockResolvedValueOnce({ ...asset, url: "/signed/refreshed-result" });
    const view = render(<TaskDetailPage />);
    const video = await waitFor(() => {
      const element = view.container.querySelector("video");
      expect(element).toHaveAttribute("src", asset.url);
      return element as HTMLVideoElement;
    });
    fireEvent.error(video);
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
    await waitFor(() =>
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        "/signed/refreshed-result",
      ),
    );
    expect(loadTaskPreview).toHaveBeenCalledTimes(2);
  });

  it("显示无结果状态，并允许重新尝试", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockResolvedValue(undefined);
    render(<TaskDetailPage />);

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
    loadTaskPreview
      .mockReturnValueOnce(pending.promise)
      .mockReturnValue(new Promise(() => {}));
    const view = render(<TaskDetailPage />);

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

  it("部分完成的生成批次在详情页照常加载已成功的成片（FIX-01）", async () => {
    const partial: StudioTask = {
      ...taskA,
      type: "视频生成",
      backendKind: "generation_batch",
      backendId: taskA.id,
      status: "partial",
      deliverableCount: 3,
    };
    loadStudioTaskDetail.mockResolvedValue(partial);
    loadTaskPreview.mockReturnValue(new Promise(() => {}));
    useStudio.mockReturnValue(
      studio(taskA.id, {
        state: {
          ...createState("task-detail"),
          selectedTaskId: taskA.id,
          selectedTaskKind: "generation_batch",
          selectedTaskBackendId: taskA.id,
        },
        data: data([partial]),
      }),
    );
    render(<TaskDetailPage />);

    await waitFor(() => expect(loadTaskPreview).toHaveBeenCalled());
    expect(screen.getByText("部分完成")).toBeInTheDocument();
    expect(
      screen.getByText(/本批已有 3 条成片可查看，其余未生成成功/),
    ).toBeInTheDocument();
  });

  it("没有成功成片的待核对批次不请求预览，也不显示为失败", async () => {
    const uncertain: StudioTask = {
      ...taskA,
      type: "视频生成",
      backendKind: "generation_batch",
      backendId: taskA.id,
      status: "uncertain",
      deliverableCount: 0,
    };
    loadStudioTaskDetail.mockResolvedValue(uncertain);
    useStudio.mockReturnValue(
      studio(taskA.id, {
        state: {
          ...createState("task-detail"),
          selectedTaskId: taskA.id,
          selectedTaskKind: "generation_batch",
          selectedTaskBackendId: taskA.id,
        },
        data: data([uncertain]),
      }),
    );
    render(<TaskDetailPage />);

    expect(await screen.findByText("状态待确认")).toBeInTheDocument();
    expect(loadTaskPreview).not.toHaveBeenCalled();
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
});

describe("V1.4 工作台首页爆款精选", () => {
  const clock = new Date("2026-11-17T12:00:00Z");

  beforeEach(() => {
    useStudio.mockReset();
    loadViralVideos.mockReset().mockResolvedValue({ videos: [], errors: [] });
    saveViralFavorite.mockReset().mockResolvedValue({ isFavorite: true });
    removeViralFavorite.mockReset().mockResolvedValue({ isFavorite: false });
    vi.useFakeTimers({ now: clock, toFake: ["Date"] });
  });

  afterEach(() => {
    loadViralVideos.mockReset().mockResolvedValue({ videos: [], errors: [] });
    vi.useRealTimers();
  });

  function homepageVideo(
    index: number,
    overrides: Partial<StudioVideo> = {},
  ): StudioVideo {
    return {
      id: `homepage-${index}`,
      nativeId: `homepage-${index}`,
      platformKey: "douyin",
      platform: "抖音",
      title: `精选视频 ${index}`,
      author: "管理员上传",
      category: "乡墅",
      poster: `/poster-${index}.jpg`,
      duration: "00:10",
      durationMs: 10_000,
      likes: 1000,
      collections: 10,
      shares: 5,
      description: "乡墅爆款案例",
      homepageFeatured: true,
      pushedAt: "2026-11-16T12:00:00Z",
      availability: "available",
      ...overrides,
    };
  }

  it("工作台两列：左侧爆款精选复用二级页卡片，右侧我的成片", () => {
    const videos = [
      homepageVideo(1, { homepageRank: 20, likes: 999999 }),
      homepageVideo(2, { homepageRank: 1, likes: 1 }),
      homepageVideo(3, { homepageRank: 2, pushedAt: "2026-11-14T12:00:00Z" }),
      homepageVideo(4, { homepageRank: 2, pushedAt: "2026-11-15T12:00:00Z" }),
      homepageVideo(5, { homepageRank: 30 }),
    ];
    const value = studio(undefined, {
      review: true,
      state: createState("workbench"),
      data: { ...data([]), homepageVideos: videos },
    });
    useStudio.mockReturnValue(value);
    const { container } = render(<WorkbenchPage />);
    const split = container.querySelector(".studio-home-split");
    expect(split).not.toBeNull();
    const panel = split?.querySelector<HTMLElement>(
      ".studio-home-recommendations",
    );
    if (!panel) throw new Error("未找到首页精选区域");

    expect(
      within(panel).getByRole("heading", { name: "爆款视频精选" }),
    ).toBeInTheDocument();
    // 左爆款在 DOM 中位于右成片之前（两列布局）。
    expect(panel.previousElementSibling).toBeNull();
    expect(panel.nextElementSibling).toHaveClass("studio-home-works");
    // 卡片复用二级页 ViralCard 结构；工作台按 3 列铺排，最多两整行共 6 张。
    expect(panel.querySelectorAll(".viral-card")).toHaveLength(5);
    expect(
      [...panel.querySelectorAll(".viral-card-title")].map(
        (node) => node.textContent,
      ),
    ).toEqual([
      "精选视频 2",
      "精选视频 4",
      "精选视频 3",
      "精选视频 1",
      "精选视频 5",
    ]);
    // 点标题进详情，返回目标为工作台。
    fireEvent.click(within(panel).getByRole("button", { name: "精选视频 2" }));
    expect(value.navigate).toHaveBeenCalledWith("viral-detail", {
      selectedVideoId: videos[1].id,
      returnTo: "workbench",
    });
    fireEvent.click(
      within(panel).getByRole("button", { name: "查看全部爆款" }),
    );
    expect(value.navigate).toHaveBeenCalledWith("viral");
  });

  it("跨平台新上传先展示，同时保留每个平台内部的人工顺序", async () => {
    const videos = [
      homepageVideo(1, {
        title: "跨平台推荐 人工置首",
        homepageRank: -30,
        pushedAt: "2026-11-14T12:00:00Z",
      }),
      homepageVideo(2, {
        title: "跨平台推荐 同平台次位",
        homepageRank: -29,
        pushedAt: "2026-11-16T12:00:00Z",
      }),
      homepageVideo(3, { title: "跨平台推荐 同平台第三", homepageRank: -28 }),
      homepageVideo(4, { title: "跨平台推荐 第五张", homepageRank: -27 }),
      homepageVideo(5, {
        title: "跨平台推荐 小红书新上传",
        platformKey: "xiaohongshu",
        platform: "小红书",
        homepageRank: -1,
        pushedAt: "2026-11-17T11:59:59Z",
      }),
    ];
    loadViralVideos.mockResolvedValue({ videos, errors: [] });
    useStudio.mockReturnValue(
      studio(undefined, { state: createState("workbench"), data: data([]) }),
    );
    render(<WorkbenchPage />);

    await screen.findByText("跨平台推荐 小红书新上传");
    expect(
      screen.getAllByText(/^跨平台推荐 /).map((node) => node.textContent),
    ).toEqual([
      "跨平台推荐 小红书新上传",
      "跨平台推荐 人工置首",
      "跨平台推荐 同平台次位",
      "跨平台推荐 同平台第三",
      "跨平台推荐 第五张",
    ]);
  });

  it.each([true, false])(
    "首页只展示推荐集合中已发布可用且排期生效的精选（审核：%s）",
    async (review) => {
      const current = homepageVideo(1);
      const excluded: StudioVideo[] = [
        homepageVideo(2, { homepageFeatured: false }),
        homepageVideo(3, { pushedAt: null }),
        homepageVideo(4, { pushedAt: undefined }),
        homepageVideo(5, { availability: "hidden" }),
        homepageVideo(6, { availability: "unavailable" }),
        homepageVideo(7, { homepageStartsAt: "2026-11-18T12:00:00Z" }),
        homepageVideo(8, { homepageEndsAt: "2026-11-17T12:00:00Z" }),
      ];
      loadViralVideos.mockResolvedValue({
        videos: [current, ...excluded],
        errors: [],
      });
      useStudio.mockReturnValue(
        studio(undefined, {
          review,
          state: createState("workbench"),
          data: {
            ...data(
              [],
              [homepageVideo(9, { title: "普通爆款网格的精选缓存" })],
            ),
            homepageVideos: [current, ...excluded],
          },
        }),
      );
      render(<WorkbenchPage />);

      expect(await screen.findByText(current.title)).toBeInTheDocument();
      expect(loadViralVideos).toHaveBeenCalledTimes(review ? 0 : 1);
      for (const video of excluded)
        expect(screen.queryByText(video.title)).toBeNull();
      expect(screen.queryByText("普通爆款网格的精选缓存")).toBeNull();
    },
  );

  it("没有独立首页推荐时保留空态，不从公共网格补入视频", () => {
    useStudio.mockReturnValue(
      studio(undefined, {
        review: true,
        state: createState("workbench"),
        data: {
          ...data([], [homepageVideo(1)]),
          homepageVideos: [],
        },
      }),
    );
    render(<WorkbenchPage />);

    expect(screen.getByText("暂无推荐视频")).toBeInTheDocument();
    expect(screen.queryByText("精选视频 1")).toBeNull();
    expect(screen.getByRole("button", { name: "查看全部爆款" })).toBeEnabled();
  });

  it("重新进入真实首页先重验，待确认和失败时都不复活启动缓存中的旧推荐", async () => {
    const oldVideo = homepageVideo(1, { title: "已撤回的旧推荐" });
    const pending = deferred<Awaited<ReturnType<typeof loadViralVideos>>>();
    loadViralVideos
      .mockResolvedValueOnce({ videos: [oldVideo], errors: [] })
      .mockReturnValueOnce(pending.promise)
      .mockRejectedValue(new Error("推荐读取离线"));
    useStudio.mockReturnValue(
      studio(undefined, {
        state: createState("workbench"),
        data: { ...data([]), homepageVideos: [oldVideo] },
      }),
    );
    const first = render(<WorkbenchPage />);
    await screen.findByText(oldVideo.title);
    first.unmount();
    render(<WorkbenchPage />);

    expect(screen.queryByText(oldVideo.title)).toBeNull();
    expect(screen.getByText("正在读取推荐视频")).toBeInTheDocument();
    await act(async () => {
      pending.resolve({ videos: [], errors: [] });
    });
    expect(screen.getByText("暂无推荐视频")).toBeInTheDocument();
    fireEvent.focus(window);
    await waitFor(() => expect(loadViralVideos).toHaveBeenCalledTimes(3));
    await act(async () => Promise.resolve());
    expect(screen.queryByText(oldVideo.title)).toBeNull();
    expect(screen.getByText("暂无推荐视频")).toBeInTheDocument();
  });

  it("空推荐按各平台服务端时钟的最早边界上线，到期刷新失败也立即移出", async () => {
    vi.useFakeTimers({
      now: clock,
      toFake: ["Date", "setTimeout", "clearTimeout"],
    });
    const video = homepageVideo(1, {
      title: "按服务端排期上线",
      homepageStartsAt: "2026-11-17T11:00:02Z",
      homepageEndsAt: "2026-11-17T11:00:04Z",
    });
    loadViralVideos
      .mockResolvedValueOnce({
        videos: [],
        errors: [],
        timing: [
          {
            platformKey: "douyin",
            serverTime: "2026-11-17T11:00:00Z",
            nextChangeAt: "2026-11-17T11:00:02Z",
            receivedAt: clock.getTime(),
          },
          {
            platformKey: "wechat_channels",
            serverTime: "2026-11-17T12:00:00Z",
            nextChangeAt: "2026-11-17T12:00:10Z",
            receivedAt: clock.getTime(),
          },
        ],
      })
      .mockResolvedValueOnce({
        videos: [video],
        errors: [],
        timing: [
          {
            platformKey: "douyin",
            serverTime: "2026-11-17T11:00:02Z",
            nextChangeAt: "2026-11-17T11:00:04Z",
            receivedAt: clock.getTime() + 2_000,
          },
        ],
      })
      .mockRejectedValue(new Error("到期刷新离线"));
    useStudio.mockReturnValue(
      studio(undefined, { state: createState("workbench"), data: data([]) }),
    );
    render(<WorkbenchPage />);
    await act(async () => Promise.resolve());
    expect(screen.getByText("暂无推荐视频")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_999);
    });
    expect(loadViralVideos).toHaveBeenCalledOnce();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2);
    });
    expect(loadViralVideos).toHaveBeenCalledTimes(2);
    expect(screen.getByText(video.title)).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(loadViralVideos).toHaveBeenCalledTimes(3);
    expect(screen.queryByText(video.title)).toBeNull();
    expect(screen.getByText("暂无推荐视频")).toBeInTheDocument();
  });

  it("等待慢平台期间已经跨过的排期边界在聚合结果到达后立即重验", async () => {
    vi.useFakeTimers({
      now: clock,
      toFake: ["Date", "setTimeout", "clearTimeout"],
    });
    const video = homepageVideo(1, { title: "慢响应后已上线" });
    loadViralVideos
      .mockResolvedValueOnce({
        videos: [],
        errors: [],
        timing: [
          {
            platformKey: "douyin",
            serverTime: "2026-11-17T11:00:00Z",
            nextChangeAt: "2026-11-17T11:00:02Z",
            receivedAt: clock.getTime() - 5_000,
          },
        ],
      })
      .mockResolvedValueOnce({ videos: [video], errors: [] });
    useStudio.mockReturnValue(
      studio(undefined, { state: createState("workbench"), data: data([]) }),
    );
    render(<WorkbenchPage />);
    await act(async () => Promise.resolve());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(loadViralVideos).toHaveBeenCalledTimes(2);
    expect(screen.getByText(video.title)).toBeInTheDocument();
  });

  it("焦点和可见性触发重验时丢弃乱序结果，换账号立即隔离上一账号推荐", async () => {
    const first = deferred<Awaited<ReturnType<typeof loadViralVideos>>>();
    const focus = deferred<Awaited<ReturnType<typeof loadViralVideos>>>();
    const visible = deferred<Awaited<ReturnType<typeof loadViralVideos>>>();
    const accountB = deferred<Awaited<ReturnType<typeof loadViralVideos>>>();
    loadViralVideos
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(focus.promise)
      .mockReturnValueOnce(visible.promise)
      .mockReturnValueOnce(accountB.promise);
    const stale = homepageVideo(1, { title: "账号A旧响应" });
    const latestA = homepageVideo(2, { title: "账号A最新推荐" });
    const latestB = homepageVideo(3, { title: "账号B推荐" });
    let current = studio(undefined, {
      state: createState("workbench"),
      data: data([]),
      user: { id: "account-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    const visibility = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    const view = render(<WorkbenchPage />);
    try {
      fireEvent.focus(window);
      await act(async () => {
        first.resolve({ videos: [stale], errors: [] });
      });
      expect(screen.queryByText(stale.title)).toBeNull();
      fireEvent(document, new Event("visibilitychange"));
      await act(async () => {
        visible.resolve({ videos: [latestA], errors: [] });
      });
      expect(screen.getByText(latestA.title)).toBeInTheDocument();
      current = {
        ...current,
        user: { id: "account-b" } as StudioContextValue["user"],
      };
      view.rerender(<WorkbenchPage />);
      expect(screen.queryByText(latestA.title)).toBeNull();
      await act(async () => {
        focus.resolve({ videos: [stale], errors: [] });
      });
      expect(screen.queryByText(stale.title)).toBeNull();
      await act(async () => {
        accountB.resolve({ videos: [latestB], errors: [] });
      });
      expect(screen.getByText(latestB.title)).toBeInTheDocument();
      expect(loadViralVideos).toHaveBeenCalledTimes(4);
    } finally {
      visibility.mockRestore();
    }
  });

  it.each([true, false])(
    "首页已收藏内容首击取消收藏，失败回滚且刷新其他卡不覆盖操作（成功：%s）",
    async (succeeds) => {
      const video = homepageVideo(1, { isFavorite: true });
      const other = homepageVideo(2);
      loadViralVideos
        .mockResolvedValueOnce({ videos: [video, other], errors: [] })
        .mockResolvedValueOnce({
          videos: [video, { ...other, title: "其他推荐已更新" }],
          errors: [],
        });
      if (!succeeds)
        removeViralFavorite.mockRejectedValueOnce(new Error("保存失败"));
      const value = studio(undefined, {
        state: createState("workbench"),
        data: data([]),
      });
      useStudio.mockReturnValue(value);
      render(<WorkbenchPage />);
      const favorite = await screen.findByRole("button", {
        name: `收藏 ${video.title}`,
      });
      expect(favorite).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(favorite);
      await waitFor(() =>
        expect(removeViralFavorite).toHaveBeenCalledExactlyOnceWith(
          "douyin",
          video.nativeId,
        ),
      );
      await waitFor(() => expect(favorite).toBeEnabled());
      expect(favorite).toHaveAttribute(
        "aria-pressed",
        succeeds ? "false" : "true",
      );
      expect(saveViralFavorite).not.toHaveBeenCalled();
      if (!succeeds)
        expect(value.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
      fireEvent.focus(window);
      await screen.findByText("其他推荐已更新");
      expect(favorite).toHaveAttribute(
        "aria-pressed",
        succeeds ? "false" : "true",
      );
    },
  );
});

describe("工作台我的成片", () => {
  it("按时间展示已完成和可交付的部分成片，封面缺失时也可查看", () => {
    const older: StudioTask = {
      ...taskA,
      status: "partial",
      backendKind: "generation_batch",
      deliverableCount: 1,
    };
    const recent: StudioTask = {
      ...taskB,
      submitted: "2026-09-07T12:00:00Z",
      poster: "/b.jpg",
    };
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([older, runningTask, recent]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);
    const section = screen.getByRole("region", { name: "我的成片" });
    expect(
      within(section)
        .getAllByRole("button", { name: /^查看成片：/ })
        .map((button) => button.getAttribute("aria-label")),
    ).toEqual([`查看成片：${recent.title}`, `查看成片：${older.title}`]);
    fireEvent.click(
      within(section).getByRole("button", { name: `查看成片：${older.title}` }),
    );
    expect(value.navigate).toHaveBeenCalledWith(
      "task-detail",
      expect.objectContaining({
        selectedTaskId: older.id,
        returnTo: "workbench",
      }),
    );
    fireEvent.click(
      within(section).getByRole("button", { name: /生成中 1 · 排队 0/ }),
    );
    expect(value.navigate).toHaveBeenCalledWith("tasks");
  });

  it("全部成片复用分页任务列表但不显示未出片任务", () => {
    const value = studio(undefined, { data: data([taskA, runningTask]) });
    useStudio.mockReturnValue(value);
    render(<TasksPage worksOnly />);
    expect(screen.getByText(taskA.title)).toBeInTheDocument();
    expect(screen.queryByText(runningTask.title)).not.toBeInTheDocument();
  });

  it("从成片卡进入发布页并选中已保存的成片", async () => {
    const task = { ...taskA, resultId: "result-1" };
    loadTaskPreview.mockResolvedValueOnce({
      id: "result-1",
      kind: "video",
      saved: true,
    });
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([task]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);
    fireEvent.click(
      screen.getByRole("button", { name: `发布成片：${task.title}` }),
    );
    await waitFor(() =>
      expect(value.navigate).toHaveBeenCalledWith("publishing", {
        selectedAssetId: "result-1",
      }),
    );
    expect(loadTaskPreview).toHaveBeenCalledWith(task);
  });

  it("访客欢迎页同样可见爆款精选、成片空态与常用工具（点击引导登录）", () => {
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([taskA]),
      requireLogin: vi.fn(),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);
    expect(
      screen.getByRole("heading", { name: "爆款视频精选" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "我的成片" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "常用工具" }),
    ).toBeInTheDocument();
    // 未登录没有成片数据：成片区落在空态引导卡，点「开始创作」即被引导登录。
    // 本用例带了一支成片：访客直接看到成片卡，点「查看成片」即被导航守卫
    // （欢迎页 mock：navigate=requireLogin）引导去登录。
    fireEvent.click(
      screen.getByRole("button", { name: `查看成片：${taskA.title}` }),
    );
    expect(value.navigate).toHaveBeenCalled();
  });
});

describe("formatTaskTime", () => {
  it("PostgreSQL 微秒及短时区偏移与标准 ISO 时间一致", () => {
    const reference = new Date("2026-09-14T06:00:00Z");
    expect(formatTaskTime("2026-09-14 05:17:11.591133+00", reference)).toBe(
      formatTaskTime("2026-09-14T05:17:11.591Z", reference),
    );
  });
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
    initiateUploadedReplicaPipeline.mockClear();
    findActiveReplicaPipeline.mockClear();
    // 默认时长在复刻上限以内：上传预检不触发本地抽轨，走整片上传。
    readVideoDuration.mockReset();
    readVideoDuration.mockResolvedValue(10);
    extractLocalVideoAudio.mockReset();
    resolveViralLink.mockReset();
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
  });

  function workbench(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    const value = studio(undefined, {
      state: { ...createState("workbench") },
      data: data([]),
      ...overrides,
    });
    if (!overrides.patchDraft) {
      vi.mocked(value.patchDraft).mockImplementation((patch) => {
        value.state = {
          ...value.state,
          draft: patchStudioDraft(value.state.draft, patch),
        };
      });
    }
    return value;
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

  it.each([30.001, 60, 200])(
    "%s 秒上传来源直接进入数字人口播，不重复上传或创建项目",
    async (duration) => {
      const value = workbench({
        user: {
          id: `account-upload-${duration}`,
        } as StudioContextValue["user"],
      });
      useStudio.mockReturnValue(value);
      readVideoDuration.mockResolvedValue(duration);
      extractLocalVideoAudio.mockResolvedValue(
        new File(["audio"], "来源.m4a", { type: "audio/mp4" }),
      );
      uploadWorkbenchSourceVideo.mockResolvedValue({
        projectId: "project-audio",
        assetId: "asset-audio",
      });
      const view = render(<WorkbenchPage />);
      changeFile("来源.mp4");
      expect(
        await screen.findByRole("button", { name: "一键复刻 · 数字人口播" }),
      ).toBeEnabled();
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-audio",
          sourceAssetId: "asset-audio",
          sourceDurationSeconds: duration,
        }),
        { persistNow: true },
      );
      expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce();
      expect(initiateUploadedReplicaPipeline).not.toHaveBeenCalled();
      expect(value.navigate).not.toHaveBeenCalled();
      view.unmount();
    },
  );

  it("超过 15 秒的视频先本地抽音轨，再以音频文件提取文案", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    readVideoDuration.mockResolvedValue(32);
    extractLocalVideoAudio.mockImplementation(
      async () => new File(["audio"], "乡墅案例.m4a", { type: "audio/mp4" }),
    );
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "project-audio",
      assetId: "asset-audio",
    });
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    await waitFor(() => expect(extractLocalVideoAudio).toHaveBeenCalledOnce());
    await waitFor(() =>
      expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce(),
    );
    // 只传抽出的音轨，原视频不出本机；用途固定为提取文案素材。
    expect(uploadWorkbenchSourceVideo.mock.calls[0]?.[0]?.name).toBe(
      "乡墅案例.m4a",
    );
    expect(uploadWorkbenchSourceVideo.mock.calls[0]?.[3]).toBe("script");
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({ projectId: "project-audio" }),
        { persistNow: true },
      ),
    );
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
    // 上传前有时长预检（微任务），上传调用不再同步发起。
    await waitFor(() =>
      expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce(),
    );
    expect(screen.getByText("正在上传 乡墅案例.mp4… 40%")).toBeInTheDocument();
    act(() => reportProgress?.(100));
    expect(screen.getByText("正在上传 乡墅案例.mp4… 100%")).toBeInTheDocument();
    expect(screen.queryByText("已上传：乡墅案例.mp4")).toBeNull();

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
      expect(value.patchDraft).toHaveBeenCalledWith(
        {
          projectId: "proj-1",
          sourceId: "asset-1",
          sourceAssetId: "asset-1",
          sourceDurationSeconds: 10,
        },
        { persistNow: true },
      ),
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
    expect(screen.getByText("已上传：乡墅案例.mp4")).toBeInTheDocument();
    expect(value.notify).toHaveBeenCalledWith(
      "视频已上传，来源已加入当前创作。",
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
    const second = deferred<WorkbenchUploadResult>();
    uploadWorkbenchSourceVideo.mockReturnValue(second.promise);
    render(<WorkbenchPage />);

    changeFile("来源A.mp4");
    changeFile("来源B.mp4");
    // 被取代的第一次选择在时长预检完成后即作废，不再发起上传；
    // 活跃的第二次上传携带自己的（未中止）信号。
    await waitFor(() =>
      expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce(),
    );
    expect(uploadWorkbenchSourceVideo.mock.calls[0]?.[2]?.aborted).toBe(false);

    second.resolve({ projectId: "project-b", assetId: "asset-b" });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        {
          projectId: "project-b",
          sourceId: "asset-b",
          sourceAssetId: "asset-b",
          sourceDurationSeconds: 10,
        },
        { persistNow: true },
      ),
    );
    await Promise.resolve();
    expect(value.patchDraft).toHaveBeenCalledTimes(1);
    expect(screen.getByText("已上传：来源B.mp4")).toBeInTheDocument();
    expect(screen.queryByText(/迟到失败/)).toBeNull();
  });

  it.each([4, 16.1, 30])(
    "%s 秒上传完成后复用项目和素材启动自动复刻，保留费用确认",
    async (duration) => {
      const value = workbench({
        user: { id: `account-short-${duration}` } as StudioContextValue["user"],
      });
      useStudio.mockReturnValue(value);
      saveReplicaPreference(value.user.id, "guide-v1", "1");
      saveReplicaPreference(value.user.id, "short-character", "cv-upload");
      readVideoDuration.mockResolvedValue(duration);
      uploadWorkbenchSourceVideo.mockResolvedValue({
        projectId: "proj-1",
        assetId: "asset-1",
        durationSeconds: duration,
      });
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
      const view = render(<WorkbenchPage />);
      changeFile("来源.mp4");
      const entry = await screen.findByRole("button", { name: "全自动复刻" });
      await waitFor(() => expect(entry).toBeEnabled());
      expect(initiateUploadedReplicaPipeline).not.toHaveBeenCalled();
      fireEvent.click(entry);
      await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
      expect(initiateUploadedReplicaPipeline).not.toHaveBeenCalled();
      confirm.mockReturnValue(true);
      fireEvent.click(entry);
      await waitFor(() =>
        expect(initiateUploadedReplicaPipeline).toHaveBeenCalledOnce(),
      );
      expect(initiateUploadedReplicaPipeline).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "proj-1",
          sourceAssetId: "asset-1",
          characterVersionId: "cv-upload",
        }),
      );
      expect(findActiveReplicaPipeline).toHaveBeenCalledWith(
        "upload",
        "asset-1",
      );
      expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce();
      expect(value.openLive).not.toHaveBeenCalled();
      expect(value.navigate).not.toHaveBeenCalled();
      view.unmount();
      confirm.mockRestore();
    },
  );

  it("重新进入工作台后按草稿保存的来源时长继续口播，不依赖原文件引用", () => {
    const state = createState("workbench");
    state.draft = {
      ...state.draft,
      projectId: "restored-project",
      sourceId: "restored-asset",
      sourceAssetId: "restored-asset",
      sourceDurationSeconds: 60,
    };
    const value = workbench({ state });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);
    expect(
      screen.getByRole("button", { name: "一键复刻 · 数字人口播" }),
    ).toBeEnabled();
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
    expect(initiateUploadedReplicaPipeline).not.toHaveBeenCalled();
  });

  it("浏览器读不出时长时使用服务端上传回执的实测时长分流", async () => {
    useStudio.mockReturnValue(workbench());
    readVideoDuration.mockRejectedValue(new Error("无法解码"));
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "p-measured",
      assetId: "a-measured",
      durationSeconds: 42,
    });
    render(<WorkbenchPage />);
    changeFile("来源.mov");
    expect(
      await screen.findByRole("button", { name: "一键复刻 · 数字人口播" }),
    ).toBeEnabled();
    expect(initiateUploadedReplicaPipeline).not.toHaveBeenCalled();
  });

  it("原片以服务端实测时长分流，抽音轨时保留原视频时长", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    readVideoDuration.mockResolvedValue(30);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "server-project",
      assetId: "server-asset",
      durationSeconds: 30.001,
    });
    const view = render(<WorkbenchPage />);
    changeFile("来源.mp4");
    expect(
      await screen.findByRole("button", { name: "一键复刻 · 数字人口播" }),
    ).toBeEnabled();
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({ sourceDurationSeconds: 30.001 }),
      { persistNow: true },
    );
    view.unmount();

    const audioValue = workbench();
    useStudio.mockReturnValue(audioValue);
    readVideoDuration.mockResolvedValue(30.001);
    extractLocalVideoAudio.mockResolvedValue(
      new File(["audio"], "来源.m4a", { type: "audio/mp4" }),
    );
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "audio-project",
      assetId: "audio-asset",
      durationSeconds: 30,
    });
    render(<WorkbenchPage />);
    changeFile("来源.mp4");
    expect(
      await screen.findByRole("button", { name: "一键复刻 · 数字人口播" }),
    ).toBeEnabled();
    expect(audioValue.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({ sourceDurationSeconds: 30.001 }),
      { persistNow: true },
    );
  });

  it("同页换来源和放弃草稿后，不再使用上次上传的项目", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "project-a",
      assetId: "asset-a",
      durationSeconds: 30,
    });
    const view = render(<WorkbenchPage />);
    changeFile("来源A.mp4");
    expect(
      await screen.findByRole("button", { name: "全自动复刻" }),
    ).toBeInTheDocument();
    value.state = {
      ...value.state,
      draft: {
        ...createState("workbench").draft,
        projectId: "project-b",
        sourceId: "asset-b",
        sourceAssetId: "asset-b",
        sourceDurationSeconds: 60,
      },
    };
    view.rerender(<WorkbenchPage />);
    expect(
      screen.getByRole("button", { name: "一键复刻 · 数字人口播" }),
    ).toBeEnabled();
    expect(screen.queryByRole("button", { name: "全自动复刻" })).toBeNull();
    expect(screen.queryByText("已上传：来源A.mp4")).toBeNull();
    value.state = createState("workbench");
    view.rerender(<WorkbenchPage />);
    expect(
      screen.queryByRole("button", { name: "一键复刻 · 数字人口播" }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "开始复刻" })).toBeEnabled();
    expect(initiateUploadedReplicaPipeline).not.toHaveBeenCalled();
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
      expect(value.patchDraft).toHaveBeenCalledWith(
        {
          projectId: "proj-1",
          sourceId: "asset-1",
          sourceAssetId: "asset-1",
          sourceDurationSeconds: 10,
        },
        { persistNow: true },
      ),
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

  it.each([false, true])(
    "诊断直达意图在 StrictMode=%s 时完成一次解析和导入",
    async (strict) => {
      const link = "https://www.douyin.com/video/7345678901234567890";
      const state = createState("workbench");
      state.replicateIntentLink = link;
      const value = workbench({ state });
      useStudio.mockReturnValue(value);
      const resolution = deferred<import("../api").ViralLinkResolution>();
      resolveViralLink.mockReturnValue(resolution.promise);
      createViralImportTask.mockResolvedValue({
        id: "intent-import",
        status: "SUCCEEDED",
        projectId: "intent-project",
        sourceAssetId: "intent-asset",
        canAnalyze: true,
      });
      const view = render(
        strict ? (
          <StrictMode>
            <WorkbenchPage />
          </StrictMode>
        ) : (
          <WorkbenchPage />
        ),
      );
      await waitFor(() => expect(resolveViralLink).toHaveBeenCalledOnce());
      expect(resolveViralLink).toHaveBeenCalledWith(
        link,
        "replica",
        expect.any(String),
      );
      expect(screen.getByLabelText("视频链接")).toHaveValue(link);
      resolution.resolve({
        importIdempotencyKey: "intent-import-key",
        item: {
          platform: "douyin",
          videoId: "7345678901234567890",
          category: "链接导入",
          title: "诊断推荐作品",
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
          playUrl: "https://cdn.example/intent.mp4",
        },
      });
      await waitFor(() =>
        expect(value.navigate).toHaveBeenCalledWith("replica", {
          selectedVideoId: "douyin-7345678901234567890",
          returnTo: "workbench",
        }),
      );
      expect(createViralImportTask).toHaveBeenCalledOnce();
      expect(createViralImportTask).toHaveBeenCalledWith(
        "douyin",
        "7345678901234567890",
        "replica",
        "intent-import-key",
      );
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "intent-project",
        sourceId: "intent-asset",
        sourceAssetId: "intent-asset",
      });
      expect(value.patchState).toHaveBeenCalledOnce();
      expect(value.patchState).toHaveBeenCalledWith({
        replicateIntentLink: undefined,
      });
      view.rerender(
        strict ? (
          <StrictMode>
            <WorkbenchPage />
          </StrictMode>
        ) : (
          <WorkbenchPage />
        ),
      );
      await act(async () => Promise.resolve());
      expect(resolveViralLink).toHaveBeenCalledOnce();
      expect(createViralImportTask).toHaveBeenCalledOnce();
      expect(value.navigate).toHaveBeenCalledOnce();
    },
  );

  it.each(["换账号", "往返换账号", "卸载"])(
    "诊断直达尚未启动时%s不消费旧意图或发出请求",
    async (transition) => {
      const state = createState("workbench");
      state.replicateIntentLink = "https://v.douyin.com/queued-account-a/";
      const accountA = workbench({
        state,
        user: { id: "queued-account-a" } as StudioContextValue["user"],
      });
      const accountB = workbench({
        // 模拟换号首帧仍有相同链接，effect 不能只依赖链接变化做清理。
        state,
        user: { id: "queued-account-b" } as StudioContextValue["user"],
      });
      let current = accountA;
      useStudio.mockImplementation(() => current);
      resolveViralLink.mockReturnValue(
        deferred<import("../api").ViralLinkResolution>().promise,
      );
      const view = render(<WorkbenchPage />);
      if (transition === "卸载") {
        view.unmount();
      } else {
        current = accountB;
        view.rerender(<WorkbenchPage />);
        if (transition === "往返换账号") {
          current = accountA;
          view.rerender(<WorkbenchPage />);
        }
      }
      await act(async () => Promise.resolve());

      expect(resolveViralLink).not.toHaveBeenCalled();
      expect(createViralImportTask).not.toHaveBeenCalled();
      for (const account of [accountA, accountB]) {
        expect(account.patchState).not.toHaveBeenCalled();
        expect(account.patchDraft).not.toHaveBeenCalled();
        expect(account.navigate).not.toHaveBeenCalled();
      }
      if (transition !== "卸载") {
        expect(screen.getByLabelText("视频链接")).toHaveValue("");
      }
    },
  );

  it("StrictMode 自动直达仍经过登录入口且不发出真实解析请求", async () => {
    const state = createState("workbench");
    state.replicateIntentLink = "https://v.douyin.com/login-required/";
    const requireLogin = vi.fn();
    const value = workbench({ state, requireLogin });
    useStudio.mockReturnValue(value);
    render(
      <StrictMode>
        <WorkbenchPage />
      </StrictMode>,
    );
    await waitFor(() => expect(requireLogin).toHaveBeenCalledOnce());
    expect(resolveViralLink).not.toHaveBeenCalled();
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
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
    expect(value.refresh).toHaveBeenCalledTimes(1);
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

  it("网络解析明确失败后由用户重试使用新请求，避免永久重放失败回执", async () => {
    useStudio.mockReturnValue(workbench());
    resolveViralLink.mockRejectedValue(
      Object.assign(new Error("请检查代理或 DNS 设置后重试"), {
        status: 503,
        code: "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE",
      }),
    );
    render(<WorkbenchPage />);
    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: {
        value: "https://www.douyin.com/jingxuan?modal_id=7672703482771972081",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await screen.findByRole("alert");
    expect(resolveViralLink).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledTimes(2));
    expect(resolveViralLink.mock.calls[1]?.[2]).not.toBe(
      resolveViralLink.mock.calls[0]?.[2],
    );
    expect(createViralImportTask).not.toHaveBeenCalled();
  });

  it("链接视频超过 15 秒上限时红标报错而非静默无反应", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    resolveViralLink.mockRejectedValue(
      Object.assign(
        new Error(
          "参考视频时长 81 秒，超过 15 秒上限，无法拆解；请上传 15 秒以内的视频。",
        ),
        { status: 422, code: "VIRAL_LINK_DURATION_EXCEEDED" },
      ),
    );
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: {
        value: "https://www.douyin.com/jingxuan?modal_id=7672703482771972081",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/超过 15 秒上限/);
    expect(alert).toHaveClass("is-error");
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("工作台展示简洁的抖音、小红书分享链接与 MP4 上传提示", () => {
    useStudio.mockReturnValue(workbench());
    render(<WorkbenchPage />);

    expect(
      screen.getByText("支持抖音、小红书的分享链接，其他平台请上传 MP4文件；"),
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
    removeStudioGenerationTask.mockReset();
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

  it("普通视频批次支持在任务中心重命名", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-name",
      backendId: "generation-name",
      backendKind: "generation_batch",
      title: "原视频名称",
      type: "视频生成",
    };
    const value = tasksPage({ data: data([generationTask]) });
    useStudio.mockReturnValue(value);
    renameStudioGenerationTask.mockResolvedValue({
      ...generationTask,
      title: "乡墅庭院成片",
    });
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    fireEvent.change(screen.getByLabelText("重命名 原视频名称"), {
      target: { value: "乡墅庭院成片" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(renameStudioGenerationTask).toHaveBeenCalledWith(
        generationTask,
        "乡墅庭院成片",
      ),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("视频名称已更新。");
  });

  it("普通视频批次支持从任务列表删除，且需二次确认", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-remove",
      backendId: "generation-remove",
      backendKind: "generation_batch",
      title: "待移除的成片",
      type: "视频生成",
    };
    const value = tasksPage({ data: data([generationTask]) });
    useStudio.mockReturnValue(value);
    removeStudioGenerationTask.mockResolvedValue(undefined);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    // 二次确认之前不得触达后端：这是账号级移除，但仍是用户不可自助撤销的操作。
    expect(removeStudioGenerationTask).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/确定从任务列表移除「待移除的成片」吗/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await waitFor(() =>
      expect(removeStudioGenerationTask).toHaveBeenCalledWith(generationTask),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("任务已从列表移除。");
  });

  it("确认弹窗说明不会取消任务也不退费，避免误解为撤单", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-copy",
      backendId: "generation-copy",
      backendKind: "generation_batch",
      title: "文案校验用成片",
      type: "视频生成",
    };
    useStudio.mockReturnValue(tasksPage({ data: data([generationTask]) }));
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByText(/仅从本账号列表移除/);
    expect(dialog.textContent).toMatch(/不会取消/);
    expect(dialog.textContent).toMatch(/不影响已产生的费用/);
  });

  it("口播任务与普通批次一样提供删除入口并走确认移除", async () => {
    const oralTask: StudioTask = {
      ...doneTask,
      id: "oral-1",
      backendId: "oral-1",
      backendKind: "oral_task",
      backendStatus: "SUCCEEDED",
      title: "张工口播成片",
      type: "数字人口播",
    };
    const value = tasksPage({ data: data([oralTask]) });
    useStudio.mockReturnValue(value);
    removeStudioGenerationTask.mockResolvedValue(undefined);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(
      await screen.findByText(/确定从任务列表移除「张工口播成片」吗/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await waitFor(() =>
      expect(removeStudioGenerationTask).toHaveBeenCalledWith(oralTask),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("任务已从列表移除。");
  });

  it("审核模式删除只本地移除，不调用后端", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-review",
      backendId: "generation-review",
      backendKind: "generation_batch",
      title: "审核模式成片",
      type: "视频生成",
    };
    const value = tasksPage({
      data: data([generationTask]),
      review: true,
    });
    useStudio.mockReturnValue(value);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认移除" }));

    await waitFor(() => expect(value.updateData).toHaveBeenCalled());
    expect(removeStudioGenerationTask).not.toHaveBeenCalled();
  });

  it("删除失败时保留任务并提示后端原因", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-fail",
      backendId: "generation-fail",
      backendKind: "generation_batch",
      title: "删除会失败的成片",
      type: "视频生成",
    };
    const value = tasksPage({ data: data([generationTask]) });
    useStudio.mockReturnValue(value);
    removeStudioGenerationTask.mockRejectedValue(
      new Error("任务不存在或无权限。"),
    );
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认移除" }));

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("任务不存在或无权限。"),
    );
    expect(value.updateData).not.toHaveBeenCalled();
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

  it("委派项目里他人提交的排队批次不出现取消入口（FIX-12）", () => {
    const others: StudioTask = {
      ...taskA,
      id: "batch-others",
      type: "视频生成",
      backendKind: "generation_batch",
      backendId: "batch-others",
      status: "queued",
      createdByUserId: "someone-else",
    };
    const mine: StudioTask = {
      ...others,
      id: "batch-mine",
      backendId: "batch-mine",
      title: "我的排队任务",
      createdByUserId: "account-a",
    };
    useStudio.mockReturnValue(
      studio(taskA.id, {
        state: createState("tasks"),
        data: data([others, mine]),
        user: { id: "account-a" } as StudioContextValue["user"],
      }),
    );
    render(<TasksPage />);
    expect(screen.getAllByRole("button", { name: "取消任务" })).toHaveLength(1);
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
    // 点击筛选器外的历史任务入口时，下拉菜单应收起。
    fireEvent.pointerDown(
      screen.getByRole("button", { name: "历史任务与下载" }),
    );
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
    expect(screen.getByText("0 积分")).toBeInTheDocument();

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

// CW-016：客户「使用记录」有两个入口——个人中心顶部「使用记录」标签，以及
// 账户概览里的「查看使用记录」按钮。两者都必须汇入同一个 live 钱包工作区
// （openLive("wallet")）；配合 LiveWorkspacePanel.test.tsx 的真实挂载锁，构成
// 「入口点击 → panel=wallet → CustomerWalletPanel」的完整链路回归保护。
describe("CW-016 两个客户钱包入口路由到 live 钱包工作区", () => {
  beforeEach(() => {
    useStudio.mockReset();
    getStudioNotificationPreferences.mockReset();
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
  });

  it("个人中心「使用记录」标签入口调用 openLive(wallet)", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ProfilePage />);

    fireEvent.click(screen.getByRole("tab", { name: "使用记录" }));

    expect(value.openLive).toHaveBeenCalledWith("wallet");
  });

  it("账户概览「查看使用记录」按钮入口调用 openLive(wallet)", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ProfilePage />);

    fireEvent.click(screen.getByRole("button", { name: "查看使用记录" }));

    expect(value.openLive).toHaveBeenCalledWith("wallet");
  });
});

const nativeAccounts = vi.hoisted(() => ({
  focusLocalPublishLogin: vi.fn(),
  canUseLocalPublishAccounts: vi.fn(() => true),
  listLocalPublishAccounts: vi.fn(),
  startLocalPublishLogin: vi.fn(),
  checkLocalPublishLogin: vi.fn(),
  cancelLocalPublishLogin: vi.fn(),
  removeLocalPublishAccount: vi.fn(),
  // PUBLISH-DELIVERY-20260917: server-side copies used by the publish worker.
  listCloudPublishAccounts: vi.fn(),
  importCloudPublishAccount: vi.fn(),
  exportLocalPublishAccountState: vi.fn(),
  deleteCloudPublishAccount: vi.fn(),
}));
vi.mock("./localPublishAccounts", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...nativeAccounts,
}));
describe("发布账号官方扫码", () => {
  const account = {
    id: "local-1",
    platform: "xiaohongshu",
    platform_user_id: "platform-uid",
    username: "平台真实昵称",
    verified_at: 1,
  };
  function open() {
    const value = studio(undefined, { review: false });
    useStudio.mockReturnValue(value);
    const view = render(<ProfilePage />);
    fireEvent.click(screen.getByRole("tab", { name: "发布账号" }));
    return { value, view };
  }
  beforeEach(() => {
    Object.values(nativeAccounts).forEach((mock) => {
      mock.mockReset();
    });
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    nativeAccounts.canUseLocalPublishAccounts.mockReturnValue(true);
    nativeIsTauri.mockImplementation(() =>
      nativeAccounts.canUseLocalPublishAccounts(),
    );
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Windows");
    // 新解绑编排使用模块内部函数，替身下沉到网络/原生命令边界，保留真实编排。
    nativeInvoke.mockImplementation(async (command, args) => {
      if (command === "remove_local_publish_account")
        return nativeAccounts.removeLocalPublishAccount(
          args.owner,
          args.accountId,
        );
      if (command === "list_local_publish_accounts")
        return nativeAccounts.listLocalPublishAccounts(args.owner);
      throw new Error(`未预期的原生命令：${command}`);
    });
    publishBrowserRequest.mockImplementation(async (path, init) => {
      if (init?.method === "DELETE") {
        await nativeAccounts.deleteCloudPublishAccount(
          decodeURIComponent(path.split("/").at(-1)),
        );
        return Response.json({ deleted: true });
      }
      if (path === "/api/studio/publish/browser/accounts")
        return Response.json(await nativeAccounts.listCloudPublishAccounts());
      throw new Error(`未预期的发布请求：${path}`);
    });
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([]);
    nativeAccounts.listCloudPublishAccounts.mockResolvedValue([]);
    nativeAccounts.importCloudPublishAccount.mockResolvedValue({
      ...account,
      id: "cloud-1",
      status: "connected",
      error_message: null,
      source: "desktop",
    });
    nativeAccounts.deleteCloudPublishAccount.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "loading",
      image: null,
      account: null,
    });
    nativeAccounts.cancelLocalPublishLogin.mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.useRealTimers();
    nativeIsTauri.mockReturnValue(false);
    nativeInvoke.mockReset();
    publishBrowserRequest.mockReset();
  });
  it("自动检测暂停后打开官方窗口会恢复当前会话检测", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("recovery-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockRejectedValue(
      new Error("暂时无法读取登录状态"),
    );
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    expect(
      screen.getByText("自动检测已暂停，请重试检测或重新获取二维码。"),
    ).toBeInTheDocument();

    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "connected",
      image: null,
      account: { ...account, platform: "douyin" },
    });
    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    await act(async () => {});
    expect(nativeAccounts.focusLocalPublishLogin).toHaveBeenCalledWith(
      value.user.id,
      "recovery-login",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.getByText("抖音 · 平台真实昵称")).toBeInTheDocument();
    expect(screen.queryByLabelText("抖音扫码登录")).toBeNull();
  });
  it("取消扫码后迟到的官方窗口响应不会恢复已结束会话", async () => {
    vi.useFakeTimers();
    const focusRequest = deferred<void>();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("cancelled-login");
    nativeAccounts.focusLocalPublishLogin.mockReturnValue(focusRequest.promise);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    fireEvent.click(screen.getByRole("button", { name: "取消扫码" }));
    await act(async () => {});
    expect(screen.queryByLabelText("抖音扫码登录")).toBeNull();

    focusRequest.resolve();
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(nativeAccounts.checkLocalPublishLogin).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("抖音扫码登录")).toBeNull();
  });
  it("旧会话的窗口响应不会重启新会话的轮询", async () => {
    vi.useFakeTimers();
    const oldFocusRequest = deferred<void>();
    nativeAccounts.startLocalPublishLogin
      .mockResolvedValueOnce("old-login")
      .mockResolvedValueOnce("new-login");
    nativeAccounts.focusLocalPublishLogin.mockReturnValue(
      oldFocusRequest.promise,
    );
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    fireEvent.click(screen.getByRole("button", { name: "取消扫码" }));
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(nativeAccounts.checkLocalPublishLogin).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("小红书扫码登录")).toBeInTheDocument();

    oldFocusRequest.resolve();
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(nativeAccounts.checkLocalPublishLogin).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("小红书扫码登录")).toBeInTheDocument();
  });
  it("扫码确认后把导出的登录状态加密同步到服务端，失败可重试", async () => {
    const storage = {
      cookies: [{ name: "sid", value: "s", domain: ".xiaohongshu.com" }],
      origins: [],
    };
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("login-1");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "connected",
      image: null,
      account,
      storage_state: storage,
    });
    nativeAccounts.importCloudPublishAccount.mockRejectedValueOnce(
      new Error("服务端暂不可用"),
    );
    const { value } = open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称", {}, { timeout: 2500 });
    await waitFor(() =>
      expect(nativeAccounts.importCloudPublishAccount).toHaveBeenCalledWith(
        "xiaohongshu",
        {
          platform_user_id: "platform-uid",
          username: "平台真实昵称",
          avatar_url: null,
        },
        storage,
      ),
    );
    await screen.findByText("服务端暂不可用");
    expect(screen.getByText(/服务端未同步/)).toBeInTheDocument();
    // Retry re-exports from the hidden official window and uploads again.
    nativeAccounts.exportLocalPublishAccountState.mockResolvedValue({
      identity: { platform_user_id: "platform-uid", username: "平台真实昵称" },
      storage_state: storage,
    });
    fireEvent.click(screen.getByRole("button", { name: "同步到服务端" }));
    await waitFor(() =>
      expect(
        nativeAccounts.exportLocalPublishAccountState,
      ).toHaveBeenCalledWith(value.user.id, "local-1"),
    );
    await screen.findByText(/已同步服务端，可自动发布/);
    expect(value.notify).toHaveBeenCalledWith(
      "登录状态已同步到服务端，可用于自动发布",
    );
    expect(screen.queryByText("服务端暂不可用")).toBeNull();
  });
  it("解绑先删除并复查服务端副本，再清理确认的本机账号", async () => {
    const cloud = {
      ...account,
      id: "cloud-9",
      status: "connected",
      error_message: null,
      source: "desktop",
    };
    let cloudPresent = true;
    let localPresent = true;
    let cloudAbsenceConfirmed = false;
    nativeAccounts.listLocalPublishAccounts.mockImplementation(async () =>
      localPresent ? [account] : [],
    );
    nativeAccounts.listCloudPublishAccounts.mockImplementation(async () => {
      if (!cloudPresent) cloudAbsenceConfirmed = true;
      return cloudPresent ? [cloud] : [];
    });
    nativeAccounts.deleteCloudPublishAccount.mockImplementation(async () => {
      expect(nativeAccounts.removeLocalPublishAccount).not.toHaveBeenCalled();
      cloudPresent = false;
    });
    nativeAccounts.removeLocalPublishAccount.mockImplementation(async () => {
      expect(cloudAbsenceConfirmed).toBe(true);
      localPresent = false;
    });
    const { value } = open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText(/已同步服务端，可自动发布/);
    fireEvent.click(screen.getByRole("button", { name: "解绑" }));
    expect(nativeAccounts.deleteCloudPublishAccount).not.toHaveBeenCalled();
    expect(nativeAccounts.removeLocalPublishAccount).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await waitFor(() =>
      expect(nativeAccounts.removeLocalPublishAccount).toHaveBeenCalledWith(
        value.user.id,
        account.id,
      ),
    );
    expect(
      nativeAccounts.deleteCloudPublishAccount,
    ).toHaveBeenCalledExactlyOnceWith("cloud-9");
    expect(cloudAbsenceConfirmed).toBe(true);
    expect(
      nativeAccounts.deleteCloudPublishAccount.mock.invocationCallOrder[0],
    ).toBeLessThan(
      nativeAccounts.removeLocalPublishAccount.mock.invocationCallOrder[0],
    );
    expect(value.notify).toHaveBeenCalledWith(
      "已确认清除该账号的本机与云端登录状态。在途发布仍需核对发布记录。",
    );
    await waitFor(() =>
      expect(screen.queryByText("小红书 · 平台真实昵称")).toBeNull(),
    );
  });
  it("只从本机加载账号并显示官方用户名，不提供 Cookie 输入框", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    const { value } = open();
    await waitFor(() =>
      expect(nativeAccounts.listLocalPublishAccounts).toHaveBeenCalled(),
    );
    expect(screen.queryByText("小红书 · 平台真实昵称")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称");
    expect(nativeAccounts.listLocalPublishAccounts).toHaveBeenCalledWith(
      value.user.id,
    );
    expect(
      screen.queryByPlaceholderText("粘贴从浏览器复制的整段 Cookie"),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "抖音" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "视频号" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "小红书" })).toBeInTheDocument();
  });
  it("扫码添加以平台响应为准，未完成前不显示已连接", async () => {
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("login-1");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "connected",
      image: null,
      account,
    });
    const { value } = open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByRole("button", { name: "取消扫码" });
    expect(value.notify).not.toHaveBeenCalledWith(
      expect.stringContaining("已连接"),
    );
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    await screen.findByText("小红书 · 平台真实昵称");
    expect(nativeAccounts.startLocalPublishLogin).toHaveBeenCalledWith(
      value.user.id,
      "xiaohongshu",
      undefined,
    );
    expect(value.notify).toHaveBeenCalledWith("已连接 小红书 · 平台真实昵称");
  });
  it("取消扫码关闭本次原生会话", async () => {
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("login-2");
    const { value } = open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "扫码添加账号" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消扫码" }));
    await waitFor(() =>
      expect(nativeAccounts.cancelLocalPublishLogin).toHaveBeenCalledWith(
        value.user.id,
        "login-2",
      ),
    );
  });
  it("解绑必须指向确认的本机账号，本机失败保留记录及同目标重试", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    nativeAccounts.removeLocalPublishAccount.mockRejectedValue(
      new Error("请先关闭官方窗口"),
    );
    const { value } = open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称");
    fireEvent.click(screen.getByRole("button", { name: "解绑" }));
    expect(nativeAccounts.removeLocalPublishAccount).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await waitFor(() =>
      expect(
        screen.getByText(
          "云端已解绑，本机清理未完成，请重试。请先关闭官方窗口",
        ),
      ).toBeInTheDocument(),
    );
    expect(
      nativeAccounts.removeLocalPublishAccount,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, account.id);
    expect(nativeAccounts.deleteCloudPublishAccount).not.toHaveBeenCalled();
    expect(value.notify).not.toHaveBeenCalled();
    expect(screen.getByText("小红书 · 平台真实昵称")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认解绑" })).toBeEnabled();

    nativeAccounts.removeLocalPublishAccount.mockResolvedValue(undefined);
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "已确认清除该账号的本机与云端登录状态。在途发布仍需核对发布记录。",
      ),
    );
    expect(nativeAccounts.removeLocalPublishAccount.mock.calls).toEqual([
      [value.user.id, account.id],
      [value.user.id, account.id],
    ]);
  });
  it("网页端读取云端账号并允许扫码添加", async () => {
    nativeAccounts.canUseLocalPublishAccounts.mockReturnValue(false);
    open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    expect(nativeAccounts.listLocalPublishAccounts).toHaveBeenCalled();
    expect(
      screen.getByText("账号的登录状态加密保存在服务器，可在个人中心解绑。"),
    ).toBeInTheDocument();
  });
  it("非原生模式需要额外验证时不显示官方窗口入口", async () => {
    nativeAccounts.canUseLocalPublishAccounts.mockReturnValue(false);
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("cloud-login");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "抖音" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await screen.findByText(
      "平台要求进一步验证，请按平台提示完成后重试。",
      {},
      { timeout: 2500 },
    );

    expect(screen.queryByRole("button", { name: "打开官方窗口" })).toBeNull();
    expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
  });
  it("个人中心显示二维码，过期后移除图片并可重新获取", async () => {
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("qr-1");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "qr_ready",
      image: "data:image/png;base64,cXI=",
      account: null,
    });
    const { value } = open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "扫码添加账号" }));
    await screen.findByAltText("抖音登录二维码", {}, { timeout: 2500 });
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "expired",
      image: null,
      account: null,
    });
    await screen.findByText(
      "二维码或本次连接已过期，请重新获取。",
      {},
      { timeout: 2500 },
    );
    expect(screen.queryByAltText("抖音登录二维码")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新获取二维码" }));
    await waitFor(() =>
      expect(nativeAccounts.startLocalPublishLogin).toHaveBeenCalledTimes(2),
    );
    expect(nativeAccounts.cancelLocalPublishLogin).toHaveBeenCalledWith(
      value.user.id,
      "qr-1",
    );
  });
  it.each([
    ["douyin", "抖音"],
    ["wechat_channels", "视频号"],
    ["xiaohongshu", "小红书"],
  ])(
    "点击 %s 直接内嵌二维码，确认后在同一标签显示账号",
    async (platform, name) => {
      const connected = { ...account, platform, username: `${name}测试账号` };
      nativeAccounts.startLocalPublishLogin.mockResolvedValue("inline-login");
      nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
        phase: "qr_ready",
        image: "data:image/png;base64,cXI=",
        account: null,
      });
      const { value } = open();
      await waitFor(() =>
        expect(screen.getByRole("button", { name })).toBeEnabled(),
      );
      fireEvent.click(screen.getByRole("button", { name }));
      await screen.findByAltText(`${name}登录二维码`, {}, { timeout: 2500 });
      expect(
        nativeAccounts.startLocalPublishLogin,
      ).toHaveBeenCalledExactlyOnceWith(value.user.id, platform, undefined);
      expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "打开官方窗口" })).toBeNull();
      // A list refresh may lag or fail: the verified account returned by login is authoritative.
      nativeAccounts.listLocalPublishAccounts.mockRejectedValue(
        new Error("列表暂不可用"),
      );
      nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
        phase: "connected",
        image: null,
        account: connected,
      });
      await screen.findByText(
        `${name} · ${name}测试账号`,
        {},
        { timeout: 2500 },
      );
      expect(screen.queryByAltText(`${name}登录二维码`)).toBeNull();
      const otherName = name === "抖音" ? "小红书" : "抖音";
      fireEvent.click(screen.getByRole("button", { name: otherName }));
      expect(screen.queryByText(`${name} · ${name}测试账号`)).toBeNull();
    },
  );
  it("已有账号的平台仅切换列表，添加更多账号仍可主动扫码", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("second-login");
    open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称");
    expect(nativeAccounts.startLocalPublishLogin).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "扫码添加账号" }));
    await screen.findByRole("button", { name: "取消扫码" });
    expect(nativeAccounts.startLocalPublishLogin).toHaveBeenCalledOnce();
  });
  it("需要进一步验证时自动打开官方窗口，无需手动点击", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("verify-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    // 源码取码失败（action_required）即自动降级为可见官方窗口，只触发一次。
    expect(
      nativeAccounts.focusLocalPublishLogin,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, "verify-login");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(nativeAccounts.focusLocalPublishLogin).toHaveBeenCalledOnce();
  });
  it("隐藏窗口持续取不到二维码时自动降级打开官方窗口", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("slow-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "loading",
      image: null,
      account: null,
    });
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(
      nativeAccounts.focusLocalPublishLogin,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, "slow-login");
  });
  it("二维码就绪时不自动打开官方窗口，验证阶段仍可手动唤起", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("ready-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "qr_ready",
      image: "data:image/png;base64,cXI=",
      account: null,
    });
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(35_000);
    });
    // 源码取码成功（qr_ready）时绝不自动弹官方窗口。
    expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "打开官方窗口" })).toBeNull();
    // 进入验证阶段后自动降级触发；手动按钮保留，便于窗口被最小化后重新唤起。
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(
      nativeAccounts.focusLocalPublishLogin,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, "ready-login");
    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    await act(async () => {});
    expect(nativeAccounts.focusLocalPublishLogin).toHaveBeenCalledTimes(2);
  });
});
