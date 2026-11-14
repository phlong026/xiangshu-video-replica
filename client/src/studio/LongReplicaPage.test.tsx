import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReplicaPromptTask, ReplicaPromptVersion } from "../api";
import { clearAccountResidue } from "./draftResidue";
import type { StudioContextValue } from "./types";

const {
  useStudio,
  readVideoDuration,
  uploadWorkbenchSourceVideo,
  startReplicaPromptTask,
  getLatestReplicaPromptTask,
  getLatestReplicaPromptResult,
  cancelReplicaPromptTask,
  waitForReplicaPromptTask,
} = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
  readVideoDuration: vi.fn<(file: File) => Promise<number>>(),
  uploadWorkbenchSourceVideo:
    vi.fn<
      (
        file: File,
        onProgress: (percent: number) => void,
        signal?: AbortSignal,
        purpose?: "replica" | "script" | "replica_prompt",
      ) => Promise<{ projectId: string; assetId: string }>
    >(),
  startReplicaPromptTask: vi.fn(),
  getLatestReplicaPromptTask: vi.fn(),
  getLatestReplicaPromptResult: vi.fn(),
  cancelReplicaPromptTask: vi.fn(),
  waitForReplicaPromptTask: vi.fn(),
}));

vi.mock("./context", () => ({ useStudio }));

vi.mock("./live", () => ({
  readVideoDuration,
  uploadWorkbenchSourceVideo,
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  startReplicaPromptTask,
  getLatestReplicaPromptTask,
  getLatestReplicaPromptResult,
  cancelReplicaPromptTask,
  waitForReplicaPromptTask,
}));

import { LongReplicaPage } from "./LongReplicaPage";
import {
  clearLongReplicaSession,
  getLongReplicaSession,
  handOffLongReplicaSource,
} from "./longReplicaSession";

