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
  fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
  await act(async () => Promise.resolve());
  fireEvent.click(await screen.findByRole("button", { name: "继续编辑" }));
  fireEvent.click(screen.getByRole("button", { name: "模拟从项目创建批次" }));
}

vi.mock("./AnalysisWorkspace", () => ({
  AnalysisWorkspace: ({
    onBatchCreated,
  }: {
    onBatchCreated: (value: unknown) => void;
  }) => (
    <button onClick={() => onBatchCreated(batch)} type="button">
      模拟从项目创建批次
    </button>
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
});
