import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const batch = {
  id: "batch-created-from-project",
  project_id: "project-1",
  prompt_version_id: "prompt-1",
  status: "SUCCEEDED",
  quantity: 1,
  stale: false,
  progress: {
    total_count: 1,
    terminal_count: 1,
    progress_percent: 100,
    counts: { succeeded: 1 },
  },
  tasks: [],
};

function createAppFetchMock() {
  return vi.fn((url: string) => {
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          id: "employee_1",
          username: "employee_1",
          display_name: "林夏",
          role: "employee",
        }),
      });
    }
    if (url.endsWith("/health")) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ status: "ok", service: "video-replica-api" }),
      });
    }
    if (url.endsWith("/api/projects")) {
      return Promise.resolve({
        ok: true,
        json: async () => [
          {
            id: "project-1",
            owner_user_id: "employee_1",
            name: "夏日咖啡馆口播复刻",
            status: "REFERENCE_READY",
            reference_asset_id: "reference-1",
            reference_upload_status: "READY",
            analysis_status: "READY",
          },
        ],
      });
    }
    if (url.endsWith(`/api/generation-batches/${batch.id}`)) {
      return Promise.resolve({ ok: true, json: async () => batch });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  });
}

async function openProjectGenerationFlow() {
  // 启动即自动验证身份进入工作台，无需点击“进入”；从项目列表
  // 行内入口进入被 mock 的工作区。
  await act(async () => Promise.resolve());
  fireEvent.click(
    await screen.findByRole("button", {
      name: "打开项目 夏日咖啡馆口播复刻",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "模拟一键生成完成" }));
}

// V1.4 一键动线交接契约：工作区内部「开始生成 → 四步流水线 →
// onBatchCreated」由 AnalysisWorkspace.test.tsx（P0-04 用例）覆盖；
// 本套件验证 App 层交接段——onBatchCreated 后自动打开任务记录
// （无需粘贴 Batch ID）与 busy 阻断导航，两层接力证明
// 「从点击到任务记录页用户操作仅 1 次」。
vi.mock("./AnalysisWorkspace", () => ({
  AnalysisWorkspace: ({
    onBatchCreated,
    onWorkspaceBusyChange,
  }: {
    onBatchCreated: (value: unknown) => void;
    onWorkspaceBusyChange?: (isBusy: boolean) => void;
  }) => (
    <div>
      <button onClick={() => onBatchCreated(batch)} type="button">
        模拟一键生成完成
      </button>
      <button onClick={() => onWorkspaceBusyChange?.(true)} type="button">
        模拟上游写入开始
      </button>
      <button onClick={() => onWorkspaceBusyChange?.(false)} type="button">
        模拟上游写入完成
      </button>
    </div>
  ),
}));

describe("App generation handoff", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
    window.location.hash = "";
  });

  it("opens the created batch task record without asking for a pasted batch id", async () => {
    window.localStorage.clear();
    window.location.hash = "";
    const fetchMock = createAppFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await openProjectGenerationFlow();

    expect(
      await screen.findByRole("heading", { level: 2, name: "任务记录" }),
    ).toBeInTheDocument();
    expect(screen.getByText(batch.id)).toBeInTheDocument();
    expect(window.location.hash).toBe("#tasks");
    expect(window.localStorage.getItem("generation.batchId")).toBe(batch.id);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://127.0.0.1:8000/api/generation-batches/${batch.id}`,
        expect.any(Object),
      ),
    );
  });

  it("opens the created batch when browser storage is unavailable", async () => {
    window.localStorage.clear();
    window.location.hash = "";
    const fetchMock = createAppFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    const storageWrite = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("storage blocked", "SecurityError");
      });

    render(<App />);
    await openProjectGenerationFlow();

    expect(
      await screen.findByRole("heading", { level: 2, name: "任务记录" }),
    ).toBeInTheDocument();
    expect(screen.getByText(batch.id)).toBeInTheDocument();
    expect(window.location.hash).toBe("#tasks");
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://127.0.0.1:8000/api/generation-batches/${batch.id}`,
        expect.any(Object),
      ),
    );
    storageWrite.mockRestore();
  });

  it("blocks sidebar and browser navigation while the analysis workspace is busy", async () => {
    window.localStorage.clear();
    window.location.hash = "";
    vi.stubGlobal("fetch", createAppFetchMock());

    render(<App />);
    await act(async () => Promise.resolve());
    fireEvent.click(
      await screen.findByRole("button", {
        name: "打开项目 夏日咖啡馆口播复刻",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "模拟上游写入开始" }));

    expect(screen.getByRole("button", { name: "任务记录" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "任务记录" }));
    expect(
      screen.getByRole("button", { name: "模拟上游写入完成" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe("#projects");

    await act(async () => {
      window.history.replaceState(null, "", "#tasks");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(window.location.hash).toBe("#projects");
    expect(
      screen.getByRole("button", { name: "模拟上游写入完成" }),
    ).toBeInTheDocument();

    await act(async () => {
      window.history.replaceState(null, "", "#characters");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(window.location.hash).toBe("#projects");
    expect(
      screen.getByRole("button", { name: "模拟上游写入完成" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "模拟上游写入完成" }));
    expect(screen.getByRole("button", { name: "任务记录" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "任务记录" }));
    expect(
      await screen.findByRole("heading", { level: 2, name: "任务记录" }),
    ).toBeInTheDocument();
  });

  it("allows the completed paid batch handoff to leave a busy analysis workspace", async () => {
    window.localStorage.clear();
    window.location.hash = "";
    vi.stubGlobal("fetch", createAppFetchMock());

    render(<App />);
    await act(async () => Promise.resolve());
    fireEvent.click(
      await screen.findByRole("button", {
        name: "打开项目 夏日咖啡馆口播复刻",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "模拟上游写入开始" }));
    fireEvent.click(screen.getByRole("button", { name: "模拟一键生成完成" }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "任务记录" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe("#tasks");
    expect(screen.getByText(batch.id)).toBeInTheDocument();
  });
});