beforeEach(() => {
  vi.resetAllMocks();
  clearLongReplicaSession("u-1");
  clearLongReplicaSession("u-2");
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => vi.useRealTimers());

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const RESULT_PAYLOAD = {
  schema_version: "replica-prompt.v1",
  summary: {
    theme: "庭院施工过程展示",
    visual_style: "写实实拍",
    pace: "明快剪辑与自然讲述交替",
    camera_language: "手持跟随与固定机位交替",
    color_tone: "自然光暖色调",
  },
  spoken_script: [
    { start_time: 0, end_time: 4.2, text: "大家好，今天带大家看小院开工。" },
    { start_time: 4.2, end_time: 9, text: "这一步是场地平整。" },
  ],
  replica_prompt:
    "一段庭院施工讲解视频：讲解者身穿工装在自家小院内面向镜头介绍施工进度……",
};

function resultPayload() {
  return {
    id: "ver-1",
    project_id: "proj-1",
    asset_id: "asset-1",
    kind: "replica_prompt",
    version_number: 1,
    payload: RESULT_PAYLOAD,
    created_by_user_id: "owner-1",
    created_at: "2026-10-16 10:00:00",
  };
}

function makeFile(overrides: { size?: number } = {}): File {
  const file = new File([new Uint8Array(16)], "长视频素材.mp4", {
    type: "video/mp4",
  });
  if (overrides.size !== undefined) {
    Object.defineProperty(file, "size", { value: overrides.size });
  }
  return file;
}

async function pickFile(file: File) {
  const input = document.querySelector('input[type="file"]');
  expect(input).not.toBeNull();
  await act(async () => {
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
  });
}

function setupContext() {
  const notify = vi.fn();
  const openLive = vi.fn();
  useStudio.mockReturnValue({
    notify,
    openLive,
    navigate: vi.fn(),
    state: {},
    data: {},
    review: false,
    user: {
      id: "u-1",
      username: "user",
      display_name: "用户",
      role: "employee",
    },
  } as unknown as StudioContextValue);
  return { notify, openLive };
}

function runningTask(): ReplicaPromptTask {
  return {
    id: "task-r",
    project_id: "proj-1",
    asset_id: "asset-1",
    status: "RUNNING",
    attempt: 1,
    result_version_id: null,
    error_code: null,
    error_message: null,
    failure_phase: null,
    retryable: false,
    request_id: null,
    created_at: "2026-10-16 10:00:00",
    updated_at: "2026-10-16 10:00:00",
    started_at: "2026-10-16 10:00:00",
    completed_at: null,
  };
}

describe("LongReplicaPage 上传门禁", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupContext();
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  it("超过 180 秒的视频在上传前被拦截", async () => {
    const { notify } = setupContext();
    readVideoDuration.mockResolvedValue(200);
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => {
      expect(notify).toHaveBeenCalledWith(
        "长视频时长不能超过 180 秒，请裁剪后再上传。",
      );
    });
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("不足 4 秒的视频在上传前被拦截", async () => {
    const { notify } = setupContext();
    readVideoDuration.mockResolvedValue(3);
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => {
      expect(notify).toHaveBeenCalledWith(
        "长视频时长不能少于 4 秒，请更换素材。",
      );
    });
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("超过 600MB 的文件在上传前被拦截", async () => {
    const { notify } = setupContext();
    readVideoDuration.mockResolvedValue(60);
    render(<LongReplicaPage />);
    await pickFile(makeFile({ size: 601 * 1024 * 1024 }));
    await waitFor(() => {
      expect(notify).toHaveBeenCalledWith(
        "长视频不能超过 600 MB，请压缩后再上传。",
      );
    });
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });
});

describe("LongReplicaPage 拆解链路", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupContext();
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    // 默认：没有历史运行可接回（防止用例间模块级状态互相污染断言）。
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getLatestReplicaPromptResult.mockResolvedValue(null);
  });

  it("上传后以 replica_prompt 用途上传并启动拆解，成功后展示三块结果", async () => {
    readVideoDuration.mockResolvedValue(120);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    startReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-1",
      status: "PENDING",
    });
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-1",
      status: "SUCCEEDED",
    });
    getLatestReplicaPromptResult.mockResolvedValue(resultPayload());
    render(<LongReplicaPage />);
    await pickFile(makeFile());

    await waitFor(() => {
      expect(uploadWorkbenchSourceVideo).toHaveBeenCalledWith(
        expect.any(File),
        expect.any(Function),
        expect.anything(),
        "replica_prompt",
      );
    });
    await waitFor(() => {
      expect(startReplicaPromptTask).toHaveBeenCalledWith("proj-1", "asset-1");
    });
    await waitFor(() => {
      expect(screen.getByText("复刻提示词")).toBeTruthy();
    });
    expect(screen.getByText(/庭院施工讲解视频/)).toBeTruthy();
    expect(screen.getByText("逐句口播文案")).toBeTruthy();
    expect(screen.getByText(/大家好，今天带大家看小院开工/)).toBeTruthy();
    expect(screen.getByText("全片摘要")).toBeTruthy();
    expect(screen.getByText(/场地平整/)).toBeTruthy();

    fireEvent.click(screen.getAllByRole("button", { name: "复制" })[0]);
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        RESULT_PAYLOAD.replica_prompt,
      );
    });
    expect(screen.getAllByText("已复制").length).toBeGreaterThan(0);
  });

  it("结果带软警告时在结果区展示警告列表", async () => {
    readVideoDuration.mockResolvedValue(120);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    startReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-1",
      status: "PENDING",
    });
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-1",
      status: "SUCCEEDED",
    });
    getLatestReplicaPromptResult.mockResolvedValue({
      ...resultPayload(),
      payload: {
        ...RESULT_PAYLOAD,
        warnings: [
          {
            code: "REPLICA_ASPECT_RATIO_MISSING",
            message: "提示词未写明画幅（9:16），生成端可能按默认画幅出片。",
          },
        ],
      },
    });
    render(<LongReplicaPage />);
    await pickFile(makeFile());

    await waitFor(() => {
      expect(screen.getByText(/未写明画幅/)).toBeTruthy();
    });
    // 警告是软提示：提示词本体照常展示。
    expect(screen.getByText(/庭院施工讲解视频/)).toBeTruthy();
  });

  it("余额不足时展示错误并打开钱包", async () => {
    const { openLive } = setupContext();
    readVideoDuration.mockResolvedValue(60);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    startReplicaPromptTask.mockRejectedValue(
      Object.assign(new Error("积分余额不足"), {
        code: "INSUFFICIENT_CREDITS",
      }),
    );
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => {
      expect(openLive).toHaveBeenCalledWith("wallet");
    });
    expect(screen.getByText(/积分余额不足/)).toBeTruthy();
    expect(screen.getByText("重新拆解")).toBeTruthy();
  });

  it("拆解失败时展示服务端原因并提供重新拆解", async () => {
    readVideoDuration.mockResolvedValue(60);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    startReplicaPromptTask.mockRejectedValue(new Error("上游拒绝（HTTP 502）"));
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => {
      expect(screen.getByText(/上游拒绝/)).toBeTruthy();
    });
    expect(screen.getByText("重新拆解")).toBeTruthy();
  });

  it("工作台交接：挂载即自动建任务开跑并展示结果", async () => {
    handOffLongReplicaSource("u-1", {
      projectId: "proj-9",
      assetId: "asset-9",
    });
    startReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-9",
      status: "PENDING",
    });
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-9",
      status: "SUCCEEDED",
    });
    getLatestReplicaPromptResult.mockResolvedValue(resultPayload());
    render(<LongReplicaPage />);

    await waitFor(() =>
      expect(startReplicaPromptTask).toHaveBeenCalledWith("proj-9", "asset-9"),
    );
    await waitFor(() => {
      expect(screen.getByText("复刻提示词")).toBeTruthy();
    });
    expect(screen.getByText(/庭院施工讲解视频/)).toBeTruthy();
  });
  it("挂载时接回在途任务并显示进行中状态", async () => {
    getLongReplicaSession("u-1").run = {
      projectId: "proj-1",
      assetId: "asset-1",
      taskId: "task-r",
    };
    getLatestReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    render(<LongReplicaPage />);
    await waitFor(() => {
      expect(screen.getByText(/正在看片拆解/)).toBeTruthy();
    });
    expect(screen.getAllByText(/预计 5–15 分钟/).length).toBeGreaterThan(0);
  });
});

