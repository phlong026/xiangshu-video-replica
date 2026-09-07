import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createReviewData, createReviewState, reviewUser } from "./fixtures";
import { StudioWorkspace } from "./StudioWorkspace";
import { createState } from "./state";

const live = vi.hoisted(() => ({
  loadStudioData: vi.fn(),
  loadViralVideos: vi.fn(async () => ({ videos: [], errors: [] })),
  loadPersonAssets: vi.fn(),
  loadProjectDraft: vi.fn(),
  reloadTasks: vi.fn(async (): Promise<unknown[]> => []),
  reloadStats: vi.fn(async (): Promise<unknown> => null),
  // C7 云端草稿：默认无草稿/空列表，具体用例再覆盖。
  loadCloudDraft: vi.fn(async (): Promise<unknown> => undefined),
  loadDraftMaterials: vi.fn(async () => ({
    assets: [] as unknown[],
    unavailableIds: [] as string[],
  })),
  loadSavedScriptList: vi.fn(async (): Promise<unknown[]> => []),
  loadLatestScriptFromUpload: vi.fn(async (): Promise<unknown> => null),
  persistCloudDraft: vi.fn(async (_draft: unknown): Promise<void> => {}),
  persistSavedScript: vi.fn(
    async (_script: unknown, _sourceProjectId?: string): Promise<void> => {},
  ),
  publishScriptVersion: vi.fn(
    async (_projectId: string, _text: string): Promise<boolean> => true,
  ),
  extractScriptFromUpload: vi.fn(
    async (
      _projectId: string,
      _assetId: string,
    ): Promise<{ text: string }> => ({
      text: "",
    }),
  ),
}));
vi.mock("./live", () => live);

// 视频生成（C2）：只覆盖新引入的四个 api 出口，其余保持原模块行为，
// 避免既有用例（不触发这些函数）受 mock 影响。
const api = vi.hoisted(() => ({
  getIndependentCapabilities: vi.fn(
    async (): Promise<{
      extended_modes_enabled: boolean;
      t2v_enabled: boolean;
      i2v_enabled: boolean;
      r2v_enabled: boolean;
      last_frame_enabled: boolean;
      max_reference_images: number;
      max_quantity: number;
    }> => ({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_quantity: 4,
    }),
  ),
  getGenerationPriceQuote: vi.fn(
    async (): Promise<{
      resolution: string;
      duration_seconds: number;
      quantity: number;
      unit_price_fen_per_second: number;
      estimated_seconds: number;
      estimated_price_fen: number;
    }> => ({
      resolution: "768P",
      duration_seconds: 8,
      quantity: 1,
      unit_price_fen_per_second: 120,
      estimated_seconds: 8,
      estimated_price_fen: 960,
    }),
  ),
  createIndependentVideoTask: vi.fn(),
  listUserSavedPrompts: vi.fn(async (): Promise<unknown[]> => []),
  getLatestScriptRewriteTask: vi.fn(async (): Promise<unknown> => null),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...api,
}));

const livePanel = vi.hoisted(() => ({
  project: {
    id: "project-1",
    owner_user_id: "review-user",
    name: "张工预算项目",
    status: "ACTIVE",
    reference_asset_id: "asset-1",
    reference_upload_status: "READY",
    analysis_status: "READY",
  },
}));
vi.mock("./LiveWorkspacePanel", () => ({
  LiveWorkspacePanel: (props: {
    handoffBatch?: { id: string } | null;
    onBatchCreated: (batch: { id: string }) => void;
    onClose: () => void;
    onHandoffConsumed?: () => void;
    onProjectSelected: (project: typeof livePanel.project) => void;
  }) => (
    <section aria-label="模拟已有功能工作区">
      <button
        type="button"
        onClick={() => props.onProjectSelected(livePanel.project)}
      >
        选择测试项目
      </button>
      <button
        type="button"
        onClick={() => props.onBatchCreated({ id: "batch-1" })}
      >
        创建测试批次
      </button>
      {props.handoffBatch ? (
        <span>存在交接批次</span>
      ) : (
        <span>没有交接批次</span>
      )}
      {props.onHandoffConsumed ? (
        <button type="button" onClick={props.onHandoffConsumed}>
          消费交接批次
        </button>
      ) : null}
      <button type="button" onClick={props.onClose}>
        返回新工作台
      </button>
    </section>
  ),
}));

