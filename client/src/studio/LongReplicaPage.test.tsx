import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ReplicaPromptHistoryItem,
  ReplicaPromptTask,
  ReplicaPromptVersion,
} from "../api";
import { fetchReplicaPromptHistory } from "../api";
import { clearAccountResidue } from "./draftResidue";
import type { StudioContextValue } from "./types";

const {
  useStudio,
  readVideoDuration,
  uploadWorkbenchSourceVideo,
  startReplicaPromptTask,
  getLatestReplicaPromptTask,
  getLatestReplicaPromptResult,
  getReplicaPromptVersionById,
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
  getReplicaPromptVersionById: vi.fn(),
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
  fetchReplicaPromptHistory: vi.fn(),
  getReplicaPromptVersionById,
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
  schema_version: "long-analysis.v1",
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
  shots: [
    {
      shot_id: "s1",
      start_time: 0,
      end_time: 9,
      shot_type: "中景",
      camera_motion: "手持跟随",
      subject: "工装讲解者",
      person_count: 1,
      action: "面向镜头介绍施工进度",
      scene: "庭院施工现场",
      spoken_text: "大家好，今天带大家看小院开工。",
      transition: "硬切",
    },
  ],
};

function resultPayload() {
  return {
    id: "ver-1",
    project_id: "proj-1",
    asset_id: "asset-1",
    kind: "long_analysis",
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

async function pickFile(
  file: File,
  { autoStart = true }: { autoStart?: boolean } = {},
) {
  const input = document.querySelector('input[type="file"]');
  expect(input).not.toBeNull();
  await act(async () => {
    fireEvent.change(input as HTMLInputElement, { target: { files: [file] } });
  });
  if (!autoStart) return;
  // 新流程（2026-11-22）：选文件停在待确认占位卡，点「开始拆解」才上传并提交。
  const startButton = await screen.findByRole("button", { name: "开始拆解" });
  await act(async () => {
    fireEvent.click(startButton);
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

describe("长视频任务结果绑定", () => {
  const boundVersion = (): ReplicaPromptVersion => ({
    ...resultPayload(),
    id: "version-a",
    kind: "long_analysis",
    payload: { ...RESULT_PAYLOAD, replica_prompt: "本次任务绑定的版本 A" },
  });
  const mismatches = [
    { field: "id", value: "other-version" },
    { field: "project_id", value: "other-project" },
    { field: "asset_id", value: "other-asset" },
    { field: "kind", value: "replica_prompt" },
  ];

  beforeEach(() => {
    setupContext();
    getLongReplicaSession("u-1").run = {
      projectId: "proj-1",
      assetId: "asset-1",
      taskId: "task-r",
    };
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "version-a",
    });
    vi.mocked(getReplicaPromptVersionById).mockResolvedValue(boundVersion());
    getLatestReplicaPromptResult.mockResolvedValue({
      ...boundVersion(),
      id: "version-b",
      payload: { ...RESULT_PAYLOAD, replica_prompt: "另一任务的最新版本 B" },
    });
  });

  it("恢复成功任务只显示其版本 A，即使项目最新版本已是 B", async () => {
    render(<LongReplicaPage />);
    expect(await screen.findByText("本次任务绑定的版本 A")).toBeInTheDocument();
    expect(screen.queryByText("另一任务的最新版本 B")).not.toBeInTheDocument();
    expect(getReplicaPromptVersionById).toHaveBeenCalledExactlyOnceWith(
      "version-a",
    );
    expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
    expect(getLatestReplicaPromptTask).toHaveBeenCalledWith(
      "proj-1",
      "storyboard",
    );
    expect(getLongReplicaSession("u-1").result?.id).toBe("version-a");
  });

  it.each([false, true])(
    "成功任务缺少结果版本 ID 时明确失败（已有缓存：%s）",
    async (cached) => {
      if (cached) getLongReplicaSession("u-1").result = boundVersion();
      getLatestReplicaPromptTask.mockResolvedValue({
        ...runningTask(),
        status: "SUCCEEDED",
        result_version_id: null,
      });
      render(<LongReplicaPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "缺少结果版本",
      );
      expect(
        screen.queryByText("本次任务绑定的版本 A"),
      ).not.toBeInTheDocument();
      expect(getReplicaPromptVersionById).not.toHaveBeenCalled();
      expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
      expect(getLongReplicaSession("u-1").result).toBeNull();
    },
  );

  it("指定结果版本不存在时明确失败，不回退项目最新结果", async () => {
    vi.mocked(getReplicaPromptVersionById).mockResolvedValue(null);
    render(<LongReplicaPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "未找到本次拆解的结果版本",
    );
    expect(getReplicaPromptVersionById).toHaveBeenCalledWith("version-a");
    expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
    expect(getLongReplicaSession("u-1").result).toBeNull();
  });

  it.each(mismatches)(
    "结果版本的 $field 不匹配时拒绝展示",
    async ({ field, value }) => {
      vi.mocked(getReplicaPromptVersionById).mockResolvedValue({
        ...boundVersion(),
        [field]: value,
      });
      render(<LongReplicaPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "结果版本与本次拆解不匹配",
      );
      expect(
        screen.queryByText("本次任务绑定的版本 A"),
      ).not.toBeInTheDocument();
      expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
      expect(getLongReplicaSession("u-1").result).toBeNull();
    },
  );

  it.each(mismatches)(
    "同任务缓存的 $field 不匹配时重新读取指定版本",
    async ({ field, value }) => {
      getLongReplicaSession("u-1").result = {
        ...boundVersion(),
        [field]: value,
        payload: { ...RESULT_PAYLOAD, replica_prompt: "不能沿用的缓存结果" },
      };
      render(<LongReplicaPage />);
      expect(
        await screen.findByText("本次任务绑定的版本 A"),
      ).toBeInTheDocument();
      expect(screen.queryByText("不能沿用的缓存结果")).not.toBeInTheDocument();
      expect(getReplicaPromptVersionById).toHaveBeenCalledExactlyOnceWith(
        "version-a",
      );
      expect(getLatestReplicaPromptResult).not.toHaveBeenCalled();
      expect(getLongReplicaSession("u-1").result).toEqual(boundVersion());
    },
  );
});

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
    await pickFile(makeFile(), { autoStart: false });
    await waitFor(() => {
      expect(notify).toHaveBeenCalledWith(
        "长视频时长不能超过 180 秒，请裁剪后再上传。",
      );
    });
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("不足 30 秒的视频在上传前被拦截并引导去 <30s 页签", async () => {
    const { notify } = setupContext();
    readVideoDuration.mockResolvedValue(3);
    render(<LongReplicaPage />);
    await pickFile(makeFile(), { autoStart: false });
    await waitFor(() => {
      expect(notify).toHaveBeenCalledWith(
        "长视频拆解仅支持 30 秒以上的视频，30 秒以内请到「<30s 视频拆解」页签。",
      );
    });
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("超过 600MB 的文件在上传前被拦截", async () => {
    const { notify } = setupContext();
    readVideoDuration.mockResolvedValue(60);
    render(<LongReplicaPage />);
    await pickFile(makeFile({ size: 601 * 1024 * 1024 }), {
      autoStart: false,
    });
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
    getReplicaPromptVersionById.mockResolvedValue(null);
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
      result_version_id: "ver-1",
    });
    getReplicaPromptVersionById.mockResolvedValue(resultPayload());
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
      expect(startReplicaPromptTask).toHaveBeenCalledWith(
        "proj-1",
        "asset-1",
        false,
        "storyboard",
      );
    });
    await waitFor(() => {
      expect(screen.getByText("复刻提示词")).toBeTruthy();
    });
    expect(screen.getByText(/庭院施工讲解视频/)).toBeTruthy();
    expect(screen.getByText("逐句口播文案")).toBeTruthy();
    expect(
      screen.getAllByText(/大家好，今天带大家看小院开工/).length,
    ).toBeGreaterThan(0);
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

  it("storyboard 结果渲染分镜表（时间轴/景别/动作/场景）", async () => {
    setupContext();
    readVideoDuration.mockResolvedValue(120);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getReplicaPromptVersionById.mockResolvedValue(resultPayload());
    startReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => {
      expect(screen.getByText(/分镜表（1 段）/)).toBeTruthy();
    });
    expect(screen.getByText("手持跟随")).toBeTruthy();
    expect(screen.getByText("面向镜头介绍施工进度")).toBeTruthy();
    expect(screen.getByText(/庭院施工现场/)).toBeTruthy();
    // 分镜表在前，复刻提示词等文本块在其后保留。
    expect(screen.getByText("复刻提示词")).toBeTruthy();
    expect(screen.getByText("全片摘要")).toBeTruthy();
  });

  it("完整显示运动、动作段边界、小数时间和待确认问题", async () => {
    setupContext();
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getLongReplicaSession("u-1").result = {
      ...resultPayload(),
      payload: {
        ...RESULT_PAYLOAD,
        shots: [
          {
            ...RESULT_PAYLOAD.shots[0],
            start_time: 0.125,
            end_time: 1.375,
            segment_kind: "ACTION_BEAT",
            boundary_reason: "同镜头内开始走动",
            scene_lighting: "左侧柔光",
            motion: {
              subject_motion_state: "WALKING+GESTURING_ONLY",
              subject_direction: "lateral",
              subject_displacement: "走向门口",
              hand_action: "抬手",
              camera_motion: "ORBIT",
              relative_motion: "摄影机环绕人物",
            },
          },
        ],
        consistency_issues: [
          {
            code: "MOTION_CONFLICT",
            shot_id: "s1",
            message: "人物运动状态与动作矛盾",
          },
        ],
      },
    };
    render(<LongReplicaPage />);
    const table = screen.getByRole("table");
    expect(within(table).getByText("00:00.125-00:01.375")).toBeInTheDocument();
    expect(within(table).getByText(/人物：行走＋手势/)).toBeInTheDocument();
    expect(within(table).getByText(/镜头：环绕/)).toBeInTheDocument();
    expect(within(table).getByText(/同镜头内开始走动/)).toBeInTheDocument();
    expect(within(table).getByText(/左侧柔光/)).toBeInTheDocument();
    expect(screen.getByText(/s1：人物运动状态与动作矛盾/)).toBeInTheDocument();
  });

  it("拆解历史弹窗列出历史并可载入历史版本", async () => {
    setupContext();
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getReplicaPromptVersionById.mockResolvedValue(resultPayload());
    startReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    vi.mocked(fetchReplicaPromptHistory).mockResolvedValue([
      {
        id: "ver-old",
        project_id: "proj-1",
        asset_id: "asset-1",
        kind: "long_analysis",
        version_number: 1,
        created_by_user_id: "owner-1",
        created_at: "2026-11-22 10:00:00",
      },
    ]);
    vi.mocked(getReplicaPromptVersionById).mockResolvedValue(resultPayload());
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    await waitFor(() => {
      expect(screen.getByText(/分镜表（1 段）/)).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    await screen.findByText(/分镜表拆解/);
    fireEvent.click(screen.getByRole("button", { name: "载入此版本" }));
    await waitFor(() => {
      expect(vi.mocked(getReplicaPromptVersionById)).toHaveBeenCalledWith(
        "ver-old",
      );
    });
    expect(screen.getByText(/分镜表（1 段）/)).toBeTruthy();
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
      result_version_id: "ver-1",
    });
    getReplicaPromptVersionById.mockResolvedValue({
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
      project_id: "proj-9",
      asset_id: "asset-9",
      status: "PENDING",
    });
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "task-9",
      project_id: "proj-9",
      asset_id: "asset-9",
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    getReplicaPromptVersionById.mockResolvedValue({
      ...resultPayload(),
      project_id: "proj-9",
      asset_id: "asset-9",
    });
    render(<LongReplicaPage />);

    await waitFor(() =>
      expect(startReplicaPromptTask).toHaveBeenCalledWith(
        "proj-9",
        "asset-9",
        false,
        "storyboard",
      ),
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
    getReplicaPromptVersionById.mockResolvedValue(null);
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
      result_version_id: "ver-1",
    });
    getReplicaPromptVersionById.mockResolvedValue(resultPayload());
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
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
  });
});

