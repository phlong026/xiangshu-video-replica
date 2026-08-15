import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const healthResponse = { status: "ok", service: "video-replica-api" };
const employeeUser = {
  id: "employee_1",
  username: "employee_1",
  display_name: "林夏",
  role: "employee",
};
const adminUser = {
  id: "admin_1",
  username: "admin_1",
  display_name: "管理员",
  role: "admin",
};
const auditorUser = {
  id: "auditor_1",
  username: "auditor_1",
  display_name: "审计员",
  role: "auditor",
};

type FetchMock = (url: string, options?: RequestInit) => Promise<unknown>;

function withAuth(handler: FetchMock, user = employeeUser) {
  return vi.fn((url: string, options?: RequestInit) => {
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve({ ok: true, json: async () => user });
    }
    return handler(url, options);
  });
}

async function enterWorkspace() {
  fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
  await act(async () => {
    await Promise.resolve();
  });
  expect(
    screen.getByRole("heading", { name: "项目工作台" }),
  ).toBeInTheDocument();
}

function openNewReplica() {
  fireEvent.click(screen.getByRole("button", { name: "新建复刻" }));
}

function openTaskRecords() {
  fireEvent.click(screen.getByRole("button", { name: "任务记录" }));
}

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
    window.location.hash = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
    window.localStorage.clear();
    window.location.hash = "";
  });

  it("starts on the internal login screen", () => {
    vi.stubGlobal("fetch", withAuth(vi.fn()));

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
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();

    expect(
      screen.getByRole("heading", { name: "项目工作台" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "主导航" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "项目" })).toHaveClass(
      "nav-button--active",
    );
    expect(screen.getByRole("button", { name: "新建复刻" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "人物库" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "任务记录" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "设置" })).toBeNull();
    expect(screen.getByText("林夏")).toBeInTheDocument();
    expect(screen.getByText("普通员工")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("本地服务已连接")).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8000/health", {
      signal: expect.any(AbortSignal),
    });
  });

  it("restores an allowed deep link and renders one shared five-entry shell", async () => {
    window.location.hash = "#characters";
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, adminUser));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(
      await screen.findByRole("heading", { name: "人物库" }),
    ).toBeInTheDocument();
    for (const label of ["项目", "新建复刻", "人物库", "任务记录", "设置"]) {
      expect(screen.getByRole("button", { name: label })).toBeEnabled();
    }
    expect(screen.getByRole("button", { name: "人物库" })).toHaveClass(
      "nav-button--active",
    );
    expect(window.location.hash).toBe("#characters");
  });

  it("falls back from an unauthorized deep link and hides write-only entries", async () => {
    window.location.hash = "#new";
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, auditorUser));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(
      await screen.findByRole("heading", { name: "项目工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建复刻" })).toBeNull();
    expect(screen.queryByRole("button", { name: "设置" })).toBeNull();
    expect(screen.getByRole("button", { name: "人物库" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "任务记录" })).toBeEnabled();
    expect(window.location.hash).toBe("#projects");
  });

  it("keeps the user on login when auth/me rejects the session", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/auth/me")) {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: async () => ({ detail: { message: "missing identity" } }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => healthResponse });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(
      await screen.findByText("身份验证失败：missing identity（401）"),
    ).toBeInTheDocument();
    expect(screen.queryByText("登录已失效，请重新进入工作台。")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "主导航" })).toBeNull();
    expect(fetchMock).not.toHaveBeenCalledWith(
      "http://127.0.0.1:8000/health",
      expect.anything(),
    );
  });

  it("separates the employee project list from reference-video creation", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();

    expect(
      await screen.findByRole("heading", { name: "项目列表" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("项目名称")).toBeNull();
    expect(
      screen.queryByText(
        "当前为只读身份，可查看项目和任务记录，不能创建、上传、编辑、删除或重试。",
      ),
    ).toBeNull();

    openNewReplica();

    expect(
      await screen.findByRole("heading", { name: "新建复刻项目" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toBeInTheDocument();
    expect(screen.getByLabelText("参考视频")).toHaveAttribute(
      "accept",
      ".mp4,.mov,video/mp4,video/quicktime",
    );
    expect(screen.getByRole("button", { name: "创建并上传" })).toBeDisabled();
  });

  it("shows the shared ten-step project flow when creating a replica", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openNewReplica();

    const flow = screen.getByRole("list", { name: "复刻项目流程" });
    expect(within(flow).getAllByRole("listitem")).toHaveLength(10);
    expect(within(flow).getByText("上传参考视频")).toHaveAttribute(
      "aria-current",
      "step",
    );
  });

  it("shows an auditor a read-only project list without write actions", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/analysis/latest")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            id: "analysis-1",
            project_id: "project-ready",
            asset_id: "asset-ready",
            kind: "analysis",
            version_number: 1,
            payload: {
              analysis: {
                summary: "只读拆解",
                duration_seconds: 8,
                shots: [
                  {
                    shot_id: "S01",
                    start_time: 0,
                    end_time: 8,
                    shot_type: "近景",
                    composition: "人物居中",
                    camera_motion: "固定",
                    subject: "主讲人",
                    action: "讲话",
                    scene: "室内",
                    spoken_text: "你好",
                    transition: "硬切",
                  },
                ],
              },
            },
            created_by_user_id: "employee_1",
            created_at: "2030-01-01T00:00:00Z",
          }),
        });
      }
      if (url.endsWith("/shot-cards/latest")) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (
        url.endsWith("/source-frames/latest") ||
        url.endsWith("/source-frames/selection/latest") ||
        url.endsWith("/first-frames/latest") ||
        url.endsWith("/first-frames/selection/latest")
      ) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.endsWith("/first-frames/history")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      return Promise.resolve({
        ok: true,
        json: async () => [
          {
            id: "project-ready",
            owner_user_id: "employee_1",
            name: "已归档项目",
            status: "REFERENCE_READY",
            reference_asset_id: "asset-ready",
            reference_upload_status: "READY",
            analysis_status: "PENDING",
          },
          {
            id: "project-pending",
            owner_user_id: "employee_1",
            name: "上传中项目",
            status: "ACTIVE",
            reference_asset_id: "asset-pending",
            reference_upload_status: "UPLOAD_PENDING",
          },
        ],
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, auditorUser));

    render(<App />);
    await enterWorkspace();

    expect(screen.getAllByText("审计员")).toHaveLength(2);
    expect(
      screen.getByText(
        "当前为只读身份，可查看项目和任务记录，不能创建、上传、编辑、删除或重试。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建并上传" })).toBeNull();
    expect(screen.queryByRole("button", { name: "继续编辑" })).toBeNull();
    expect(screen.queryByRole("button", { name: "删除项目" })).toBeNull();
    expect(screen.queryByRole("button", { name: "继续编辑" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "查看项目" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看项目" }));
    expect(
      await screen.findByText(
        "当前为只读身份，可查看拆解、候选记录和历史版本；不会请求素材下载链接，也不能保存、选择或生成。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("S01 动作")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存镜头卡片" })).toBeNull();
    expect(screen.queryByRole("button", { name: "选择人物" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "重新生成候选首帧" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "确认用于 H3 的首帧" }),
    ).toBeDisabled();
  });

  it("returns to login and clears the current user when a local API returns 401", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: async () => ({ detail: { message: "session expired" } }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));

    expect(
      await screen.findByText("登录已失效，请重新进入工作台。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "短视频复刻工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("林夏")).toBeNull();
  });

  it("lets an employee resume a pending upload from an existing project", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({
        ok: true,
        json: async () => [
          {
            id: "project-pending",
            owner_user_id: "employee_1",
            name: "待续传项目",
            status: "ACTIVE",
            reference_asset_id: "asset-pending",
            reference_upload_status: "UPLOAD_PENDING",
          },
        ],
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));

    expect(window.location.hash).toBe("#new");
    expect(
      screen.getByRole("heading", { name: "新建复刻项目" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toHaveValue("待续传项目");
    expect(screen.getByLabelText("项目名称")).toBeDisabled();
    expect(
      screen.getByText("正在为“待续传项目”重新上传参考视频。"),
    ).toBeInTheDocument();
  });

  it("resumes a ready project at video analysis without creating another project", async () => {
    let analysisReady = false;
    const analysis = {
      id: "analysis-recovered",
      project_id: "project-ready",
      asset_id: "asset-ready",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "恢复后的拆解结果",
          duration_seconds: 8,
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 8,
              shot_type: "近景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "讲话",
              scene: "室内",
              spoken_text: "你好",
              transition: "硬切",
            },
          ],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    };
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: "project-ready",
              owner_user_id: "employee_1",
              name: "待拆解项目",
              status: "REFERENCE_READY",
              reference_asset_id: "asset-ready",
              reference_upload_status: "READY",
              analysis_status: "PENDING",
            },
          ],
        });
      }
      if (url.endsWith("/analysis/latest")) {
        return analysisReady
          ? Promise.resolve({ ok: true, json: async () => analysis })
          : Promise.resolve({
              ok: false,
              status: 404,
              json: async () => ({
                detail: { message: "Project has no analysis version." },
              }),
            });
      }
      if (url.endsWith("/analysis") && options?.method === "POST") {
        analysisReady = true;
        return Promise.resolve({ ok: true, json: async () => analysis });
      }
      if (url.endsWith("/shot-cards/latest")) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (
        url.endsWith("/source-frames/latest") ||
        url.endsWith("/source-frames/selection/latest") ||
        url.endsWith("/first-frames/latest") ||
        url.endsWith("/first-frames/selection/latest")
      ) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.endsWith("/first-frames/history")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));

    expect(
      await screen.findByRole("button", { name: "开始视频拆解" }),
    ).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "开始视频拆解" }),
    );

    expect(await screen.findByText("恢复后的拆解结果")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-ready/analysis",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ asset_id: "asset-ready" }),
      }),
    );
    expect(
      fetchMock.mock.calls.filter(
        ([url, options]) =>
          url.endsWith("/api/projects") && options?.method === "POST",
      ),
    ).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "返回项目" }));
    expect(await screen.findByText("视频拆解已完成")).toBeInTheDocument();
  });

  it("lets an employee delete an unfinished project after confirmation", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects/project-pending")) {
        return Promise.resolve({ ok: true, status: 204 });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: "project-pending",
              owner_user_id: "employee_1",
              name: "等待删除",
              status: "ACTIVE",
              reference_asset_id: "asset-pending",
              reference_upload_status: "UPLOAD_PENDING",
            },
          ],
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );

    render(<App />);
    await enterWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "删除项目" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-pending",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(
      await screen.findByText("项目“等待删除”已删除。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("等待删除")).toBeNull();
  });

  it("lets an employee edit and save the latest analysis shot cards", async () => {
    const analysis = {
      id: "analysis-1",
      project_id: "project-ready",
      asset_id: "asset-ready",
      kind: "analysis",
      version_number: 1,
      payload: {
        provider_response_ref: {
          raw: { provider: "fake_gemini" },
        },
        analysis: {
          summary: "咖啡口播拆解",
          duration_seconds: 8,
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 8,
              shot_type: "近景",
              composition: "人物居中",
              camera_motion: "轻微推进",
              subject: "主讲人",
              action: "看向镜头讲话",
              scene: "咖啡店",
              spoken_text: "一杯好咖啡",
              transition: "硬切",
            },
          ],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    };
    const fetchMock = vi.fn((...args: [url: string, options?: RequestInit]) => {
      const [url] = args;
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: "project-ready",
              owner_user_id: "employee_1",
              name: "咖啡复刻",
              status: "REFERENCE_READY",
              reference_asset_id: "asset-ready",
              reference_upload_status: "READY",
            },
          ],
        });
      }
      if (url.endsWith("/analysis/latest")) {
        return Promise.resolve({ ok: true, json: async () => analysis });
      }
      if (url.endsWith("/shot-cards/latest")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ...analysis,
            id: "shot-card-1",
            kind: "shot_card",
            payload: {
              source_analysis_version_id: "analysis-1",
              duration_seconds: 8,
              shots: [
                {
                  ...analysis.payload.analysis.shots[0],
                  action: "已保存的动作",
                },
              ],
            },
          }),
        });
      }
      if (url.endsWith("/analysis/analysis-1/shots")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ...analysis,
            id: "shot-card-1",
            kind: "shot_card",
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));

    expect(
      screen.getByRole("heading", { name: "咖啡复刻" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "镜头卡片" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("咖啡口播拆解")).toBeInTheDocument();
    expect(
      screen.getByText("拆解来源：内置模拟拆解（尚未调用 Gemini）"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "当前显示的是内置模拟结果。请在设置中保存 Gemini 视频分析 API Key，并配置可用的 COS 或 OSS 存储后重新拆解。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("S01 动作")).toHaveValue("已保存的动作");
    fireEvent.change(screen.getByLabelText("S01 动作"), {
      target: { value: "端起咖啡杯" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存镜头卡片" }));

    expect(
      await screen.findByText("镜头卡片已保存为版本 #1。"),
    ).toBeInTheDocument();
    const saveCall = fetchMock.mock.calls.find(
      ([url]) => url === "http://127.0.0.1:8000/api/analysis/analysis-1/shots",
    );
    expect(saveCall).toBeDefined();
    if (!saveCall) {
      throw new Error("镜头卡片保存请求未发出。");
    }
    expect(saveCall[1]).toEqual(expect.objectContaining({ method: "PUT" }));
    expect(JSON.parse(String((saveCall[1] as RequestInit).body))).toMatchObject(
      {
        shots: [
          expect.objectContaining({ shot_id: "S01", action: "端起咖啡杯" }),
        ],
      },
    );
  });

  it("lets an employee choose an authorized main character for the project", async () => {
    const analysis = {
      id: "analysis-1",
      project_id: "project-ready",
      asset_id: "asset-ready",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "人物选择测试",
          duration_seconds: 8,
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 8,
              shot_type: "近景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "讲话",
              scene: "室内",
              spoken_text: "你好",
              transition: "硬切",
            },
          ],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    };
    const fetchMock = vi.fn((...args: [url: string, options?: RequestInit]) => {
      const [url, options] = args;
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: "project-ready",
              owner_user_id: "employee_1",
              name: "人物复刻",
              status: "REFERENCE_READY",
              reference_asset_id: "asset-ready",
              reference_upload_status: "READY",
            },
          ],
        });
      }
      if (url.endsWith("/analysis/latest")) {
        return Promise.resolve({ ok: true, json: async () => analysis });
      }
      if (url.endsWith("/shot-cards/latest")) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.includes("/api/characters?project_id=project-ready")) {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: "character-1",
              name: "小夏",
              reference_asset_ids: ["ref-1"],
              authorization_project_ids: ["project-ready"],
              authorization_expires_at: null,
              is_active: true,
              created_by_user_id: "admin_1",
              created_at: "2030-01-01T00:00:00Z",
              updated_at: "2030-01-01T00:00:00Z",
            },
          ],
        });
      }
      if (url.endsWith("/main-character") && options?.method === "GET") {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.endsWith("/main-character") && options?.method === "PUT") {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            project_id: "project-ready",
            character_id: "character-1",
            version_id: "main-character-1",
            version_number: 1,
            character_snapshot: { name: "小夏" },
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));
    fireEvent.click(await screen.findByRole("button", { name: "选择人物" }));
    fireEvent.click(await screen.findByRole("radio", { name: /小夏/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认使用人物" }));

    expect(await screen.findByText("已选择人物“小夏”。")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-ready/main-character",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ character_id: "character-1" }),
      }),
    );
  });

  it("uploads a valid reference video, completes precheck, and starts analysis", async () => {
    class SuccessfulUploadRequest {
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 200;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      open() {}
      setRequestHeader() {}
      send() {
        this.upload.onprogress?.({
          lengthComputable: true,
          loaded: 10,
          total: 10,
        } as ProgressEvent);
        this.onload?.();
      }
    }

    let projectCollectionCalls = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects") && !url.includes("analysis")) {
        projectCollectionCalls += 1;
        const projectResponse =
          projectCollectionCalls === 1
            ? []
            : {
                id: "project-1",
                owner_user_id: "employee_1",
                name: "咖啡口播",
                status: "ACTIVE",
              };
        return Promise.resolve({
          ok: true,
          json: async () => projectResponse,
        });
      }
      if (url.endsWith("/upload-intent")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            asset_id: "asset-1",
            project_id: "project-1",
            storage_key: "projects/project-1/reference.mp4",
            method: "PUT",
            url: "https://storage.example/upload",
            headers: { "content-type": "video/mp4" },
            expires_at: "2030-01-01T00:00:00Z",
          }),
        });
      }
      if (url.endsWith("/complete")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            asset_id: "asset-1",
            project_id: "project-1",
            status: "uploaded",
            storage_uri: "cos://private-bucket/reference.mp4",
            sha256: "hash",
            size_bytes: 10,
            content_type: "video/mp4",
            metadata: { duration_seconds: 8 },
          }),
        });
      }
      if (url.endsWith("/shot-cards/latest")) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (
        url.endsWith("/source-frames/latest") ||
        url.endsWith("/source-frames/selection/latest") ||
        url.endsWith("/first-frames/latest") ||
        url.endsWith("/first-frames/selection/latest")
      ) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.endsWith("/first-frames/history")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({
          id: "analysis-1",
          project_id: "project-1",
          asset_id: "asset-1",
          kind: "analysis",
          version_number: 1,
          payload: {
            analysis: {
              summary: "咖啡口播拆解完成",
              duration_seconds: 8,
              shots: [
                {
                  shot_id: "S01",
                  start_time: 0,
                  end_time: 8,
                  shot_type: "近景",
                  composition: "人物居中",
                  camera_motion: "固定",
                  subject: "主讲人",
                  action: "讲话",
                  scene: "咖啡店",
                  spoken_text: "你好",
                  transition: "硬切",
                },
              ],
            },
          },
          created_by_user_id: "employee_1",
          created_at: "2030-01-01T00:00:00Z",
        }),
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    vi.stubGlobal("XMLHttpRequest", SuccessfulUploadRequest);

    render(<App />);
    await enterWorkspace();
    openNewReplica();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "咖啡口播" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传" }));

    expect(
      await screen.findByText(/已完成上传和预检（8.0 秒），已自动进入视频拆解/),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "镜头卡片" }),
    ).toBeInTheDocument();
    expect(screen.getByText("咖啡口播拆解完成")).toBeInTheDocument();
    expect(
      screen.queryByRole("progressbar", { name: "参考视频上传进度" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "继续上传" })).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/analysis",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          asset_id: "asset-1",
          duration_seconds: 8,
          reuse_existing: true,
        }),
      }),
    );
  });

  it("clears the automatic-analysis error after recovery succeeds", async () => {
    class SuccessfulUploadRequest {
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 200;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      open() {}
      setRequestHeader() {}
      send() {
        this.onload?.();
      }
    }

    const analysis = {
      id: "analysis-recovered",
      project_id: "project-recovery",
      asset_id: "asset-recovery",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "恢复成功",
          duration_seconds: 8,
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 8,
              shot_type: "近景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "讲话",
              scene: "室内",
              spoken_text: "你好",
              transition: "硬切",
            },
          ],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    };
    let analysisPostCalls = 0;
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects") && !url.includes("analysis")) {
        return Promise.resolve({
          ok: true,
          json: async () =>
            options?.method === "POST"
              ? {
                  id: "project-recovery",
                  owner_user_id: "employee_1",
                  name: "失败后恢复",
                  status: "ACTIVE",
                  reference_asset_id: null,
                  reference_upload_status: "NOT_STARTED",
                  analysis_status: "NOT_READY",
                }
              : [],
        });
      }
      if (url.endsWith("/upload-intent")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            asset_id: "asset-recovery",
            project_id: "project-recovery",
            storage_key: "projects/project-recovery/reference.mp4",
            method: "PUT",
            url: "https://storage.example/upload",
            headers: { "content-type": "video/mp4" },
            expires_at: "2030-01-01T00:00:00Z",
          }),
        });
      }
      if (url.endsWith("/complete")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            asset_id: "asset-recovery",
            project_id: "project-recovery",
            status: "uploaded",
            storage_uri: "cos://private-bucket/reference.mp4",
            sha256: "hash",
            size_bytes: 10,
            content_type: "video/mp4",
            metadata: { duration_seconds: 8 },
          }),
        });
      }
      if (url.endsWith("/analysis") && options?.method === "POST") {
        analysisPostCalls += 1;
        return analysisPostCalls === 1
          ? Promise.resolve({
              ok: false,
              status: 502,
              json: async () => ({
                detail: { message: "模型暂时不可用" },
              }),
            })
          : Promise.resolve({ ok: true, json: async () => analysis });
      }
      if (url.endsWith("/analysis/latest")) {
        return Promise.resolve({ ok: true, json: async () => analysis });
      }
      if (url.endsWith("/shot-cards/latest")) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (
        url.endsWith("/source-frames/latest") ||
        url.endsWith("/source-frames/selection/latest") ||
        url.endsWith("/first-frames/latest") ||
        url.endsWith("/first-frames/selection/latest")
      ) {
        return Promise.resolve({ ok: false, status: 404 });
      }
      if (url.endsWith("/first-frames/history")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    vi.stubGlobal("XMLHttpRequest", SuccessfulUploadRequest);

    render(<App />);
    await enterWorkspace();
    openNewReplica();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "失败后恢复" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传" }));

    expect(await screen.findByText(/模型暂时不可用/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始视频拆解" }));

    expect(await screen.findByText("恢复成功")).toBeInTheDocument();
    expect(screen.queryByText(/模型暂时不可用/)).toBeNull();
  });

  it("rejects unsupported reference videos before creating a project", async () => {
    vi.stubGlobal("fetch", withAuth(vi.fn()));

    render(<App />);
    await enterWorkspace();
    openNewReplica();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "错误文件" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: { files: [new File(["text"], "reference.txt")] },
    });

    expect(
      screen.getByText("只支持 MP4 或 MOV 格式的视频。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建并上传" })).toBeDisabled();
  });

  it("keeps the upload stage visible while the file transfer is still pending", async () => {
    class PendingUploadRequest {
      onabort: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 0;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      open() {}
      setRequestHeader() {}
      send() {}
      abort() {
        this.onabort?.();
      }
    }

    vi.stubGlobal(
      "fetch",
      withAuth((url: string, options?: RequestInit) => {
        if (url.endsWith("/health")) {
          return Promise.resolve({
            ok: true,
            json: async () => healthResponse,
          });
        }
        if (url.endsWith("/api/projects")) {
          return Promise.resolve({
            ok: true,
            json: async () =>
              options?.method === "POST"
                ? {
                    id: "project-1",
                    owner_user_id: "employee_1",
                    name: "上传中项目",
                    status: "ACTIVE",
                    reference_asset_id: null,
                    reference_upload_status: "UPLOAD_PENDING",
                  }
                : [],
          });
        }
        if (url.endsWith("/upload-intent")) {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              asset_id: "asset-1",
              project_id: "project-1",
              storage_key: "projects/project-1/reference.mp4",
              method: "PUT",
              url: "https://storage.example/upload",
              headers: { "content-type": "video/mp4" },
              expires_at: "2030-01-01T00:00:00Z",
            }),
          });
        }
        return Promise.resolve({ ok: true, json: async () => [] });
      }),
    );
    vi.stubGlobal("XMLHttpRequest", PendingUploadRequest);

    render(<App />);
    await enterWorkspace();
    openNewReplica();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "上传中项目" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传" }));

    expect(
      await screen.findByText("步骤 3/5 · 正在上传参考视频"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "参考视频上传进度" }),
    ).toHaveAttribute("aria-valuetext", "正在上传参考视频，等待传输进度");
    fireEvent.click(screen.getByRole("button", { name: "取消上传" }));
    expect(await screen.findByText(/上传已取消/)).toBeInTheDocument();
  });

  it("aborts an in-flight upload when the desktop page is unloaded", async () => {
    let abortCalls = 0;
    class PendingUploadRequest {
      onabort: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 0;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      open() {}
      setRequestHeader() {}
      send() {}
      abort() {
        abortCalls += 1;
        this.onabort?.();
      }
    }

    vi.stubGlobal(
      "fetch",
      withAuth((url: string, options?: RequestInit) => {
        if (url.endsWith("/health")) {
          return Promise.resolve({
            ok: true,
            json: async () => healthResponse,
          });
        }
        if (url.endsWith("/api/projects")) {
          return Promise.resolve({
            ok: true,
            json: async () =>
              options?.method === "POST"
                ? {
                    id: "project-unload",
                    owner_user_id: "employee_1",
                    name: "关闭窗口测试",
                    status: "ACTIVE",
                    reference_asset_id: null,
                    reference_upload_status: "UPLOAD_PENDING",
                    analysis_status: "NOT_READY",
                  }
                : [],
          });
        }
        if (url.endsWith("/upload-intent")) {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              asset_id: "asset-unload",
              project_id: "project-unload",
              storage_key: "projects/project-unload/reference.mp4",
              method: "PUT",
              url: "https://storage.example/upload",
              headers: { "content-type": "video/mp4" },
              expires_at: "2030-01-01T00:00:00Z",
            }),
          });
        }
        return Promise.resolve({ ok: true, json: async () => [] });
      }),
    );
    vi.stubGlobal("XMLHttpRequest", PendingUploadRequest);

    render(<App />);
    await enterWorkspace();
    openNewReplica();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "关闭窗口测试" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传" }));
    await screen.findByText("步骤 3/5 · 正在上传参考视频");

    act(() => window.dispatchEvent(new Event("pagehide")));

    await waitFor(() => expect(abortCalls).toBe(1));
    expect(await screen.findByText(/上传已取消/)).toBeInTheDocument();
  });

  it("keeps the created project available for an upload retry", async () => {
    class FailedUploadRequest {
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 500;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      open() {}
      setRequestHeader() {}
      send() {
        this.onload?.();
      }
    }

    let projectCollectionCalls = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        projectCollectionCalls += 1;
        const projectResponse =
          projectCollectionCalls === 1
            ? []
            : {
                id: "project-1",
                owner_user_id: "employee_1",
                name: "失败重试",
                status: "ACTIVE",
              };
        return Promise.resolve({
          ok: true,
          json: async () => projectResponse,
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({
          asset_id: "asset-1",
          project_id: "project-1",
          storage_key: "projects/project-1/reference.mp4",
          method: "PUT",
          url: "https://storage.example/upload",
          headers: { "content-type": "video/mp4" },
          expires_at: "2030-01-01T00:00:00Z",
        }),
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    vi.stubGlobal("XMLHttpRequest", FailedUploadRequest);

    render(<App />);
    await enterWorkspace();
    openNewReplica();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "失败重试" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传" }));

    expect(
      await screen.findByText(/上传参考视频失败（500）/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新上传" })).toBeEnabled();
    expect(screen.getByLabelText("项目名称")).toBeDisabled();
  });

  it("lets an admin configure providers, run a non-generative diagnostic, and download its log", async () => {
    const settingsResponse = {
      providers: {
        metaso: {
          provider: "metaso",
          configured: true,
          config: {},
        },
        apilio: { provider: "apilio", configured: false, config: {} },
        cos: { provider: "cos", configured: false, config: {} },
        oss: { provider: "oss", configured: false, config: {} },
      },
      runtime: {
        max_generation_count_per_batch: 4,
        max_concurrent_h3_tasks: 2,
        active_storage_provider: "cos",
      },
    };
    const diagnosticResponse = {
      id: "diagnostic-1",
      status: "attention",
      providers: [
        {
          provider: "metaso",
          status: "configured_only",
          configured_fields: ["api_key"],
          adapter_capability: "configuration_only",
          test_kind: "connection",
          http_status: null,
          error_code: null,
          latency_ms: 1,
          message: "参数已保存；真实服务适配器尚未启用，因此未发起外部调用。",
        },
      ],
      download_url:
        "/api/admin/settings/diagnostic-reports/diagnostic-1/download",
    };
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/admin/settings")) {
        return Promise.resolve({
          ok: true,
          json: async () => settingsResponse,
        });
      }
      if (url.endsWith("/diagnostic-test") && options?.method === "POST") {
        return Promise.resolve({
          ok: true,
          json: async () => diagnosticResponse,
        });
      }
      if (url.endsWith("/diagnostic-reports/diagnostic-1/download")) {
        return Promise.resolve({
          ok: true,
          blob: async () => new Blob(["{}"]),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, adminUser));
    const createObjectUrl = vi.fn(() => "blob:diagnostic-1");
    const revokeObjectUrl = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    vi.stubGlobal("URL", {
      createObjectURL: createObjectUrl,
      revokeObjectURL: revokeObjectUrl,
    });

    render(<App />);
    await enterWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "设置" }));

    expect(
      await screen.findByRole("heading", { name: "服务设置" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("H3 API Key")).toHaveValue("");
    expect(screen.getByLabelText("H3 API Key")).toHaveAttribute(
      "placeholder",
      "已保存，留空不修改",
    );
    expect(screen.getByText("模型服务（Apilio）")).toBeInTheDocument();
    expect(screen.getByText("当前已保存：腾讯云 COS")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("当前对象存储"), {
      target: { value: "oss" },
    });
    expect(
      screen.getByText("尚未保存：将切换为阿里云 OSS"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /可能产生云存储请求费用；该操作不会提交 H3、视频或图片生成任务/,
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "测试设置" }));

    expect(
      await screen.findByText("检测到需要处理的配置项"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载诊断日志" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载诊断日志" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8000/api/admin/settings/diagnostic-reports/diagnostic-1/download",
        expect.objectContaining({ method: "GET" }),
      ),
    );
    expect(createObjectUrl).toHaveBeenCalledOnce();
    await waitFor(
      () => expect(revokeObjectUrl).toHaveBeenCalledWith("blob:diagnostic-1"),
      { timeout: 2_000 },
    );
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
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openTaskRecords();
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
    let generationRequestCount = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      generationRequestCount += 1;
      return Promise.resolve({
        ok: true,
        json: async () =>
          generationRequestCount === 1 ? runningBatch : doneBatch,
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openTaskRecords();
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

    expect(generationRequestCount).toBe(2);
  });

  it("stops polling when the batch needs manual handling", async () => {
    vi.useFakeTimers();
    const attentionBatch = batchResponse({ status: "NEEDS_ATTENTION" });
    let generationRequestCount = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      generationRequestCount += 1;
      return Promise.resolve({ ok: true, json: async () => attentionBatch });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openTaskRecords();
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

    expect(generationRequestCount).toBe(1);
  });

  it("backs off after poll failures while keeping the last running batch visible", async () => {
    vi.useFakeTimers();
    let generationRequestCount = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      generationRequestCount += 1;
      if (generationRequestCount === 2) {
        return Promise.reject(new Error("network"));
      }
      return Promise.resolve({
        ok: true,
        json: async () =>
          generationRequestCount === 1
            ? batchResponse({
                progress: {
                  ...batchResponse().progress,
                  terminal_count: 0,
                  progress_percent: 0,
                },
              })
            : batchResponse(),
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openTaskRecords();
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
    expect(generationRequestCount).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(generationRequestCount).toBe(3);
  });

  it("stops polling and clears storage when the batch returns 404", async () => {
    vi.useFakeTimers();
    let generationRequestCount = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      generationRequestCount += 1;
      return Promise.resolve({
        ok: false,
        status: 404,
        json: async () => ({ detail: { code: "BATCH_NOT_FOUND" } }),
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openTaskRecords();
    fireEvent.change(screen.getByLabelText("Batch ID"), {
      target: { value: "batch-missing" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    await act(async () => {
      await Promise.resolve();
    });

    expect(
      screen.getByText("该任务记录不存在，已停止自动刷新。"),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("generation.batchId")).toBeNull();
    expect(generationRequestCount).toBe(1);
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
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    openTaskRecords();

    expect(
      await screen.findByDisplayValue("batch-restored"),
    ).toBeInTheDocument();
    expect(await screen.findByText("batch-restored")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/generation-batches/batch-restored",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });
});