describe("长视频任务状态回归", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupContext();
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getLatestReplicaPromptResult.mockResolvedValue(null);
  });

  it("首次上传后离开上传态，分析期间显示计时与取消入口", async () => {
    readVideoDuration.mockResolvedValue(120);
    uploadWorkbenchSourceVideo.mockImplementation(async (_file, progress) => {
      progress(100);
      return { projectId: "proj-1", assetId: "asset-1" };
    });
    startReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => expect(waitForReplicaPromptTask).toHaveBeenCalled());
    expect(screen.queryByText(/正在上传视频/)).not.toBeInTheDocument();
    expect(screen.getByText(/正在看片拆解/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消拆解" })).toBeEnabled();
  });

  it("取消请求失败后保留任务跟踪，不提示取消成功或积分将退回", async () => {
    const { notify } = setupContext();
    handOffLongReplicaSource("u-1", {
      projectId: "proj-1",
      assetId: "asset-1",
    });
    startReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    cancelReplicaPromptTask.mockRejectedValue(new Error("network unavailable"));
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    await screen.findByRole("alert");
    expect(screen.getByText(/正在看片拆解/)).toBeInTheDocument();
    expect(notify).not.toHaveBeenCalledWith(expect.stringContaining("已取消"));
    expect(notify).not.toHaveBeenCalledWith(
      expect.stringContaining("积分将退回"),
    );
  });

  it("换账号后不读取或显示上一账号的长视频任务与结果", async () => {
    handOffLongReplicaSource("u-1", {
      projectId: "proj-1",
      assetId: "asset-1",
    });
    startReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    getLatestReplicaPromptResult.mockResolvedValue(resultPayload());
    const first = render(<LongReplicaPage />);
    await screen.findByText(/庭院施工讲解视频/);
    first.unmount();
    getLatestReplicaPromptTask.mockClear();
    const current = useStudio();
    useStudio.mockReturnValue({
      ...current,
      user: { ...current.user, id: "u-2" },
    });
    render(<LongReplicaPage />);
    expect(screen.queryByText(/庭院施工讲解视频/)).not.toBeInTheDocument();
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
    expect(screen.getByText("还没有上传长视频")).toBeInTheDocument();
  });
});