describe("V1.4 workspace integration", () => {
  it("保持共享壳层尺寸稳定，避免路由切换时 Logo 和标题跳动", () => {
    const { container } = render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const shell = container.querySelector(".studio-shell");
    const main = container.querySelector(".studio-main");
    const sidebar = container.querySelector(".studio-sidebar");

    expect(shell?.className).not.toContain("studio-route-");
    expect(main).toHaveClass("studio-route-workbench");
    expect(sidebar?.closest("[class*='studio-route-']")).toBeNull();

    fireEvent.click(
      within(screen.getByRole("navigation", { name: "主要导航" })).getByRole(
        "button",
        { name: /任务中心/ },
      ),
    );

    expect(shell?.className).not.toContain("studio-route-");
    expect(main).toHaveClass("studio-route-tasks");
    expect(sidebar?.closest("[class*='studio-route-']")).toBeNull();
  });

  it("左下角与右上角使用同一个账号头像", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );

    const accountAvatar = screen
      .getByRole("button", { name: "用户档案，积分 2680" })
      .querySelector("img");
    const topAvatar = screen
      .getByRole("button", { name: "用户档案" })
      .querySelector("img");

    expect(accountAvatar).not.toBeNull();
    expect(accountAvatar?.getAttribute("src")).toBe(
      topAvatar?.getAttribute("src"),
    );
  });

  it("publishing accounts stay in account settings, not the publishing editor", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("profile")}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "发布账号" }));
    expect(
      screen.getByRole("heading", { name: "发布账号管理" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "用户档案" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "发布管理" }),
    ).not.toBeInTheDocument();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    live.loadPersonAssets.mockResolvedValue({ assets: [], errors: [] });
    window.history.replaceState(null, "", "/#studio/workbench");
  });
  it("renders the approved navigation order and keeps review data isolated", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const nav = within(screen.getByRole("navigation", { name: "主要导航" }));
    expect(nav.getAllByRole("button").map((node) => node.textContent)).toEqual(
      expect.arrayContaining(["爆款视频", "文案工坊", "视频创作"]),
    );
    expect(screen.getByText("粘贴一条爆款乡墅视频链接，")).toBeInTheDocument();
    expect(screen.getByText("快速生成它的原创视频")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "用户档案，积分 2680" }),
    ).toBeInTheDocument();
    expect(live.loadStudioData).not.toHaveBeenCalled();
    fireEvent.click(nav.getByRole("button", { name: "文案工坊" }));
    fireEvent.click(screen.getByRole("button", { name: "用于数字人口播" }));
    expect(screen.getByText("张工本人音色 V1")).toBeInTheDocument();
    expect(screen.getByText(/去文案工坊修改/)).toBeInTheDocument();
  });
  it("changing IP invalidates the previous person's voice and avatar", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更换 IP" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: /李总/ }),
    );
    expect(screen.queryByText("张工本人音色 V1")).not.toBeInTheDocument();
    expect(screen.getByText("待确认 V3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成口播视频" })).toBeDisabled();
  });
  it("cancelling a picker leaves the original draft intact", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更换 IP" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "关闭" }),
    );
    expect(screen.getByText("张工本人音色 V1")).toBeInTheDocument();
  });
  it("loads real data without falling back to review examples", async () => {
    live.loadStudioData.mockResolvedValue({
      people: [],
      assets: [],
      videos: [],
      tasks: [],
      projects: [],
      errors: ["人物库暂不可用"],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    });
    render(<StudioWorkspace currentUser={reviewUser} />);
    await waitFor(() => expect(live.loadStudioData).toHaveBeenCalled());
    expect(screen.getByText(/人物库暂不可用/)).toBeInTheDocument();
    expect(screen.queryByText("张工")).not.toBeInTheDocument();
    expect(screen.queryByText(/示例审核/)).not.toBeInTheDocument();
  });

  it("关闭已有项目工作区不会再次导入并覆盖当前草稿", async () => {
    const imported = createReviewState("workbench").draft;
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    live.loadProjectDraft.mockResolvedValue({ draft: imported, errors: [] });
    render(<StudioWorkspace currentUser={reviewUser} />);

    // 工作台“上传视频”已是图标化的本机文件上传；打开旧项目面板的入口
    // 是无来源时的“开始复刻”。
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.click(screen.getByRole("button", { name: "选择测试项目" }));
    await waitFor(() => expect(live.loadProjectDraft).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "返回新工作台" }));
    await waitFor(() => expect(live.loadProjectDraft).toHaveBeenCalledTimes(1));
  });

  it("workbench metric cards show real platform stats", async () => {
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
      stats: {
        today_completed: 5,
        running: 2,
        queued: 1,
        needs_attention: 4,
        total_completed: 42,
      },
    });
    render(<StudioWorkspace currentUser={reviewUser} />);

    await waitFor(() => expect(screen.getByText("5")).toBeInTheDocument());
    // 队列 = running + queued（3）；待处理来自统计而非 20 条切片。
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("消费任务交接后清除暂存批次", async () => {
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    render(<StudioWorkspace currentUser={reviewUser} />);

    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.click(screen.getByRole("button", { name: "创建测试批次" }));
    expect(screen.getByText("存在交接批次")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "消费交接批次" }));
    expect(screen.getByText("没有交接批次")).toBeInTheDocument();
  });

  it("polls generation task progress silently while the workspace is open", async () => {
    vi.useFakeTimers();
    const runningTask = {
      id: "batch-9",
      batchId: "batch-9",
      title: "乡墅批次一",
      type: "视频生成" as const,
      status: "running" as const,
      progress: 45,
      submitted: "2026-09-06T09:00:00Z",
    };
    live.loadStudioData.mockResolvedValue({
      people: [],
      assets: [],
      videos: [],
      projects: [],
      errors: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
      tasks: [runningTask],
    });
    live.reloadTasks.mockResolvedValue([
      { ...runningTask, status: "completed" as const, progress: 100 },
    ]);

    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("tasks")}
      />,
    );
    await vi.waitFor(() =>
      expect(screen.getByText("生成中 45%")).toBeInTheDocument(),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });

    expect(live.reloadTasks).toHaveBeenCalledWith(reviewUser);
    // "已完成" appears as both the filter tab and the refreshed row status.
    expect(screen.getAllByText("已完成")).toHaveLength(2);
    expect(screen.queryByText("生成中 45%")).not.toBeInTheDocument();
  });

  describe("C7 云端草稿", () => {
    // 前面的轮询用例开启了 fake timers 且不恢复；本组用例的 waitFor 依赖
    // 真实 setTimeout，先显式切回，防止用例间定时器状态泄漏。
    beforeEach(() => {
      vi.useRealTimers();
    });

    const emptyStudioData = {
      people: [],
      assets: [],
      videos: [],
      projects: [],
      tasks: [],
      errors: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    };

    function restoredDraft() {
      const draft = createState("copy").draft;
      draft.script.title = "云端恢复的标题";
      draft.script.text = "云端恢复的文案内容";
      draft.script.confirmed = true;
      return draft;
    }

    async function openCopyPage() {
      fireEvent.click(screen.getByRole("button", { name: "文案工坊" }));
      await waitFor(() =>
        expect(screen.getByLabelText("二创文案")).toBeInTheDocument(),
      );
    }

    it("挂载时恢复云端草稿与我的文案列表", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue({ draft: restoredDraft() });
      live.loadSavedScriptList.mockResolvedValue([
        {
          id: "saved-1",
          title: "已保存文案",
          original: "",
          text: "已保存的文本",
          version: 2,
          confirmed: false,
        },
      ]);
      render(<StudioWorkspace currentUser={reviewUser} />);

      await openCopyPage();
      // 恢复是异步 setState：等值到位，而不是等 textarea 出现。
      await waitFor(() =>
        expect(
          (screen.getByLabelText("二创文案") as HTMLTextAreaElement).value,
        ).toBe("云端恢复的文案内容"),
      );
      expect(screen.getByText("终稿 V1")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
      expect(screen.getByText("已保存文案")).toBeInTheDocument();
      // 未做任何编辑时不触发自动保存。
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(live.loadDraftMaterials).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({ text: "云端恢复的文案内容" }),
        }),
      );
    });

    it("编辑二创文案后防抖自动保存到云端", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);

      // 用真实定时器完成渲染与导航（waitFor 依赖真实 setTimeout）。
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "新的二创内容" },
        });
        expect(live.persistCloudDraft).not.toHaveBeenCalled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        const savedDraft = live.persistCloudDraft.mock.calls[0][0] as {
          script: { text: string; confirmed: boolean };
        };
        expect(savedDraft.script.text).toBe("新的二创内容");
        expect(savedDraft.script.confirmed).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存已在途时显式保存排队补写终态草稿", async () => {
      let resolveAutosave: (() => void) | undefined;
      live.persistCloudDraft.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveAutosave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "自动保存与显式保存共用正文" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());
        expect(live.persistSavedScript).toHaveBeenCalledTimes(1);
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        await act(async () => resolveAutosave?.());
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(screen.getAllByText(/已保存到我的文案/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存进行中连续两次显式保存仍发送最新终态草稿", async () => {
      let resolveAutosave: (() => void) | undefined;
      let resolveFinalDraft: (() => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveAutosave = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveFinalDraft = resolve;
          }),
        );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-double-save";
      state.draft.sourceId = "source-double-save";
      state.draft.sourceAssetId = "asset-double-save";
      state.draft.ipId = "identity-double-save";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
      await openCopyPage();

      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "连续保存的终态正文" },
      });
      await waitFor(
        () => expect(live.persistCloudDraft).toHaveBeenCalledTimes(1),
        { timeout: 3_000 },
      );
      const autosaveDraft = live.persistCloudDraft.mock.calls[0]?.[0] as
        | ReturnType<typeof restoredDraft>
        | undefined;

      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      await waitFor(() =>
        expect(live.persistSavedScript).toHaveBeenCalledTimes(2),
      );
      expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

      await act(async () => resolveAutosave?.());
      await waitFor(() =>
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2),
      );
      expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
        projectId: "project-double-save",
        sourceId: "source-double-save",
        sourceAssetId: "asset-double-save",
        quoteRevision: autosaveDraft?.quoteRevision,
        scriptEdited: false,
        script: {
          ipId: "identity-double-save",
          sourceProjectId: "project-double-save",
          sourceKind: "project",
        },
      });

      await act(async () => resolveFinalDraft?.());
      expect(await screen.findAllByText(/换设备登录也能找回/)).toHaveLength(1);
    });

    it("同范围自动保存已成功时显式保存补写终态草稿", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "已经自动保存的正文" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());
        expect(live.persistSavedScript).toHaveBeenCalledTimes(1);
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(screen.getAllByText(/已保存到我的文案/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存后显式保存补齐终态来源且重挂载无需再次保存", async () => {
      let storedDraft: ReturnType<typeof restoredDraft> | undefined;
      live.persistCloudDraft.mockImplementation(async (draft) => {
        storedDraft = structuredClone(
          draft as ReturnType<typeof restoredDraft>,
        );
      });
      live.loadCloudDraft.mockImplementation(async () =>
        storedDraft ? { draft: structuredClone(storedDraft) } : undefined,
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-source";
      state.draft.sourceId = "source-video";
      state.draft.sourceAssetId = "source-asset";
      state.draft.ipId = "identity-source";
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "需要补齐来源的正文" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(storedDraft).toMatchObject({
          projectId: "project-source",
          sourceId: "source-video",
          sourceAssetId: "source-asset",
          scriptEdited: false,
          script: {
            ipId: "identity-source",
            sourceProjectId: "project-source",
            sourceKind: "project",
          },
        });

        vi.useRealTimers();
        view.unmount();
        render(<StudioWorkspace currentUser={reviewUser} />);
        await openCopyPage();
        await waitFor(() =>
          expect(
            (screen.getByLabelText("二创文案") as HTMLTextAreaElement).value,
          ).toBe("需要补齐来源的正文"),
        );
        expect(live.loadDraftMaterials).toHaveBeenLastCalledWith(
          expect.objectContaining({
            projectId: "project-source",
            sourceId: "source-video",
            sourceAssetId: "source-asset",
            scriptEdited: false,
          }),
        );
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    });

    it("账号切换期间串行写入并只保留尚未开始的最新草稿", async () => {
      let resolveFirst: (() => void) | undefined;
      let resolveSecond: (() => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveFirst = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveSecond = resolve;
          }),
        );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const accountA = { ...reviewUser, id: "account-a" };
      const accountB = { ...reviewUser, id: "account-b" };
      const view = render(<StudioWorkspace currentUser={accountA} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A第一版" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        view.rerender(<StudioWorkspace currentUser={accountB} />);
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号B待写版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        view.rerender(<StudioWorkspace currentUser={accountA} />);
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A最终版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
          resolveFirst?.();
          await Promise.resolve();
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
          script: { text: "账号A最终版本" },
        });
        expect(
          live.persistCloudDraft.mock.calls.some(
            ([draft]) =>
              (draft as ReturnType<typeof restoredDraft>).script.text ===
              "账号B待写版本",
          ),
        ).toBe(false);
        await act(async () => resolveSecond?.());
      } finally {
        vi.useRealTimers();
      }
    });

    it("旧账号写入失败后仍发送当前账号排队的草稿", async () => {
      let rejectA: ((cause: Error) => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((_resolve, reject) => {
            rejectA = reject;
          }),
        )
        .mockResolvedValueOnce(undefined);
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const accountA = { ...reviewUser, id: "failed-account-a" };
      const accountB = { ...reviewUser, id: "current-account-b" };
      const view = render(<StudioWorkspace currentUser={accountA} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A失败版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        view.rerender(<StudioWorkspace currentUser={accountB} />);
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号B当前版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
          rejectA?.(new Error("account A offline"));
          await Promise.resolve();
        });

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
          script: { text: "账号B当前版本" },
        });
      } finally {
        vi.useRealTimers();
      }
    });

    it("旧自动保存失败后仍发送排队的确认终稿", async () => {
      let rejectAutosave: ((cause: Error) => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((_resolve, reject) => {
            rejectAutosave = reject;
          }),
        )
        .mockResolvedValueOnce(undefined);
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "等待确认的终稿" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        fireEvent.click(screen.getByRole("button", { name: "确认终稿" }));
        await act(async () => {
          rejectAutosave?.(new Error("autosave offline"));
          await Promise.resolve();
        });

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
          script: { text: "等待确认的终稿", confirmed: true },
        });
      } finally {
        vi.useRealTimers();
      }
    });

    it("复用的自动保存失败时提示草稿待同步并允许显式重试", async () => {
      let rejectAutosave: ((cause: Error) => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((_resolve, reject) => {
            rejectAutosave = reject;
          }),
        )
        .mockRejectedValueOnce(new Error("final draft offline"))
        .mockResolvedValueOnce(undefined);
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-retry";
      state.draft.sourceId = "source-retry";
      state.draft.sourceAssetId = "asset-retry";
      state.draft.ipId = "identity-retry";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "首次同步失败后重试" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => rejectAutosave?.(new Error("offline")));

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(
          screen.getByText(/版本已保存，但云端草稿同步失败/),
        ).toBeInTheDocument();
        expect(
          screen.queryByText(/换设备登录也能找回/),
        ).not.toBeInTheDocument();
        expect(
          screen.getByRole("button", { name: "按 IP 二创" }),
        ).toBeDisabled();

        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(3);
        expect(live.persistCloudDraft.mock.calls[2]?.[0]).toMatchObject({
          projectId: "project-retry",
          sourceId: "source-retry",
          sourceAssetId: "asset-retry",
          scriptEdited: false,
          script: {
            ipId: "identity-retry",
            sourceProjectId: "project-retry",
            sourceKind: "project",
          },
        });
        expect(screen.getAllByText(/换设备登录也能找回/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存定时器等待期间账号A到B再回A时不使用迟到认证写入", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const accountA = { ...reviewUser, id: "account-a" };
      const accountB = { ...reviewUser, id: "account-b" };
      const view = render(<StudioWorkspace currentUser={accountA} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A的待保存内容" },
        });
        view.rerender(<StudioWorkspace currentUser={accountB} />);
        view.rerender(<StudioWorkspace currentUser={accountA} />);

        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it("保存版本写入云端我的文案", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "要保存的文案";
      state.draft.scriptEdited = true;
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      await waitFor(() => expect(live.persistSavedScript).toHaveBeenCalled());
      const [script] = live.persistSavedScript.mock.calls[0] as unknown as [
        { text: string },
      ];
      expect(script.text).toBe("要保存的文案");
      expect(live.persistSavedScript).toHaveBeenCalledWith(
        expect.objectContaining({
          ipId: "identity-1",
          sourceProjectId: "project-1",
        }),
        "project-1",
        "identity-1",
      );
      expect(screen.getByText(/已保存到我的文案/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "按 IP 二创" })).toBeEnabled();
      expect(live.persistCloudDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-1",
          ipId: "identity-1",
          scriptEdited: false,
        }),
      );
    });

    it("StrictMode下慢云写成功后仅写一次草稿并通知一次", async () => {
      let resolveCloud: (() => void) | undefined;
      live.persistCloudDraft.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveCloud = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "只保存一次的文案";
      state.draft.scriptEdited = true;
      render(
        <StrictMode>
          <StudioWorkspace currentUser={reviewUser} initialState={state} />
        </StrictMode>,
      );

      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
          resolveCloud?.();
          await Promise.resolve();
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        expect(screen.getAllByText(/已保存到我的文案/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("显式草稿云写等待期间卸载后保持静默且不追加写入", async () => {
      let resolveCloud: (() => void) | undefined;
      live.persistCloudDraft.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveCloud = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.script.text = "卸载前的显式保存";
      state.draft.scriptEdited = true;
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      await waitFor(() =>
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1),
      );
      view.unmount();
      await act(async () => resolveCloud?.());
      expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
    });

    it("版本保存失败时仍保持未保存门禁", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.persistSavedScript.mockRejectedValueOnce(new Error("save failed"));
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "仍未保存的文案";
      state.draft.scriptEdited = true;
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));

      await waitFor(() =>
        expect(screen.getByText(/云端保存失败/)).toBeInTheDocument(),
      );
      expect(screen.getByRole("button", { name: "按 IP 二创" })).toBeDisabled();
      expect(screen.getByText(/先保存当前编辑/)).toBeInTheDocument();
    });

    it("保存等待期间账号A到B再回A时不写草稿、不更新列表且保持静默", async () => {
      let resolveSave: (() => void) | undefined;
      live.persistSavedScript.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "source-1";
      state.draft.sourceAssetId = "source-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "账号隔离正文";
      state.draft.scriptEdited = true;
      const accountA = { ...reviewUser, id: "account-a" };
      const accountB = { ...reviewUser, id: "account-b" };
      const view = render(
        <StudioWorkspace currentUser={accountA} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));

      view.rerender(
        <StudioWorkspace currentUser={accountB} initialState={state} />,
      );
      view.rerender(
        <StudioWorkspace currentUser={accountA} initialState={state} />,
      );
      await act(async () => resolveSave?.());

      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(screen.queryByText(/已保存到我的文案/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
      expect(screen.queryByText("账号隔离正文")).not.toBeInTheDocument();
    });

    it("保存等待期间正文A到B再回A或卸载时不发后续cloud写入", async () => {
      let resolveSave: (() => void) | undefined;
      live.persistSavedScript.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "source-1";
      state.draft.sourceAssetId = "source-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "正文A";
      state.draft.scriptEdited = true;
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "正文B" },
      });
      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "正文A" },
      });
      view.unmount();
      await act(async () => resolveSave?.());

      expect(live.persistCloudDraft).not.toHaveBeenCalled();
    });

    it("保存等待期间完整草稿被替换时即使脚本字段相同也不回填", async () => {
      let resolveSave: (() => void) | undefined;
      live.persistSavedScript.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "source-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "局部字段保持不变";
      state.draft.scriptEdited = true;
      state.draft.prompt = "旧提示词";
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));

      state.draft.prompt = "替换后的另一份完整草稿";
      view.rerender(
        <StudioWorkspace
          currentUser={{ ...reviewUser }}
          initialState={state}
        />,
      );
      await act(async () => resolveSave?.());

      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(screen.queryByText(/已保存到我的文案/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
      expect(screen.queryByText("局部字段保持不变")).not.toBeInTheDocument();
    });

    it("确认终稿立即持久化并带 projectId 时发布到项目脚本版本", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.script.text = "终稿内容";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "确认终稿" }));
      await waitFor(() =>
        expect(live.publishScriptVersion).toHaveBeenCalledWith(
          "project-1",
          "终稿内容",
        ),
      );
      expect(live.persistCloudDraft).toHaveBeenCalled();
      const savedDraft = live.persistCloudDraft.mock.calls[0][0] as unknown as {
        script: { confirmed: boolean };
      };
      expect(savedDraft.script.confirmed).toBe(true);
    });

    it("确认终稿无项目来源时不发布脚本版本", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.script.text = "无项目终稿";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "确认终稿" }));
      await waitFor(() => expect(live.persistCloudDraft).toHaveBeenCalled());
      expect(live.publishScriptVersion).not.toHaveBeenCalled();
    });

    it("提取文案成功后回填草稿并跳转文案工坊", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.extractScriptFromUpload.mockResolvedValue({
        text: "提取出的乡墅口播原文",
      });
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(live.extractScriptFromUpload).toHaveBeenCalledWith(
          "project-1",
          "asset-1",
        ),
      );
      await waitFor(() =>
        expect(screen.getByLabelText("二创文案")).toBeInTheDocument(),
      );
      expect(screen.getByText(/文案已提取/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "文案改写" }));
      expect(
        (screen.getByLabelText("二创文案") as HTMLTextAreaElement).value,
      ).toBe("提取出的乡墅口播原文");
    });

    it("重新进入工作区时恢复同一来源资产已完成的提取任务", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.loadLatestScriptFromUpload.mockResolvedValueOnce({
        id: "asr-restored",
        status: "SUCCEEDED",
        result: { text: "后台已经完成的转写文案" },
        sourceAssetId: "asset-1",
      });
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      expect(
        await screen.findByText(/文案提取已完成.*恢复到当前草稿/),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "文案工坊" }));
      expect(screen.getByLabelText("二创文案")).toHaveValue(
        "后台已经完成的转写文案",
      );
    });

    it("提取文案失败时保留工作区并提示服务端错误", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.extractScriptFromUpload.mockRejectedValue(
        new Error("语音转写服务返回错误（HTTP 500）"),
      );
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(screen.getByText(/语音转写服务返回错误/)).toBeInTheDocument(),
      );
      expect(screen.queryByLabelText("二创文案")).not.toBeInTheDocument();
    });

    it("有项目来源但资产缺失时提取文案提示先上传", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("workbench");
      state.draft.sourceId = "proj-9";
      state.draft.projectId = "proj-9";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(screen.getByText(/请先上传视频来源/)).toBeInTheDocument();
      expect(live.extractScriptFromUpload).not.toHaveBeenCalled();
    });

    it("审核示例模式不触发任何云端草稿接口", async () => {
      render(
        <StudioWorkspace
          currentUser={reviewUser}
          reviewData={createReviewData()}
          initialState={createReviewState("copy")}
        />,
      );
      await openCopyPage();
      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "审核模式编辑" },
      });
      await waitFor(() =>
        expect(
          screen.getByLabelText("二创文案") as HTMLTextAreaElement,
        ).toHaveValue("审核模式编辑"),
      );
      expect(live.loadCloudDraft).not.toHaveBeenCalled();
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(live.loadSavedScriptList).not.toHaveBeenCalled();
    });
  });
});

