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
  fireEvent.click(screen.getByRole("button", { name: "进入" }));
  await act(async () => {
    await Promise.resolve();
  });
  expect(
    screen.getByRole("heading", { level: 1, name: "项目" }),
  ).toBeInTheDocument();
}

function openTaskRecords() {
  fireEvent.click(screen.getByRole("button", { name: "任务记录" }));
}

function isBatchHistoryRequest(url: string) {
  return url.includes("/api/generation-batches?");
}

function emptyBatchHistory() {
  return { items: [], next_cursor: null };
}

function batchResponse(overrides = {}) {
  return {
    id: "batch-1",
    project_id: "project-1",
    prompt_version_id: "prompt-1",
    status: "RUNNING",
    quantity: 2,
    stale: false,
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
      screen.getByRole("heading", { name: "镜序 Studio" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "进入" })).toBeInTheDocument();
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
      screen.getByRole("heading", { level: 1, name: "项目" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "主导航" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "项目" })).toHaveClass(
      "nav-button--active",
    );
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

  it("restores an allowed deep link and renders one shared four-entry shell", async () => {
    window.location.hash = "#characters";
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, adminUser));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入" }));

    expect(
      await screen.findByRole("heading", { name: "人物库" }),
    ).toBeInTheDocument();
    for (const label of ["项目", "人物库", "任务记录", "设置"]) {
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
    fireEvent.click(screen.getByRole("button", { name: "进入" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "项目" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("项目名称")).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "进入" }));

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

  it("shows the employee project list with an inline creation form", async () => {
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
      await screen.findByText("还没有项目。在上方开始第一个复刻。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "项目" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toBeInTheDocument();
    expect(screen.getByLabelText("参考视频")).toHaveAttribute(
      "accept",
      ".mp4,.mov,video/mp4,video/quicktime",
    );
    expect(screen.getByRole("button", { name: "开始" })).toBeDisabled();
    expect(screen.queryByText("只读身份：仅可查看。")).toBeNull();
  });

  it("shows the shared three-stage project flow when creating a replica", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({ ok: true, json: async () => [] });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    window.location.hash = "#new";

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "进入" }));

    const flow = await screen.findByRole("list", { name: "复刻项目流程" });
    expect(within(flow).getAllByRole("listitem")).toHaveLength(3);
    expect(within(flow).getByText("上传与拆解")).toHaveAttribute(
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
    expect(screen.getByText("只读身份：仅可查看。")).toBeInTheDocument();
    expect(screen.queryByLabelText("项目名称")).toBeNull();
    expect(screen.queryByRole("button", { name: "继续编辑" })).toBeNull();
    expect(screen.queryByRole("button", { name: "删除项目" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "查看项目" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看项目" }));
    expect(await screen.findByText("只读身份：仅可查看。")).toBeInTheDocument();
    expect(screen.getByLabelText("S01 动作")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存镜头卡片" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "查看角色版本" }),
    ).toBeInTheDocument();
    expect(screen.getByText("先选择角色版本")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新生成候选首帧" }),
    ).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "进入" }));

    expect(
      await screen.findByText("登录已失效，请重新进入工作台。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "镜序 Studio" }),
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
      screen.getByRole("heading", { level: 2, name: "新建项目" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toHaveValue("待续传项目");
    expect(screen.getByLabelText("项目名称")).toBeDisabled();
    expect(screen.getByText("重新上传 · 待续传项目")).toBeInTheDocument();
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
      await screen.findByRole("button", { name: "开始拆解" }),
    ).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "开始拆解" }));

    expect(await screen.findByText("恢复后的拆解结果")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-ready/analysis",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          asset_id: "asset-ready",
          reuse_existing: true,
        }),
      }),
    );
    expect(
      fetchMock.mock.calls.filter(
        ([url, options]) =>
          url.endsWith("/api/projects") && options?.method === "POST",
      ),
    ).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    expect(await screen.findByText("已拆解")).toBeInTheDocument();
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
      await screen.findByRole("heading", { name: "镜头与口播" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("咖啡口播拆解")).toBeInTheDocument();
    expect(screen.getByText("拆解来源：演示拆解")).toBeInTheDocument();
    expect(
      screen.getByText("演示数据 · 在设置中配置 Gemini 后可重新拆解"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("S01 动作")).toHaveValue("已保存的动作");
    fireEvent.change(screen.getByLabelText("S01 动作"), {
      target: { value: "端起咖啡杯" },
    });

    expect(
      await screen.findByText("已自动保存 · 版本 #1", {}, { timeout: 3_000 }),
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
      if (url.endsWith("/character-versions/available")) {
        return Promise.resolve({
          ok: true,
          json: async () => [
            {
              character_version_id: "character-version-3",
              version_number: 3,
              identity_id: "identity-1",
              identity_name: "小夏",
              authorization_expires_at: null,
              persona_id: "persona-1",
              persona_snapshot_json: {
                name: "乡墅项目管理专家",
                occupation: "项目管理",
              },
              provider: "fake_character",
              model: "fake-character-v1",
              template_version: "character-prompt-v1",
              template_hash: "template-hash",
              published_at: "2030-01-01T00:00:00Z",
              publication_hash: "publication-hash",
              assets: [
                "FRONT_FACE",
                "FRONT_HALF",
                "FRONT_FULL",
                "LEFT_45",
                "RIGHT_45",
                "LEFT_SIDE",
                "RIGHT_SIDE",
              ].map((viewType) => ({
                character_asset_id: `character-asset-${viewType}`,
                asset_id: `asset-${viewType}`,
                view_type: viewType,
              })),
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
            character_id: null,
            character_version_id: "character-version-3",
            version_id: "main-character-1",
            version_number: 1,
            character_snapshot: {
              schema_version: "project-character-selection.v1",
              character_version_id: "character-version-3",
              character_version_number: 3,
              identity: {
                id: "identity-1",
                display_name: "小夏",
                authorization_expires_at: null,
              },
              persona_snapshot_json: { name: "乡墅项目管理专家" },
              provider: "fake_character",
              model: "fake-character-v1",
              published_assets: [],
            },
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));
    const chooseVersionButton = await screen.findByRole("button", {
      name: "选择角色版本",
    });
    await waitFor(() => expect(chooseVersionButton).toBeEnabled());
    fireEvent.click(chooseVersionButton);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8000/api/projects/project-ready/character-versions/available",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    fireEvent.click(
      await screen.findByRole("radio", { name: /小夏.*乡墅项目管理专家.*V3/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认角色版本" }));

    expect(
      await screen.findByText("已选择角色“小夏 · 乡墅项目管理专家 V3”。"),
    ).toBeInTheDocument();
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-ready/main-character",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ character_version_id: "character-version-3" }),
      }),
    );
  });

  // 15.05s clears the upload precheck (15s + 0.1s rounding tolerance) but used to be
  // rejected by the analysis request schema, so the automatic start returned a bare 422.
  it("uploads a 15.05s reference video, completes precheck, and starts analysis without echoing the duration", async () => {
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
            metadata: { duration_seconds: 15.05 },
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
              duration_seconds: 15.05,
              shots: [
                {
                  shot_id: "S01",
                  start_time: 0,
                  end_time: 15.05,
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
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "咖啡口播" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));

    expect(
      await screen.findByText(/预检通过（15.1 秒），拆解中/),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "镜头与口播" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("咖啡口播拆解完成")).toBeInTheDocument();
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
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "失败后恢复" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));

    expect(await screen.findByText(/模型暂时不可用/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "开始拆解" }));

    expect(await screen.findByText("恢复成功")).toBeInTheDocument();
    expect(screen.queryByText(/模型暂时不可用/)).toBeNull();
  });

  it("rejects unsupported reference videos before creating a project", async () => {
    vi.stubGlobal("fetch", withAuth(vi.fn()));

    render(<App />);
    await enterWorkspace();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "错误文件" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: { files: [new File(["text"], "reference.txt")] },
    });

    expect(
      screen.getByText("只支持 MP4 或 MOV 格式的视频。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始" })).toBeDisabled();
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
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "上传中项目" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));

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
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "关闭窗口测试" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));
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
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "失败重试" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));

    expect(
      await screen.findByText(/上传参考视频失败（500）/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新上传" })).toBeEnabled();
    expect(screen.getByLabelText("项目名称")).toBeDisabled();
  });

  it("attributes a network-level cloud upload failure to object storage connectivity", async () => {
    class NetworkFailureRequest {
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
      send() {
        this.onerror?.();
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
                name: "云存储不可达",
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
          url: "https://bucket-1250000000.cos.ap-shanghai.myqcloud.com/projects/project-1/reference.mp4?q-signature=abc",
          headers: { "content-type": "video/mp4" },
          expires_at: "2030-01-01T00:00:00Z",
        }),
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    vi.stubGlobal("XMLHttpRequest", NetworkFailureRequest);

    render(<App />);
    await enterWorkspace();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "云存储不可达" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));

    expect(
      await screen.findByText(/上传参考视频失败（无法连接对象存储/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/上传参考视频失败（网络错误）/),
    ).not.toBeInTheDocument();
  });

  it("attributes a network-level local upload failure to the local service", async () => {
    class NetworkFailureRequest {
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
      send() {
        this.onerror?.();
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
                name: "本地服务不可达",
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
          url: "http://127.0.0.1:8000/api/assets/local-objects/projects/project-1/uploads/asset-1/reference.mp4",
          headers: { "content-type": "video/mp4" },
          expires_at: "2030-01-01T00:00:00Z",
        }),
      });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock));
    vi.stubGlobal("XMLHttpRequest", NetworkFailureRequest);

    render(<App />);
    await enterWorkspace();
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "本地服务不可达" },
    });
    fireEvent.change(screen.getByLabelText("参考视频"), {
      target: {
        files: [new File(["video"], "reference.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始" }));

    expect(
      await screen.findByText(
        /上传参考视频失败（无法连接本地服务，请确认服务已启动）/,
      ),
    ).toBeInTheDocument();
  });

  it("warns about bucket CORS rules when the storage provider switches to COS", async () => {
    const settingsResponse = {
      providers: {
        metaso: { provider: "metaso", configured: false, config: {} },
        apilio: { provider: "apilio", configured: false, config: {} },
        cos: { provider: "cos", configured: false, config: {} },
      },
      runtime: {
        max_generation_count_per_batch: 4,
        max_concurrent_h3_tasks: 2,
        active_storage_provider: "local",
      },
    };
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/admin/settings")) {
        return Promise.resolve({
          ok: true,
          json: async () => settingsResponse,
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, adminUser));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "设置" }));

    expect(
      await screen.findByRole("heading", { name: "运行设置" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/跨域访问 CORS/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("存储方式"), {
      target: { value: "cos" },
    });

    expect(screen.getByText(/跨域访问 CORS/)).toBeInTheDocument();
  });

  it("lets an admin configure providers and run per-provider connection tests", async () => {
    const settingsResponse = {
      providers: {
        metaso: {
          provider: "metaso",
          configured: true,
          config: {},
        },
        apilio: { provider: "apilio", configured: false, config: {} },
        cos: { provider: "cos", configured: false, config: {} },
      },
      runtime: {
        max_generation_count_per_batch: 4,
        max_concurrent_h3_tasks: 2,
        active_storage_provider: "local",
      },
    };
    let resolveRuntimeSave: ((response: unknown) => void) | undefined;
    const runtimeSaveResponse = new Promise<unknown>((resolve) => {
      resolveRuntimeSave = resolve;
    });
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
      if (
        url.endsWith("/api/admin/settings/runtime") &&
        options?.method === "PATCH"
      ) {
        return runtimeSaveResponse;
      }
      if (
        url.endsWith("/api/admin/settings/providers/metaso/connection-test") &&
        options?.method === "POST"
      ) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: "configured_only",
            provider: "metaso",
            test_kind: "connection",
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, adminUser));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "设置" }));

    expect(
      await screen.findByRole("heading", { name: "服务设置" }),
    ).toBeInTheDocument();
    const apiKeyInput = screen.getByLabelText("API Key");
    expect(apiKeyInput).toHaveValue("");
    expect(apiKeyInput).toHaveAttribute("placeholder", "已保存，留空不修改");
    expect(apiKeyInput).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    expect(apiKeyInput).toHaveAttribute("type", "text");
    fireEvent.click(screen.getByRole("button", { name: "隐藏API Key" }));
    expect(apiKeyInput).toHaveAttribute("type", "password");
    expect(screen.getByText("模型服务")).toBeInTheDocument();
    expect(screen.queryByLabelText("Region")).not.toBeInTheDocument();
    expect(screen.getByText(/区域固定为上海/)).toBeInTheDocument();
    expect(screen.queryByText("阿里云 OSS")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("存储方式"), {
      target: { value: "cos" },
    });
    expect(screen.getByText("存储方式修改尚未保存")).toBeInTheDocument();
    await act(async () => Promise.resolve());
    expect(screen.getByText("存储方式修改尚未保存")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "保存" })[3]);
    expect(screen.getByLabelText("存储方式")).toBeDisabled();
    expect(screen.getByLabelText("单次生成数量上限")).toBeDisabled();
    expect(screen.getByLabelText("视频生成并发数")).toBeDisabled();
    expect(screen.getByRole("button", { name: "正在保存" })).toBeDisabled();
    await act(async () => {
      resolveRuntimeSave?.({
        ok: true,
        json: async () => ({
          ...settingsResponse.runtime,
          active_storage_provider: "cos",
        }),
      });
      await Promise.resolve();
    });
    expect(await screen.findByText("已保存")).toBeInTheDocument();
    expect(screen.getByLabelText("存储方式")).toBeEnabled();
    expect(
      screen.getByText(/测试连接会创建并删除一个临时对象/),
    ).toBeInTheDocument();

    const testButtons = screen.getAllByRole("button", { name: "测试连接" });
    expect(testButtons).toHaveLength(3);
    fireEvent.click(testButtons[0]);
    expect(
      await screen.findByText("参数已保存；测试不会发起外部调用"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("旧版诊断结果不应在新界面中渲染。"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "测试设置" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "下载诊断日志" }),
    ).not.toBeInTheDocument();
  });

  it("tells an admin that saved settings remain when the local key is unavailable", async () => {
    const message =
      "本地配置仍保存在数据库中，但当前主密钥缺失或不匹配；系统未覆盖已保存配置。";
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      if (url.endsWith("/api/admin/settings")) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({
            detail: {
              code: "SETTINGS_CONFIGURATION_UNAVAILABLE",
              message,
            },
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    vi.stubGlobal("fetch", withAuth(fetchMock, adminUser));

    render(<App />);
    await enterWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "设置" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
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

  it("marks a batch as historical when its frozen inputs are stale", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/health")) {
        return Promise.resolve({ ok: true, json: async () => healthResponse });
      }
      return Promise.resolve({
        ok: true,
        json: async () => batchResponse({ stale: true }),
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

    expect(
      await screen.findByText(
        "该批次的上游版本已更新；结果仍可查看，但不能作为当前版本的交付依据。",
      ),
    ).toBeInTheDocument();
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
      if (isBatchHistoryRequest(url)) {
        return Promise.resolve({
          ok: true,
          json: async () => emptyBatchHistory(),
        });
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
      if (isBatchHistoryRequest(url)) {
        return Promise.resolve({
          ok: true,
          json: async () => emptyBatchHistory(),
        });
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
      if (isBatchHistoryRequest(url)) {
        return Promise.resolve({
          ok: true,
          json: async () => emptyBatchHistory(),
        });
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
      if (isBatchHistoryRequest(url)) {
        return Promise.resolve({
          ok: true,
          json: async () => emptyBatchHistory(),
        });
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