describe("长视频异步边界回归", () => {
  beforeEach(() => {
    setupContext();
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getLatestReplicaPromptResult.mockResolvedValue(resultPayload());
    startReplicaPromptTask.mockResolvedValue(runningTask());
    readVideoDuration.mockResolvedValue(120);
  });

  function pendingRun() {
    handOffLongReplicaSource("u-1", {
      projectId: "proj-1",
      assetId: "asset-1",
    });
    const completion = deferred<ReplicaPromptTask>();
    waitForReplicaPromptTask.mockReturnValue(completion.promise);
    return completion;
  }

  function switchAccount(accountId: string) {
    const context = useStudio.getMockImplementation()?.() as StudioContextValue;
    useStudio.mockReturnValue({
      ...context,
      user: { ...context.user, id: accountId },
    });
  }

  it("上传进度逐步更新，创建任务期间已离开上传态，分析计时持续更新", async () => {
    vi.useFakeTimers();
    const upload = deferred<{ projectId: string; assetId: string }>();
    const creation = deferred<ReplicaPromptTask>();
    uploadWorkbenchSourceVideo.mockReturnValue(upload.promise);
    startReplicaPromptTask.mockReturnValue(creation.promise);
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    const progress = uploadWorkbenchSourceVideo.mock.calls[0][1];
    act(() => progress(37));
    expect(screen.getByText(/正在上传视频.*37%/)).toBeInTheDocument();
    expect(startReplicaPromptTask).not.toHaveBeenCalled();
    await act(async () =>
      upload.resolve({ projectId: "proj-1", assetId: "asset-1" }),
    );
    expect(screen.queryByText(/正在上传视频/)).not.toBeInTheDocument();
    expect(screen.getByText(/正在看片拆解/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "取消拆解" }),
    ).not.toBeInTheDocument();
    await act(async () => creation.resolve(runningTask()));
    expect(screen.getByRole("button", { name: "取消拆解" })).toBeEnabled();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByRole("status")).toHaveTextContent("00:02");
  });

  it.each([
    { label: "文件过大", size: 601 * 1024 * 1024, duration: 120 },
    { label: "时长超限", size: 16, duration: 181 },
  ])("再次选择$label素材不能让已有上传失去跟踪", async ({ size, duration }) => {
    const upload = deferred<{ projectId: string; assetId: string }>();
    uploadWorkbenchSourceVideo.mockReturnValue(upload.promise);
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    const [, progress, signal] = uploadWorkbenchSourceVideo.mock.calls[0];
    readVideoDuration.mockResolvedValueOnce(duration);
    await pickFile(makeFile({ size }));
    expect(signal?.aborted).toBe(false);
    act(() => progress(61));
    expect(screen.getByText(/正在上传视频.*61%/)).toBeInTheDocument();
    await act(async () =>
      upload.resolve({ projectId: "proj-1", assetId: "asset-1" }),
    );
    await screen.findByText(/庭院施工讲解视频/);
    expect(uploadWorkbenchSourceVideo).toHaveBeenCalledTimes(1);
    expect(startReplicaPromptTask).toHaveBeenCalledTimes(1);
  });

  it("连续选择文件的时长探测乱序返回时只上传较新的有效选择", async () => {
    const older = deferred<number>();
    const newer = deferred<number>();
    readVideoDuration
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const oldFile = new File(["old"], "old.mp4", { type: "video/mp4" });
    const newFile = new File(["new"], "new.mp4", { type: "video/mp4" });
    uploadWorkbenchSourceVideo.mockReturnValue(new Promise(() => {}));
    render(<LongReplicaPage />);
    await pickFile(oldFile);
    await pickFile(newFile);
    await act(async () => newer.resolve(120));
    await act(async () => older.resolve(120));
    expect(uploadWorkbenchSourceVideo).toHaveBeenCalledTimes(1);
    expect(uploadWorkbenchSourceVideo.mock.calls[0][0]).toBe(newFile);
  });

  it("服务端确认取消后显示取消态，迟到的成功轮询不能重新展示结果", async () => {
    const completion = pendingRun();
    const { notify } = useStudio();
    cancelReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "FAILED",
      error_code: "REPLICA_PROMPT_TASK_CANCELLED",
    });
    const view = render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    await screen.findByText(/本次长视频拆解已取消。积分变动/);
    expect(notify).toHaveBeenCalledWith("本次长视频拆解已取消。");
    expect(waitForReplicaPromptTask.mock.calls[0][2].aborted).toBe(true);
    await act(async () =>
      completion.resolve({
        ...runningTask(),
        status: "SUCCEEDED",
      } as ReplicaPromptTask),
    );
    expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
    expect(screen.queryByText("复刻提示词")).not.toBeInTheDocument();
    view.unmount();
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "FAILED",
      error_code: "REPLICA_PROMPT_TASK_CANCELLED",
    });
    render(<LongReplicaPage />);
    await screen.findByText(/本次长视频拆解已取消。积分变动/);
  });

  it("取消与完成竞态以服务端成功终态展示成片，不误报取消或退款", async () => {
    pendingRun();
    const { notify } = useStudio();
    cancelReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    await screen.findByText(/庭院施工讲解视频/);
    expect(notify).not.toHaveBeenCalledWith(expect.stringContaining("取消"));
    expect(notify).not.toHaveBeenCalledWith(expect.stringContaining("退回"));
  });

  it("服务端返回普通失败时显示失败原因，不把失败等同于取消", async () => {
    pendingRun();
    cancelReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "FAILED",
      error_code: "ANALYSIS_FAILED",
      error_message: "视频分析失败",
    });
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("视频分析失败");
    expect(screen.queryByText(/本次长视频拆解已取消/)).not.toBeInTheDocument();
  });

  it.each(["SUCCEEDED", "FAILED"] as const)(
    "取消请求确认%s后，旧轮询迟到的网络错误不能覆盖终态",
    async (status) => {
      const completion = pendingRun();
      cancelReplicaPromptTask.mockResolvedValue({
        ...runningTask(),
        status,
        error_code: status === "FAILED" ? "ANALYSIS_FAILED" : null,
        error_message: status === "FAILED" ? "原始分析失败" : null,
      });
      render(<LongReplicaPage />);
      fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
      if (status === "SUCCEEDED") await screen.findByText(/庭院施工讲解视频/);
      else await screen.findByText("原始分析失败");
      expect(waitForReplicaPromptTask.mock.calls[0][2].aborted).toBe(true);
      await act(async () => completion.reject(new Error("迟到的网络错误")));
      expect(screen.queryByText("迟到的网络错误")).not.toBeInTheDocument();
      if (status === "SUCCEEDED")
        expect(screen.getByText(/庭院施工讲解视频/)).toBeInTheDocument();
      else expect(screen.getByRole("alert")).toHaveTextContent("原始分析失败");
    },
  );

  it("取消请求确认成功后读取结果失败时展示实际错误，旧轮询仍停止", async () => {
    pendingRun();
    cancelReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    getLatestReplicaPromptResult.mockRejectedValue(
      new Error("结果暂时无法读取"),
    );
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "结果暂时无法读取",
    );
    expect(waitForReplicaPromptTask.mock.calls[0][2].aborted).toBe(true);
    expect(screen.queryByText(/取消结果暂未确认/)).not.toBeInTheDocument();
  });

  it("取消未确认或断网时继续跟踪，原任务随后完成仍可看到结果", async () => {
    const completion = pendingRun();
    cancelReplicaPromptTask.mockRejectedValueOnce(new Error("offline"));
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "取消结果暂未确认",
    );
    expect(waitForReplicaPromptTask.mock.calls[0][2].aborted).toBe(false);
    cancelReplicaPromptTask.mockResolvedValueOnce(runningTask());
    fireEvent.click(screen.getByRole("button", { name: "取消拆解" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("取消尚未确认");
    await act(async () =>
      completion.resolve({
        ...runningTask(),
        status: "SUCCEEDED",
      } as ReplicaPromptTask),
    );
    await screen.findByText(/庭院施工讲解视频/);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("取消请求未完成时禁用按钮，重复点击只提交一次", async () => {
    pendingRun();
    const cancellation = deferred<ReplicaPromptTask>();
    cancelReplicaPromptTask.mockReturnValue(cancellation.promise);
    render(<LongReplicaPage />);
    const button = await screen.findByRole("button", { name: "取消拆解" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "正在取消…" })).toBeDisabled();
    expect(cancelReplicaPromptTask).toHaveBeenCalledTimes(1);
    await act(async () =>
      cancellation.resolve(runningTask() as ReplicaPromptTask),
    );
    expect(screen.getByRole("button", { name: "取消拆解" })).toBeEnabled();
  });

  it("恢复时用服务端最新任务ID取消，不能沿用旧任务ID", async () => {
    getLongReplicaSession("u-1").run = {
      projectId: "proj-1",
      assetId: "asset-1",
      taskId: "old-task",
    };
    const latest = deferred<ReplicaPromptTask>();
    getLatestReplicaPromptTask.mockReturnValue(latest.promise);
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    cancelReplicaPromptTask.mockResolvedValue(runningTask());
    render(<LongReplicaPage />);
    expect(
      screen.queryByRole("button", { name: "取消拆解" }),
    ).not.toBeInTheDocument();
    await act(async () =>
      latest.resolve({ ...runningTask(), id: "latest-task" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    expect(cancelReplicaPromptTask).toHaveBeenCalledWith("latest-task");
  });

  it("重试创建新任务期间不允许取消上一条已失败任务", async () => {
    getLongReplicaSession("u-1").run = {
      projectId: "proj-1",
      assetId: "asset-1",
      taskId: "old-task",
    };
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "FAILED",
      error_code: "ANALYSIS_FAILED",
      error_message: "分析失败",
    });
    const creation = deferred<ReplicaPromptTask>();
    startReplicaPromptTask.mockReturnValue(creation.promise);
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "重新拆解" }));
    expect(screen.getByText(/正在看片拆解/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "取消拆解" }),
    ).not.toBeInTheDocument();
    await act(async () =>
      creation.resolve({ ...runningTask(), id: "retry-task" }),
    );
    cancelReplicaPromptTask.mockResolvedValue(runningTask());
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    expect(cancelReplicaPromptTask).toHaveBeenCalledExactlyOnceWith(
      "retry-task",
    );
  });

  it("轮询已展示成功结果后，迟到的成功取消回执不再查询并覆盖结果", async () => {
    const completion = pendingRun();
    const cancellation = deferred<ReplicaPromptTask>();
    cancelReplicaPromptTask.mockReturnValue(cancellation.promise);
    getLatestReplicaPromptResult
      .mockResolvedValueOnce(resultPayload())
      .mockRejectedValueOnce(new Error("重复查询断网"));
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    await act(async () =>
      completion.resolve({ ...runningTask(), status: "SUCCEEDED" }),
    );
    await screen.findByText(/庭院施工讲解视频/);
    await act(async () =>
      cancellation.resolve({ ...runningTask(), status: "SUCCEEDED" }),
    );
    expect(screen.getByText(/庭院施工讲解视频/)).toBeInTheDocument();
    expect(screen.queryByText("重复查询断网")).not.toBeInTheDocument();
    expect(getLatestReplicaPromptResult).toHaveBeenCalledTimes(1);
  });

  it("同一页面切换账号立即清空旧结果，切回原账号只恢复它自己的任务", async () => {
    pendingRun();
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    const view = render(<LongReplicaPage />);
    await screen.findByText(/庭院施工讲解视频/);
    getLatestReplicaPromptTask.mockClear();
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    expect(screen.getByText("还没有上传长视频")).toBeInTheDocument();
    expect(screen.queryByText(/庭院施工讲解视频/)).not.toBeInTheDocument();
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    switchAccount("u-1");
    view.rerender(<LongReplicaPage />);
    await screen.findByText(/庭院施工讲解视频/);
    expect(getLatestReplicaPromptTask).toHaveBeenCalledWith("proj-1");
  });

  it("其他账号不能消费工作台的待启动来源，原账号回来才启动一次", async () => {
    handOffLongReplicaSource("u-1", {
      projectId: "proj-1",
      assetId: "asset-1",
    });
    switchAccount("u-2");
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    const view = render(<LongReplicaPage />);
    expect(startReplicaPromptTask).not.toHaveBeenCalled();
    switchAccount("u-1");
    view.rerender(<LongReplicaPage />);
    await screen.findByRole("button", { name: "取消拆解" });
    expect(startReplicaPromptTask).toHaveBeenCalledTimes(1);
  });

  it("读取本地时长期间切账号，旧文件不得继续上传", async () => {
    const duration = deferred<number>();
    readVideoDuration.mockReturnValue(duration.promise);
    const view = render(<LongReplicaPage />);
    await pickFile(makeFile());
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    await act(async () => duration.resolve(120));
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
    expect(startReplicaPromptTask).not.toHaveBeenCalled();
  });

  it("上传中切账号会终止上传，迟到的上传完成不能再创建任务", async () => {
    const upload = deferred<{ projectId: string; assetId: string }>();
    uploadWorkbenchSourceVideo.mockReturnValue(upload.promise);
    const view = render(<LongReplicaPage />);
    await pickFile(makeFile());
    const signal = uploadWorkbenchSourceVideo.mock.calls[0][2];
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    expect(signal?.aborted).toBe(true);
    await act(async () =>
      upload.resolve({ projectId: "proj-1", assetId: "asset-1" }),
    );
    expect(startReplicaPromptTask).not.toHaveBeenCalled();
    expect(screen.getByText("还没有上传长视频")).toBeInTheDocument();
  });

  it("结果查询中切账号，旧结果不能写回缓存或新账号界面", async () => {
    pendingRun();
    const result = deferred<ReplicaPromptVersion>();
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
    });
    getLatestReplicaPromptResult.mockReturnValue(result.promise);
    const view = render(<LongReplicaPage />);
    await waitFor(() =>
      expect(getLatestReplicaPromptResult).toHaveBeenCalled(),
    );
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    await act(async () => result.resolve(resultPayload()));
    expect(screen.queryByText(/庭院施工讲解视频/)).not.toBeInTheDocument();
    expect(screen.getByText("还没有上传长视频")).toBeInTheDocument();
    expect(getLongReplicaSession("u-1").result).toBeNull();
  });

  it("换账号会终止旧任务轮询，迟到结果不能继续请求旧项目结果", async () => {
    const completion = pendingRun();
    const view = render(<LongReplicaPage />);
    await screen.findByRole("button", { name: "取消拆解" });
    const signal = waitForReplicaPromptTask.mock.calls[0][2];
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    expect(signal.aborted).toBe(true);
    await act(async () =>
      completion.resolve({
        ...runningTask(),
        status: "SUCCEEDED",
      } as ReplicaPromptTask),
    );
    expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
  });

  it("取消请求中切账号，迟到成功不会通知新账号或停止新账号任务", async () => {
    pendingRun();
    const cancellation = deferred<ReplicaPromptTask>();
    const { notify } = useStudio();
    cancelReplicaPromptTask.mockReturnValue(cancellation.promise);
    const view = render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    await act(async () =>
      cancellation.resolve({
        ...runningTask(),
        status: "FAILED",
        error_code: "REPLICA_PROMPT_TASK_CANCELLED",
      } as ReplicaPromptTask),
    );
    expect(notify).not.toHaveBeenCalledWith("本次长视频拆解已取消。");
    expect(screen.getByText("还没有上传长视频")).toBeInTheDocument();
  });

  it("登出清理后迟到的建任务响应不能复活旧缓存，同账号重登也为空", async () => {
    handOffLongReplicaSource("u-1", {
      projectId: "proj-1",
      assetId: "asset-1",
    });
    const creation = deferred<ReplicaPromptTask>();
    startReplicaPromptTask.mockReturnValue(creation.promise);
    const view = render(<LongReplicaPage />);
    await waitFor(() => expect(startReplicaPromptTask).toHaveBeenCalled());
    clearAccountResidue("u-1");
    view.unmount();
    await act(async () => creation.resolve(runningTask() as ReplicaPromptTask));
    render(<LongReplicaPage />);
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
    expect(waitForReplicaPromptTask).not.toHaveBeenCalled();
    expect(screen.getByText("还没有上传长视频")).toBeInTheDocument();
  });

  it("StrictMode 重挂载复用正在创建的任务，进度可恢复且不重复提交", async () => {
    handOffLongReplicaSource("u-1", {
      projectId: "proj-1",
      assetId: "asset-1",
    });
    const creation = deferred<ReplicaPromptTask>();
    startReplicaPromptTask.mockReturnValue(creation.promise);
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    render(
      <StrictMode>
        <LongReplicaPage />
      </StrictMode>,
    );
    await act(async () => creation.resolve(runningTask() as ReplicaPromptTask));
    await screen.findByRole("button", { name: "取消拆解" });
    expect(startReplicaPromptTask).toHaveBeenCalledTimes(1);
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
    expect(screen.getByText(/正在看片拆解/)).toBeInTheDocument();
  });
});