describe("视频生成（C2 独立创作）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    live.loadCloudDraft.mockResolvedValue(undefined);
    live.loadDraftMaterials.mockResolvedValue({
      assets: [],
      unavailableIds: [],
    });
  });

  const emptyStudioData = {
    people: [],
    assets: [],
    materials: [],
    videos: [],
    projects: [],
    tasks: [],
    errors: [],
    loading: false,
    stats: null,
    analytics7: null,
    analytics30: null,
  };

  async function openVideoPage() {
    // 侧边栏「视频创作」进入复刻页签组，再切到「视频生成」。
    fireEvent.click(screen.getByRole("button", { name: "视频创作" }));
    fireEvent.click(screen.getByRole("tab", { name: "视频生成" }));
    await waitFor(() =>
      expect(screen.getByLabelText("提示词")).toBeInTheDocument(),
    );
  }

  it("T2V 扩展模式被门禁时不打开确认弹窗并提示等待供应商核对", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: false,
      t2v_enabled: false,
      i2v_enabled: true,
      r2v_enabled: false,
      last_frame_enabled: false,
      max_reference_images: 4,
      max_quantity: 4,
    });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();

    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "山间别墅延时" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));

    expect(
      await screen.findByText("该模式需要完成供应商核对后开放，敬请期待。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("生成确认 · 视频生成")).toBeNull();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("参考素材选择器只展示图片并在达到能力上限后阻止继续添加", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 2,
      max_quantity: 4,
    });
    const imageA = {
      id: "image-a",
      name: "外立面 A.jpg",
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    };
    const imageB = { ...imageA, id: "image-b", name: "外立面 B.jpg" };
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        imageA,
        imageB,
        { ...imageA, id: "video-a", name: "运镜.mp4", kind: "video" },
        { ...imageA, id: "audio-a", name: "环境声.wav", kind: "audio" },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    const pickerButton = await screen.findByRole("button", {
      name: /从素材库选择/,
    });
    fireEvent.click(pickerButton);
    const picker = screen.getByRole("dialog", { name: "选择参考素材" });
    expect(within(picker).queryByText("运镜.mp4")).toBeNull();
    expect(within(picker).queryByText("环境声.wav")).toBeNull();
    expect(within(picker).queryByText("外立面 A.jpg")).toBeNull();
    fireEvent.click(within(picker).getByRole("button", { name: /外立面 B/ }));

    expect(
      await screen.findByText("已选 2/2 张参考图，需移除后才能继续添加。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /从素材库选择/ })).toBeDisabled();
    expect(screen.getByLabelText("上传参考图")).toBeDisabled();
  });

  it("参考生视频能力关闭时禁止进入提交确认", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: false,
      t2v_enabled: false,
      i2v_enabled: true,
      r2v_enabled: false,
      last_frame_enabled: false,
      max_reference_images: 4,
      max_quantity: 4,
    });
    const image = (id: string) => ({
      id,
      name: `${id}.jpg`,
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [image("image-a")],
    });
    const initial = createState("reference");
    initial.draft.prompt = "旧草稿";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(
      await screen.findByText("参考生视频当前未开放，请等待能力开启后再提交。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(screen.queryByText("生成确认 · 视频生成")).toBeNull();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("旧草稿参考图超出能力上限时要求整理并禁止提交", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 2,
      max_quantity: 4,
    });
    const image = (id: string) => ({
      id,
      name: `${id}.jpg`,
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [image("image-a"), image("image-b"), image("image-c")],
    });
    const initial = createState("reference");
    initial.draft.prompt = "旧草稿";
    initial.draft.referenceIds = ["image-a", "image-b", "image-c"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(
      await screen.findByText("当前最多选择 2 张参考图，旧草稿已超出 1 张。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "整理参考图" })).toBeEnabled();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("云端草稿参考图解析完成前禁止误整理合法引用", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_quantity: 4,
    });
    const restored = createState("reference").draft;
    restored.prompt = "恢复草稿";
    restored.referenceIds = ["image-restored"];
    live.loadCloudDraft.mockResolvedValue({ draft: restored });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    let resolveMaterials:
      | ((value: { assets: unknown[]; unavailableIds: string[] }) => void)
      | undefined;
    live.loadDraftMaterials.mockImplementation(
      () =>
        new Promise<{ assets: unknown[]; unavailableIds: string[] }>(
          (resolve) => {
            resolveMaterials = resolve;
          },
        ),
    );
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );

    expect(
      await screen.findByText("正在恢复草稿参考图，请稍候。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "整理参考图" })).toBeNull();
    resolveMaterials?.({
      assets: [
        {
          id: "image-restored",
          name: "恢复参考图.jpg",
          kind: "image",
          group: "参考素材",
          source: "云端草稿",
          saved: true,
        },
      ],
      unavailableIds: [],
    });

    expect(await screen.findByText("恢复参考图.jpg")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "整理参考图" })).toBeNull();
  });

  it("云端草稿参考图解析失败时保留引用并可重试恢复", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_quantity: 4,
    });
    const restored = createState("reference").draft;
    restored.prompt = "恢复草稿";
    restored.referenceIds = ["image-restored"];
    live.loadCloudDraft.mockResolvedValue({ draft: restored });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    live.loadDraftMaterials
      .mockRejectedValueOnce(new Error("解析接口失败"))
      .mockResolvedValueOnce({
        assets: [
          {
            id: "image-restored",
            name: "恢复参考图.jpg",
            kind: "image",
            group: "参考素材",
            source: "云端草稿",
            saved: true,
          },
        ],
        unavailableIds: [],
      });
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );

    expect(
      await screen.findByText("草稿参考图读取失败，请重试。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "整理参考图" })).toBeNull();
    expect(live.persistCloudDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试读取草稿参考图" }));

    expect(await screen.findByText("恢复参考图.jpg")).toBeInTheDocument();
    expect(live.loadDraftMaterials).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "整理参考图" })).toBeNull();
    expect(live.persistCloudDraft).not.toHaveBeenCalled();
  });

  it("切换草稿后忽略旧草稿参考图重试的迟到结果", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_quantity: 4,
    });
    const restored = createState("reference").draft;
    restored.referenceIds = ["image-restored"];
    live.loadCloudDraft.mockResolvedValue({ draft: restored });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    let resolveRetry:
      | ((value: { assets: unknown[]; unavailableIds: string[] }) => void)
      | undefined;
    live.loadDraftMaterials
      .mockRejectedValueOnce(new Error("解析接口失败"))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRetry = resolve;
          }),
      );
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "重试读取草稿参考图" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "新建创作" }));
    fireEvent.click(screen.getByRole("button", { name: "从空白创作开始" }));
    resolveRetry?.({
      assets: [
        {
          id: "image-restored",
          name: "不应回填.jpg",
          kind: "image",
          group: "参考素材",
          source: "云端草稿",
          saved: true,
        },
      ],
      unavailableIds: [],
    });
    await Promise.resolve();

    expect(screen.queryByText("不应回填.jpg")).toBeNull();
    expect(screen.queryByText("草稿参考图读取失败，请重试。")).toBeNull();
  });

  it("视频能力读取失败后可重试并恢复参考图操作", async () => {
    api.getIndependentCapabilities
      .mockRejectedValueOnce(new Error("能力接口失败"))
      .mockResolvedValueOnce({
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_quantity: 4,
      });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        {
          id: "image-a",
          name: "外立面 A.jpg",
          kind: "image",
          group: "参考素材",
          source: "素材库",
          saved: true,
        },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(
      await screen.findByText("视频生成能力读取失败，请重试。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试读取视频能力" }));

    await waitFor(() =>
      expect(api.getIndependentCapabilities).toHaveBeenCalledTimes(2),
    );
    expect(screen.getByRole("button", { name: "生成视频" })).toBeEnabled();
  });

  it("确认弹窗打开后参考图失效也不能绕过最终提交校验", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_quantity: 4,
    });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        {
          id: "image-a",
          name: "外立面 A.jpg",
          kind: "image",
          group: "参考素材",
          source: "素材库",
          saved: true,
        },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    fireEvent.click(await screen.findByRole("button", { name: "生成视频" }));
    expect(await screen.findByText("生成确认 · 视频生成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "移除 外立面 A.jpg" }));
    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));

    expect(await screen.findByText("请至少选择一张参考图")).toBeInTheDocument();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("确认弹窗展示按秒报价并可提交任务、预览区进入进度视图", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_quantity: 4,
    });
    const submittedBatch = {
      id: "batch-video-1",
      project_id: null,
      creation_kind: "independent",
      stale: false,
      progress: { total_count: 1, terminal_count: 0, progress_percent: 0 },
      tasks: [],
    };
    api.createIndependentVideoTask.mockResolvedValue(submittedBatch);
    const runningTask = {
      id: "batch-video-1",
      type: "视频生成",
      title: "视频生成",
      status: "running",
      progress: 40,
      submitted: "2026-09-06 12:00:00",
    };
    live.loadStudioData
      .mockResolvedValueOnce(emptyStudioData)
      .mockResolvedValue({
        ...emptyStudioData,
        tasks: [runningTask],
      });
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();

    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "航拍乡墅庭院" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));

    // 报价按秒折算：120 分/秒 × 8 秒 = 9.60 元。
    expect(await screen.findByText(/9\.60 元/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));
    await waitFor(() =>
      expect(api.createIndependentVideoTask).toHaveBeenCalledTimes(1),
    );
    const input = api.createIndependentVideoTask.mock.calls[0][0] as {
      mode: string;
      prompt_text: string;
      idempotency_key: string;
    };
    expect(input.mode).toBe("t2v");
    expect(input.prompt_text).toBe("航拍乡墅庭院");
    expect(input.idempotency_key).toBeTruthy();

    // 提交后留在本页：预览区出现阶段进度（数据来自任务轮询）。
    expect(await screen.findByText("生成中")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "生成进度" }),
    ).toBeInTheDocument();
  });

  it("提示词导入：从我的提示词一键回填", async () => {
    api.listUserSavedPrompts.mockResolvedValue([
      {
        id: "sp-1",
        project_id: "project-a",
        name: "庭院黄昏",
        prompt_text: "黄昏光线下的庭院推进镜头",
        created_at: "2026-09-06 12:00:00",
      },
    ]);
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();

    fireEvent.click(screen.getByRole("button", { name: "导入提示词" }));
    fireEvent.click(await screen.findByText("庭院黄昏"));

    expect((screen.getByLabelText("提示词") as HTMLTextAreaElement).value).toBe(
      "黄昏光线下的庭院推进镜头",
    );
  });
});