describe("长视频账号历史恢复", () => {
  const savedVersion = (): ReplicaPromptVersion => ({
    ...resultPayload(),
    id: "saved-version",
    project_id: "other-own-project",
    kind: "long_analysis",
    payload: { ...RESULT_PAYLOAD, replica_prompt: "选中的旧项目历史版本" },
  });
  const historyItem = (): ReplicaPromptHistoryItem => ({
    id: "saved-version",
    project_id: "other-own-project",
    asset_id: "saved-asset",
    kind: "long_analysis",
    version_number: 9,
    created_by_user_id: "u-1",
    created_at: "2026-11-20 10:00:00",
  });

  beforeEach(() => {
    setupContext();
    vi.mocked(fetchReplicaPromptHistory).mockResolvedValue([historyItem()]);
    vi.mocked(getReplicaPromptVersionById).mockResolvedValue(savedVersion());
  });

  function switchAccount(accountId: string) {
    const context = useStudio.getMockImplementation()?.() as StudioContextValue;
    useStudio.mockReturnValue({
      ...context,
      user: { ...context.user, id: accountId },
    });
  }

  it("冷启动和登出重登后没有当前项目也能打开并载入账号历史", async () => {
    const first = render(<LongReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    await screen.findByRole("dialog", { name: "长视频拆解历史" });
    fireEvent.click(await screen.findByRole("button", { name: "载入此版本" }));
    await screen.findByText("选中的旧项目历史版本");
    expect(fetchReplicaPromptHistory).toHaveBeenCalledWith();
    expect(startReplicaPromptTask).not.toHaveBeenCalled();
    clearAccountResidue("u-1");
    first.unmount();

    render(<LongReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    fireEvent.click(await screen.findByRole("button", { name: "载入此版本" }));
    await screen.findByText("选中的旧项目历史版本");
    expect(fetchReplicaPromptHistory).toHaveBeenCalledTimes(2);
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
  });

  it("选中另一项目历史后重挂载仍保留选择，不恢复旧运行的最新结果", async () => {
    const cache = getLongReplicaSession("u-1");
    cache.run = { projectId: "proj-1", assetId: "asset-1", taskId: "task-r" };
    cache.result = resultPayload() as ReplicaPromptVersion;
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    const first = render(<LongReplicaPage />);
    await screen.findByText(/庭院施工讲解视频/);
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    fireEvent.click(await screen.findByRole("button", { name: "载入此版本" }));
    await screen.findByText("选中的旧项目历史版本");
    first.unmount();
    getLatestReplicaPromptTask.mockClear();

    render(<LongReplicaPage />);
    await screen.findByText("选中的旧项目历史版本");
    expect(screen.queryByText(/庭院施工讲解视频/)).not.toBeInTheDocument();
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
    expect(cache.run).toBeNull();
    expect(cache.result?.id).toBe("saved-version");
    expect(cache.pendingAutoStart).toBe(false);
    expect(cache.starting).toBeNull();
  });

  it.each(["成功", "失败"])(
    "切账号后旧历史请求迟到%s不结束新账号加载或混入旧记录",
    async (outcome) => {
      const previous = deferred<ReplicaPromptHistoryItem[]>();
      const next = deferred<ReplicaPromptHistoryItem[]>();
      vi.mocked(fetchReplicaPromptHistory)
        .mockReturnValueOnce(previous.promise)
        .mockReturnValueOnce(next.promise);
      const view = render(<LongReplicaPage />);
      fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
      await screen.findByText("正在读取拆解历史…");
      switchAccount("u-2");
      view.rerender(<LongReplicaPage />);
      fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
      await act(async () => {
        if (outcome === "成功") previous.resolve([historyItem()]);
        else previous.reject(new Error("旧账号历史读取失败"));
      });
      expect(screen.getByText("正在读取拆解历史…")).toBeInTheDocument();
      expect(screen.queryByText(/第 9 版/)).not.toBeInTheDocument();
      expect(screen.queryByText("旧账号历史读取失败")).not.toBeInTheDocument();
      await act(async () =>
        next.resolve([
          { ...historyItem(), id: "account-2-version", version_number: 2 },
        ]),
      );
      expect(screen.getByText(/第 2 版/)).toBeInTheDocument();
      expect(screen.queryByText(/第 9 版/)).not.toBeInTheDocument();
    },
  );

  it("载入历史期间切账号，旧详情不能写回任何账号缓存", async () => {
    const previous = deferred<ReplicaPromptVersion>();
    vi.mocked(getReplicaPromptVersionById).mockReturnValue(previous.promise);
    const view = render(<LongReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    fireEvent.click(await screen.findByRole("button", { name: "载入此版本" }));
    await waitFor(() => expect(getReplicaPromptVersionById).toHaveBeenCalled());
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    await act(async () => previous.resolve(savedVersion()));
    expect(screen.queryByText("选中的旧项目历史版本")).not.toBeInTheDocument();
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
    expect(getLongReplicaSession("u-1").result).toBeNull();
    expect(getLongReplicaSession("u-2").result).toBeNull();
  });

  it.each([
    ["空对象或纯编号", [{}, { shot_id: "s1" }, { person_count: 1 }]],
    ["空值与标量", [null, 7, false, "坏镜头", []]],
    ["对象文本", [{ subject: { text: "不能直接渲染" }, scene: ["坏场景"] }]],
    [
      "无效数值",
      [
        { start_time: Number.NaN, end_time: Number.POSITIVE_INFINITY },
        {
          start_time: true,
          end_time: Number.NEGATIVE_INFINITY,
          person_count: -1,
        },
        { start_time: 0 },
        { start_time: 2, end_time: 1 },
      ],
    ],
  ])("历史分镜含%s时仍可读提示词且不展示空镜头", async (_name, shots) => {
    vi.mocked(getReplicaPromptVersionById).mockResolvedValue({
      ...savedVersion(),
      payload: { ...savedVersion().payload, shots },
    } as unknown as ReplicaPromptVersion);
    render(<LongReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    fireEvent.click(await screen.findByRole("button", { name: "载入此版本" }));
    expect(await screen.findByText("选中的旧项目历史版本")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("历史分镜混有坏行和坏字段时只展示规范化后的有效内容", async () => {
    vi.mocked(getReplicaPromptVersionById).mockResolvedValue({
      ...savedVersion(),
      payload: {
        ...savedVersion().payload,
        shots: [
          null,
          {},
          {
            " \nshot_id ": "history-shot",
            " subject\n": "保留的历史主体",
            start_time: 0,
            " end_time ": 60,
            action: { text: "不能直接渲染" },
            scene: ["坏场景"],
            spoken_text: false,
            camera_motion: "固定机位",
            unknown: { debug: "不展示" },
          },
        ],
      },
    } as unknown as ReplicaPromptVersion);
    render(<LongReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
    fireEvent.click(await screen.findByRole("button", { name: "载入此版本" }));
    expect(await screen.findByText("分镜表（1 段）")).toBeInTheDocument();
    expect(screen.getByText("保留的历史主体")).toBeInTheDocument();
    expect(screen.getByText("history-shot")).toBeInTheDocument();
    expect(screen.getByText("00:00-01:00")).toBeInTheDocument();
    expect(screen.getByText("固定机位")).toBeInTheDocument();
    expect(screen.queryByText("不能直接渲染")).not.toBeInTheDocument();
    expect(screen.getByText("选中的旧项目历史版本")).toBeInTheDocument();
  });
});

describe("长视频口播安全展示", () => {
  beforeEach(() => {
    setupContext();
  });

  async function showSpokenScript(spokenScript: unknown, source = "历史") {
    getReplicaPromptVersionById.mockResolvedValue({
      ...resultPayload(),
      payload: {
        ...RESULT_PAYLOAD,
        spoken_script: spokenScript,
        replica_prompt: "口播异常也保留的提示词",
      },
    });
    if (source === "新任务") {
      getLongReplicaSession("u-1").run = {
        projectId: "proj-1",
        assetId: "asset-1",
        taskId: "task-r",
      };
      getLatestReplicaPromptTask.mockResolvedValue({
        ...runningTask(),
        status: "SUCCEEDED",
        result_version_id: "ver-1",
      });
      await act(async () => {
        render(<LongReplicaPage />);
      });
    } else {
      vi.mocked(fetchReplicaPromptHistory).mockResolvedValue([
        {
          ...resultPayload(),
        },
      ]);
      render(<LongReplicaPage />);
      fireEvent.click(screen.getByRole("button", { name: "拆解历史" }));
      const load = await screen.findByRole("button", { name: "载入此版本" });
      await act(async () => {
        fireEvent.click(load);
      });
    }
  }

  it.each(["历史", "新任务"])(
    "%s口播混有空行和非对象时仍展示并可复制有效口播",
    async (source) => {
      await showSpokenScript(
        [
          null,
          7,
          false,
          "坏行",
          [],
          {},
          { text: " \n" },
          { " start_time\n": 0, " end_time ": 2, " text ": "有效口播" },
          { text: "缺时间的口播" },
        ],
        source,
      );
      expect(screen.getByText("口播异常也保留的提示词")).toBeInTheDocument();
      expect(screen.getByText("分镜表（1 段）")).toBeInTheDocument();
      const block = screen
        .getByText("逐句口播文案")
        .closest("section") as HTMLElement;
      expect(within(block).getAllByRole("listitem")).toHaveLength(2);
      expect(within(block).getByText("有效口播")).toBeInTheDocument();
      fireEvent.click(within(block).getByRole("button", { name: "复制" }));
      await waitFor(() =>
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
          "00:00-00:02 有效口播\n--:-----:-- 缺时间的口播",
        ),
      );
    },
  );

  it("历史口播的对象或数组正文被过滤，不能作为React子节点", async () => {
    await showSpokenScript([
      { start_time: 0, end_time: 1, text: { bad: "对象正文" } },
      { text: ["数组正文"] },
      { text: true },
      { text: 3 },
      { start_time: 1, end_time: 2, text: "保留有效正文" },
    ]);
    expect(screen.getByText("保留有效正文")).toBeInTheDocument();
    expect(screen.queryByText("对象正文")).not.toBeInTheDocument();
    expect(screen.queryByText("数组正文")).not.toBeInTheDocument();
    const block = screen
      .getByText("逐句口播文案")
      .closest("section") as HTMLElement;
    expect(within(block).getAllByRole("listitem")).toHaveLength(1);
  });

  it("历史口播全为坏行时仍保留有用结果，显示无可辨识口播", async () => {
    await showSpokenScript([
      null,
      7,
      false,
      "坏行",
      [],
      {},
      { text: [] },
      { text: "  " },
    ]);
    expect(screen.getByText("口播异常也保留的提示词")).toBeInTheDocument();
    expect(screen.getByText("分镜表（1 段）")).toBeInTheDocument();
    expect(screen.getByText("这条视频没有可辨识的口播。")).toBeInTheDocument();
  });

  it("历史口播时间对象不进入模板key或复制正文", async () => {
    await showSpokenScript([
      {
        start_time: { toString: null },
        end_time: ["坏时间"],
        text: "时间未知仍保留正文",
      },
    ]);
    const block = screen
      .getByText("逐句口播文案")
      .closest("section") as HTMLElement;
    expect(within(block).getByText("--:-----:--")).toBeInTheDocument();
    fireEvent.click(within(block).getByRole("button", { name: "复制" }));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "--:-----:-- 时间未知仍保留正文",
      ),
    );
  });
});

describe("长视频异步边界回归", () => {
  beforeEach(() => {
    setupContext();
    getLatestReplicaPromptTask.mockResolvedValue(null);
    getReplicaPromptVersionById.mockResolvedValue(resultPayload());
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
    // fake timers 下 findByRole 会卡死：占位卡同步渲染后直接同步点「开始拆解」。
    await pickFile(makeFile(), { autoStart: false });
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "开始拆解" }));
    });
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
      result_version_id: "ver-1",
    });
    render(<LongReplicaPage />);
    await pickFile(makeFile());
    const [, progress, signal] = uploadWorkbenchSourceVideo.mock.calls[0];
    readVideoDuration.mockResolvedValueOnce(duration);
    await pickFile(makeFile({ size }), { autoStart: false });
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
    // 新流程：两次选择都停在待确认占位卡，探测乱序回写后只允许最新的占位开始。
    await pickFile(oldFile, { autoStart: false });
    await pickFile(newFile, { autoStart: false });
    await act(async () => newer.resolve(120));
    await act(async () => older.resolve(120));
    const startButton = await screen.findByRole("button", {
      name: "开始拆解",
    });
    await act(async () => {
      fireEvent.click(startButton);
    });
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
        result_version_id: "ver-1",
      } as ReplicaPromptTask),
    );
    expect(getReplicaPromptVersionById).not.toHaveBeenCalled();
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
      result_version_id: "ver-1",
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
        result_version_id: status === "SUCCEEDED" ? "ver-1" : null,
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
      result_version_id: "ver-1",
    });
    getReplicaPromptVersionById.mockRejectedValue(
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
        result_version_id: "ver-1",
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
    getReplicaPromptVersionById
      .mockResolvedValueOnce(resultPayload())
      .mockRejectedValueOnce(new Error("重复查询断网"));
    render(<LongReplicaPage />);
    fireEvent.click(await screen.findByRole("button", { name: "取消拆解" }));
    await act(async () =>
      completion.resolve({
        ...runningTask(),
        status: "SUCCEEDED",
        result_version_id: "ver-1",
      }),
    );
    await screen.findByText(/庭院施工讲解视频/);
    await act(async () =>
      cancellation.resolve({
        ...runningTask(),
        status: "SUCCEEDED",
        result_version_id: "ver-1",
      }),
    );
    expect(screen.getByText(/庭院施工讲解视频/)).toBeInTheDocument();
    expect(screen.queryByText("重复查询断网")).not.toBeInTheDocument();
    expect(getReplicaPromptVersionById).toHaveBeenCalledTimes(1);
  });

  it("恢复到服务端新任务后再次离页返回，不能把旧结果误当作新任务结果", async () => {
    const cache = getLongReplicaSession("u-1");
    cache.run = { projectId: "proj-1", assetId: "asset-1", taskId: "old-task" };
    cache.result = resultPayload() as ReplicaPromptVersion;
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "new-task",
    });
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    const view = render(<LongReplicaPage />);
    await screen.findByRole("button", { name: "取消拆解" });
    view.unmount();
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      id: "new-task",
      status: "SUCCEEDED",
      result_version_id: "new-version",
    });
    getReplicaPromptVersionById.mockResolvedValue({
      ...resultPayload(),
      id: "new-version",
      payload: { ...RESULT_PAYLOAD, replica_prompt: "新任务的最新拆解结果" },
    });
    render(<LongReplicaPage />);
    expect(await screen.findByText("新任务的最新拆解结果")).toBeInTheDocument();
    expect(screen.queryByText(/一段庭院施工讲解视频/)).not.toBeInTheDocument();
  });

  it("同一页面切换账号立即清空旧结果，切回原账号只恢复它自己的任务", async () => {
    pendingRun();
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    const view = render(<LongReplicaPage />);
    await screen.findByText(/庭院施工讲解视频/);
    getLatestReplicaPromptTask.mockClear();
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
    expect(screen.queryByText(/庭院施工讲解视频/)).not.toBeInTheDocument();
    expect(getLatestReplicaPromptTask).not.toHaveBeenCalled();
    getLatestReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    switchAccount("u-1");
    view.rerender(<LongReplicaPage />);
    await screen.findByText(/庭院施工讲解视频/);
    expect(getLatestReplicaPromptTask).toHaveBeenCalledWith(
      "proj-1",
      "storyboard",
    );
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
    await pickFile(makeFile(), { autoStart: false });
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
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
  });

  it("结果查询中切账号，旧结果不能写回缓存或新账号界面", async () => {
    pendingRun();
    const result = deferred<ReplicaPromptVersion>();
    waitForReplicaPromptTask.mockResolvedValue({
      ...runningTask(),
      status: "SUCCEEDED",
      result_version_id: "ver-1",
    });
    getReplicaPromptVersionById.mockReturnValue(result.promise);
    const view = render(<LongReplicaPage />);
    await waitFor(() => expect(getReplicaPromptVersionById).toHaveBeenCalled());
    switchAccount("u-2");
    view.rerender(<LongReplicaPage />);
    await act(async () => result.resolve(resultPayload()));
    expect(screen.queryByText(/庭院施工讲解视频/)).not.toBeInTheDocument();
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
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
        result_version_id: "ver-1",
      } as ReplicaPromptTask),
    );
    expect(getReplicaPromptVersionById).not.toHaveBeenCalled();
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
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
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
    expect(screen.getByText("拖拽视频到此处，或")).toBeInTheDocument();
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
