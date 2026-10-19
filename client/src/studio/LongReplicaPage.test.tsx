import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

import { handOffLongReplicaSource, LongReplicaPage } from "./LongReplicaPage";

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

function runningTask() {
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
    handOffLongReplicaSource({ projectId: "proj-9", assetId: "asset-9" });
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
    getLatestReplicaPromptTask.mockResolvedValue(runningTask());
    waitForReplicaPromptTask.mockReturnValue(new Promise(() => {}));
    render(<LongReplicaPage />);
    await waitFor(() => {
      expect(screen.getByText(/正在看片拆解/)).toBeTruthy();
    });
    expect(screen.getAllByText(/预计 5–15 分钟/).length).toBeGreaterThan(0);
  });
});
