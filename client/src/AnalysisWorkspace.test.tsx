import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  AnalysisWorkspace,
  shotCardFeatureSuggestion,
} from "./AnalysisWorkspace";
import * as api from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    compileGenerationPrompt: vi.fn(),
    createGenerationBatch: vi.fn(),
    createScriptVersion: vi.fn(),
    getGenerationRuntimeLimits: vi.fn(),
    getLatestGenerationPrompt: vi.fn(),
    getLatestProjectAnalysis: vi.fn(),
    getLatestProjectShotCards: vi.fn(),
    getLatestScriptVersion: vi.fn(),
    lockGenerationPrompt: vi.fn(),
    reviseGenerationPrompt: vi.fn(),
    saveShotCards: vi.fn(),
  };
});

const characterSelection = {
  project_id: "project-1",
  character_id: null,
  character_version_id: "character-version-1",
  version_id: "main-character-1",
  version_number: 1,
  character_snapshot: { name: "林夏" },
};

const changedCharacterSelection = {
  ...characterSelection,
  character_version_id: "character-version-2",
  version_id: "main-character-2",
  version_number: 2,
};

const legacyCharacterSelection = {
  project_id: "project-1",
  character_id: "legacy-character-1",
  character_version_id: "legacy-character-version-1",
  version_id: "main-character-legacy-1",
  version_number: 1,
  character_snapshot: {
    name: "历史人物",
    reference_asset_ids: ["legacy-reference-1"],
  },
};

const sourceSelection = {
  id: "source-selection-1",
  project_id: "project-1",
  asset_id: "source-1",
  kind: "source_frame_selection",
  version_number: 1,
  payload: { source_frame_asset_id: "source-1" },
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

const referenceSelection = {
  id: "reference-selection-1",
  project_id: "project-1",
  source_frame_version_id: sourceSelection.id,
  character_version_id: "character-version-1",
  recommended_asset_ids_json: ["reference-1"],
  selected_asset_ids_json: ["reference-1"],
  recommendation_reason_json: {},
  character_version_snapshot_json: {
    main_character_version_id: "main-character-1",
  },
  selected_by: "employee_1",
  selected_at: "2030-01-01T00:00:00Z",
};

const firstFrameSelection = {
  ...sourceSelection,
  id: "first-frame-selection-1",
  kind: "first_frame_selection",
  asset_id: "first-frame-1",
  payload: {
    first_frame_candidates_version_id: "first-frame-candidates-1",
    first_frame_asset_id: "first-frame-1",
  },
};

const editableShot: api.ShotCard = {
  shot_id: "S01",
  start_time: 0,
  end_time: 8,
  shot_type: "中景",
  composition: "居中",
  camera_motion: "固定",
  subject: "人物",
  action: "讲述",
  scene: "咖啡馆",
  spoken_text: "原始口播稿",
  transition: "无",
};

vi.mock("./CharacterSelection", () => ({
  CharacterSelection: ({
    onBusyChange,
    onVersionChange,
    readOnly,
  }: apiMockProps) => (
    <>
      <button
        disabled={readOnly}
        onClick={() => onVersionChange?.(characterSelection)}
        type="button"
      >
        完成角色选择
      </button>
      <button
        disabled={readOnly}
        onClick={() => onVersionChange?.(changedCharacterSelection)}
        type="button"
      >
        切换角色版本
      </button>
      <button
        disabled={readOnly}
        onClick={() => onVersionChange?.(legacyCharacterSelection)}
        type="button"
      >
        恢复历史兼容人物
      </button>
      <button onClick={() => onBusyChange?.(true)} type="button">
        模拟角色保存中
      </button>
      <button onClick={() => onBusyChange?.(false)} type="button">
        模拟角色保存完成
      </button>
    </>
  ),
}));

vi.mock("./SourceFrameSelection", () => ({
  SourceFrameSelection: ({
    featureSuggestion,
    onBusyChange,
    onSelectionChange,
    readOnly,
  }: apiMockProps) => (
    <>
      <span data-testid="sf-feature-suggestion">
        {featureSuggestion
          ? `${featureSuggestion.shot_size}/${featureSuggestion.body_completeness}`
          : "none"}
      </span>
      <button
        disabled={readOnly}
        onClick={() => onSelectionChange?.(sourceSelection)}
        type="button"
      >
        完成源画面
      </button>
      <button
        disabled={readOnly}
        onClick={() => onSelectionChange?.(null)}
        type="button"
      >
        标记源画面失效
      </button>
      <button onClick={() => onBusyChange?.(true)} type="button">
        模拟源画面保存中
      </button>
      <button onClick={() => onBusyChange?.(false)} type="button">
        模拟源画面保存完成
      </button>
    </>
  ),
}));

vi.mock("./CharacterReferenceSelection", () => ({
  CharacterReferenceSelection: ({
    onBusyChange,
    onSelectionChange,
    readOnly,
  }: apiMockProps) => (
    <>
      <button
        disabled={readOnly}
        onClick={() => onSelectionChange?.(referenceSelection)}
        type="button"
      >
        完成人物参考
      </button>
      <button onClick={() => onBusyChange?.(true)} type="button">
        模拟人物参考保存中
      </button>
      <button onClick={() => onBusyChange?.(false)} type="button">
        模拟人物参考保存完成
      </button>
    </>
  ),
}));

vi.mock("./FirstFrameSelection", () => ({
  FirstFrameSelection: ({
    legacyCharacterSelected,
    onBusyChange,
    onSelectionChange,
    readOnly,
    referenceSelection: currentReferenceSelection,
  }: apiMockProps) => (
    <>
      <span>首帧历史可查看</span>
      {legacyCharacterSelected && !currentReferenceSelection ? (
        <span>历史兼容首帧可用</span>
      ) : null}
      <button
        disabled={readOnly}
        onClick={() => onSelectionChange?.(firstFrameSelection)}
        type="button"
      >
        完成置换首帧
      </button>
      <button onClick={() => onBusyChange?.(true)} type="button">
        模拟首帧保存中
      </button>
      <button onClick={() => onBusyChange?.(false)} type="button">
        模拟首帧保存完成
      </button>
    </>
  ),
}));

vi.mock("./GenerationComposer", () => ({
  // P0-02-03：组件薄化为 drafts 注入后，mock 只保留真实 props；
  // 生成 busy 改由真实“保存口播稿”挂起驱动（drafts.busyAction）。
  GenerationComposer: ({
    firstFrameAssetId,
    onBatchCreated,
    readOnly,
    shotCardVersionId,
  }: generationComposerMockProps) => (
    <div>
      <span>
        生成输入：{shotCardVersionId}/{firstFrameAssetId}
      </span>
      <textarea
        aria-label="生成草稿"
        defaultValue="草稿初始值"
        readOnly={readOnly}
      />
      <button
        disabled={readOnly}
        onClick={() =>
          onBatchCreated?.({
            id: "batch-1",
            project_id: "project-1",
            prompt_version_id: "prompt-1",
            status: "QUEUED",
            quantity: 1,
            stale: false,
            progress: {
              total_count: 1,
              terminal_count: 0,
              progress_percent: 0,
              counts: {},
            },
            tasks: [],
          })
        }
        type="button"
      >
        模拟创建批次
      </button>
    </div>
  ),
}));

type apiMockProps = {
  featureSuggestion?: {
    body_completeness: string;
    face_visible: boolean;
    orientation: string;
    shot_size: string;
  } | null;
  legacyCharacterSelected?: boolean;
  onBusyChange?: (isBusy: boolean) => void;
  onSelectionChange?: (value: unknown) => void;
  onVersionChange?: (value: unknown) => void;
  readOnly?: boolean;
  referenceSelection?: unknown;
};

type generationComposerMockProps = {
  firstFrameAssetId?: string;
  onBatchCreated?: (value: unknown) => void;
  readOnly?: boolean;
  shotCardVersionId?: string;
};

function readReactProps<T>(element: Element): T {
  const reactPropsKey = Object.keys(element).find((key) =>
    key.startsWith("__reactProps$"),
  );
  if (!reactPropsKey) {
    throw new Error("React props are unavailable for the test element.");
  }
  return (element as unknown as Record<string, T>)[reactPropsKey];
}

describe("AnalysisWorkspace workflow gates", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue(null);
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: null,
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: null,
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getGenerationRuntimeLimits).mockResolvedValue({
      min_quantity: 1,
      max_quantity: 4,
      estimated_cost_per_task: null,
    });
  });

  it("preserves downstream gates for idempotent confirmations and rolls back on a changed character", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "流程测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 4 项",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("完成源画面")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 3 项",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 2 项",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 1 项",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText("✓"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText("✓"),
    ).toBeInTheDocument();
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText("✓"),
    ).toBeInTheDocument();
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 3 项",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("完成人物参考")).toBeNull();
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();
  });

  it("keeps first-frame history visible while the current source selection is stale", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "历史查看测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "标记源画面失效" }));

    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 3 项",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();
  });

  it("keeps legacy first-frame history and generation reachable without a seven-view selection", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "历史项目",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "恢复历史兼容人物" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));

    expect(screen.queryByText("完成人物参考")).toBeNull();
    expect(screen.getByText("历史兼容首帧可用")).toBeInTheDocument();
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText(
        "缺失 1 项",
      ),
    ).toBeInTheDocument();
  });

  it("opens generation only with a saved shot card and forwards the created batch", async () => {
    const onBatchCreated = vi.fn();
    const onWorkspaceBusyChange = vi.fn();
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={onBatchCreated}
        onClose={vi.fn()}
        onWorkspaceBusyChange={onWorkspaceBusyChange}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "生成闭环测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));

    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText("✓"),
    ).toBeInTheDocument();
    for (const [startLabel, finishLabel] of [
      ["模拟角色保存中", "模拟角色保存完成"],
      ["模拟源画面保存中", "模拟源画面保存完成"],
      ["模拟人物参考保存中", "模拟人物参考保存完成"],
      ["模拟首帧保存中", "模拟首帧保存完成"],
    ]) {
      fireEvent.click(screen.getByRole("button", { name: startLabel }));
      expect(onWorkspaceBusyChange).toHaveBeenLastCalledWith(true);
      expect(
        screen.getByRole("button", { name: "模拟创建批次" }),
      ).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "模拟创建批次" }));
      expect(onBatchCreated).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: finishLabel }));
      expect(onWorkspaceBusyChange).toHaveBeenLastCalledWith(false);
      expect(
        screen.getByRole("button", { name: "模拟创建批次" }),
      ).toBeEnabled();
    }
    fireEvent.click(screen.getByRole("button", { name: "模拟角色保存中" }));
    fireEvent.click(screen.getByRole("button", { name: "模拟角色保存中" }));
    fireEvent.click(screen.getByRole("button", { name: "模拟角色保存完成" }));
    expect(screen.getByRole("button", { name: "模拟创建批次" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "模拟角色保存完成" }));
    expect(screen.getByRole("button", { name: "模拟创建批次" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "模拟创建批次" }));
    expect(onBatchCreated).toHaveBeenCalledWith(
      expect.objectContaining({ id: "batch-1" }),
    );
  });

  it("keeps workspace busy while multiple upstream sources overlap (P0-01-02 行为锁定)", async () => {
    const onWorkspaceBusyChange = vi.fn();
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        onWorkspaceBusyChange={onWorkspaceBusyChange}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "多源 busy 聚合测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    await screen.findByText("拆解完成");
    // 首帧区块需要角色已选才渲染，先建立选择链。
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    // 两个不同上游源同时 busy：整体必须保持 busy。
    fireEvent.click(screen.getByRole("button", { name: "模拟角色保存中" }));
    expect(onWorkspaceBusyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: "模拟首帧保存中" }));
    expect(onWorkspaceBusyChange).toHaveBeenLastCalledWith(true);

    // 解除其中一个源：另一个仍 busy，导航仍被阻断。
    fireEvent.click(screen.getByRole("button", { name: "模拟角色保存完成" }));
    expect(onWorkspaceBusyChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByRole("button", { name: "返回" })).toBeDisabled();

    // 解除最后一个源：整体才恢复空闲。
    fireEvent.click(screen.getByRole("button", { name: "模拟首帧保存完成" }));
    expect(onWorkspaceBusyChange).toHaveBeenLastCalledWith(false);
    expect(screen.getByRole("button", { name: "返回" })).toBeEnabled();
  });

  it("blocks upstream selection changes while generation is busy (P0-01-02 行为锁定)", async () => {
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    let resolveScriptSave: ((version: api.AnalysisVersion) => void) | undefined;
    vi.mocked(api.createScriptVersion).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveScriptSave = resolve;
        }),
    );

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        onWorkspaceBusyChange={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "生成 busy 阻断测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));

    const generationInput = () =>
      screen.getByText("生成输入：shot-card-2/first-frame-1");

    // 生成动作处理中（口播稿保存挂起）：切换角色的请求必须被拒绝，下游选择保持不变。
    fireEvent.click(await screen.findByRole("button", { name: "保存口播稿" }));
    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(generationInput()).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "标记源画面失效" }));
    expect(generationInput()).toBeInTheDocument();

    // 生成动作完成后，同一操作立即生效（切换角色会清空下游，生成区退回骨架引导）。
    resolveScriptSave?.(savedScriptVersion);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "切换角色版本" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(
      screen.getByText("先在「人物设定」标签页确认置换首帧。"),
    ).toBeInTheDocument();
  });

  it("blocks generation without losing drafts while shot edits are reverted", async () => {
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [editableShot],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    let resolveScriptSave: ((version: api.AnalysisVersion) => void) | undefined;
    vi.mocked(api.createScriptVersion).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveScriptSave = resolve;
        }),
    );

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "镜头编辑门禁",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("生成草稿"), {
      target: { value: "未保存的生成草稿" },
    });
    expect(
      within(screen.getByRole("tab", { name: /人物设定/ })).getByText("✓"),
    ).toBeInTheDocument();
    const sceneInput = screen.getByLabelText("S01 场景");
    const form = sceneInput.closest("form");
    if (!form) {
      throw new Error("Analysis form is unavailable.");
    }
    const staleSceneHandler = readReactProps<{
      onChange: (event: { target: { value: string } }) => void;
    }>(sceneInput).onChange;
    const staleSubmitHandler = readReactProps<{
      onSubmit: (event: { preventDefault: () => void }) => void;
    }>(form).onSubmit;

    fireEvent.click(await screen.findByRole("button", { name: "保存口播稿" }));
    act(() => {
      staleSceneHandler({ target: { value: "旧闭包不应写入" } });
      staleSubmitHandler({ preventDefault: vi.fn() });
    });
    expect(screen.getByLabelText("S01 场景")).toBeDisabled();
    expect(screen.getByLabelText("S01 场景")).toHaveValue("咖啡馆");
    expect(api.saveShotCards).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "返回" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "切换角色版本" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "标记源画面失效" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "完成人物参考" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "完成置换首帧" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "处理中不应写入" },
    });
    expect(screen.getByLabelText("S01 场景")).toHaveValue("咖啡馆");
    resolveScriptSave?.(savedScriptVersion);
    await waitFor(() =>
      expect(screen.getByLabelText("S01 场景")).toBeEnabled(),
    );

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "未保存的新场景" },
    });

    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("生成草稿")).toHaveValue("未保存的生成草稿");
    expect(screen.getByLabelText("生成草稿")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "模拟创建批次" })).toBeDisabled();
    expect(screen.getByText("镜头编辑自动保存中…")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "咖啡馆" },
    });

    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("生成草稿")).toHaveValue("未保存的生成草稿");
    expect(screen.getByLabelText("生成草稿")).not.toHaveAttribute("readonly");
    expect(api.saveShotCards).not.toHaveBeenCalled();
  });

  it("skips unchanged shot-card saves without losing generation drafts", async () => {
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [editableShot],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "保存竞态门禁",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("生成草稿"), {
      target: { value: "未保存的生成草稿" },
    });

    expect(api.saveShotCards).not.toHaveBeenCalled();
    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("生成草稿")).toHaveValue("未保存的生成草稿");
    expect(screen.getByRole("button", { name: "模拟创建批次" })).toBeEnabled();
  });

  it("prevents shot edits while a changed draft is being saved", async () => {
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [editableShot],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    let resolveSave: ((version: api.AnalysisVersion) => void) | undefined;
    vi.mocked(api.saveShotCards).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "保存竞态门禁",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "已修改的场景" },
    });

    await waitFor(() => expect(api.saveShotCards).toHaveBeenCalledOnce(), {
      timeout: 3_000,
    });
    expect(screen.getByLabelText("S01 场景")).toBeDisabled();
    expect(screen.getByText("镜头保存中…")).toBeInTheDocument();

    resolveSave?.({
      id: "shot-card-3",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 3,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:01Z",
    });
    expect(await screen.findByText(/已自动保存 · 版本 #3/)).toBeInTheDocument();
    expect(screen.getByLabelText("S01 场景")).toBeEnabled();
  });

  const savedScriptVersion = {
    id: "script-2",
    project_id: "project-1",
    asset_id: null,
    kind: "script",
    version_number: 2,
    payload: {
      source: "original",
      full_text: "原始口播稿",
      shot_card_version_id: "shot-card-3",
      shot_mappings: [],
    },
    created_by_user_id: "employee_1",
    created_at: "2030-01-01T00:00:00Z",
  };

  it("标签页①：镜头卡自动保存后内容徽章转就绪（口播稿有匹配版本）", async () => {
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [editableShot],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: savedScriptVersion,
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.saveShotCards).mockResolvedValue({
      id: "shot-card-3",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 3,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:01Z",
    });

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "内容就绪测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    // 初始：镜头卡未保存且口播稿与空镜头版本不匹配 → 缺失 2 项
    expect(
      within(screen.getByRole("tab", { name: /内容配置/ })).getByText(
        "缺失 2 项",
      ),
    ).toBeInTheDocument();

    // 镜头卡未保存时保存口播稿被守卫拦截，反馈必须出现在标签页①
    // （此状态下标签页③不挂载，评审 Major 2）。
    fireEvent.click(await screen.findByRole("button", { name: "保存口播稿" }));
    expect(
      screen.getByText("镜头卡片自动保存后才能保存口播稿。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "未保存的新场景" },
    });

    await waitFor(() => expect(api.saveShotCards).toHaveBeenCalledOnce(), {
      timeout: 3_000,
    });
    expect(
      await waitFor(() =>
        within(screen.getByRole("tab", { name: /内容配置/ })).getByText("✓"),
      ),
    ).toBeInTheDocument();
  });

  it("标签页①：口播稿脏稿时内容徽章显示缺失", async () => {
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [editableShot],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...savedScriptVersion,
        payload: {
          ...savedScriptVersion.payload,
          shot_card_version_id: "shot-card-2",
        },
      },
      stale: false,
      stale_reasons: [],
    });

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "脏稿徽章测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    // 镜头卡已保存 + 口播稿已保存且匹配 → 内容就绪
    expect(
      await waitFor(() =>
        within(screen.getByRole("tab", { name: /内容配置/ })).getByText("✓"),
      ),
    ).toBeInTheDocument();

    // 标签页①内编辑口播稿：切自定义后即脏稿 → 徽章转缺失
    fireEvent.click(screen.getByLabelText("自定义稿"));
    expect(
      within(screen.getByRole("tab", { name: /内容配置/ })).getByText(
        "缺失 1 项",
      ),
    ).toBeInTheDocument();
  });

  it("标签页②：无角色时四区块同屏可见（骨架态）", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "人物流水线测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    // 纵向流水线四区块常驻同屏（角色 → 源画面 → 人物参考 → 首帧）。
    expect(
      screen.getByRole("region", { name: "角色版本" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "源画面选择" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "人物参考" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "置换首帧" }),
    ).toBeInTheDocument();
    // 角色区块可直接操作；下游三区为骨架 + 引导，不再是门禁文案。
    expect(screen.getByRole("button", { name: "完成角色选择" })).toBeEnabled();
    expect(screen.getAllByText("先在上方选择角色版本")).toHaveLength(3);
    expect(screen.queryByText("先选择角色版本")).toBeNull();
  });

  it("标签页②：选角色后源画面可交互、人物参考仍骨架", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "流水线解锁测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));

    // 源画面解锁可交互；人物参考等源画面；首帧区块挂载（历史可查看）。
    expect(screen.getByRole("button", { name: "完成源画面" })).toBeEnabled();
    expect(screen.getByText("先在上方完成源画面选择")).toBeInTheDocument();
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();
  });

  it("标签页②：切换角色后人物参考回退骨架、旧首帧历史仍可查看", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "stale 级联测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));

    // 下游 stale 级联（任务 12 决议）：人物参考回骨架，源画面可重新选择，
    // 首帧区块保留且旧首帧历史仍可查看。
    expect(screen.getByText("先在上方完成源画面选择")).toBeInTheDocument();
    expect(screen.queryByText("完成人物参考")).toBeNull();
    expect(screen.getByRole("button", { name: "完成源画面" })).toBeEnabled();
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();
  });

  it("标签页②：源画面特征建议随镜头卡首镜头映射预填（P0-03-02）", async () => {
    // 默认拆解 fixture 的 shots 为空，注入带 shot_type 的首镜头验证映射。
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue({
      id: "analysis-1",
      project_id: "project-1",
      asset_id: "reference-video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "拆解完成",
          duration_seconds: 8,
          original_script: "原始口播稿",
          shots: [editableShot],
        },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "特征预填测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    // 默认拆解首镜头 shot_type="中景" → 半身/上半身建议（预填仅建议，
    // 确认仍为人工动作）。
    expect(
      await screen.findByTestId("sf-feature-suggestion"),
    ).toHaveTextContent("HALF_BODY/UPPER_BODY");
  });

  it("标签页②：镜头卡 shot_type → 源画面特征建议映射三分支（P0-03-02）", () => {
    // 近景/特写 → 特写组合；全景/远景/全身 → 全身组合；
    // 其余（如中景）→ 半身默认。预填仅为建议值，用户可改可撤回。
    const expectSuggestion = (shotType: string, expected: object) => {
      expect(
        shotCardFeatureSuggestion({ ...editableShot, shot_type: shotType }),
      ).toEqual(expected);
    };
    const closeUp = {
      orientation: "FRONT",
      shot_size: "CLOSE_UP",
      face_visible: true,
      body_completeness: "FACE_ONLY",
    };
    const fullBody = {
      orientation: "FRONT",
      shot_size: "FULL_BODY",
      face_visible: true,
      body_completeness: "FULL_BODY",
    };
    const halfBody = {
      orientation: "FRONT",
      shot_size: "HALF_BODY",
      face_visible: true,
      body_completeness: "UPPER_BODY",
    };
    expectSuggestion("近景", closeUp);
    expectSuggestion("特写", closeUp);
    expectSuggestion("全景", fullBody);
    expectSuggestion("远景", fullBody);
    expectSuggestion("全身", fullBody);
    expectSuggestion("中景", halfBody);
  });

  it("主操作栏：未就绪点「开始生成」弹出缺失项模态（覆盖 7 类缺失）", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "缺失项模态测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始生成" }));

    // 模态列出全部 7 类缺失，每项附「前往处理」。
    expect(
      screen.getByRole("dialog", { name: "缺失项清单" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "前往处理" })).toHaveLength(7);
    for (const label of [
      "镜头卡片尚未保存",
      "口播稿尚未保存",
      "未选择角色版本",
      "未选择源画面",
      "未确认人物参考",
      "未确认首帧",
      "Prompt 未锁定或参数不一致",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }

    // Esc 可关闭（评审 Minor：模态最小键盘支持）。
    fireEvent.keyDown(screen.getByRole("dialog", { name: "缺失项清单" }), {
      key: "Escape",
    });
    expect(screen.queryByRole("dialog")).toBeNull();

    // 重新打开后「关闭」按钮同样可关闭。
    fireEvent.click(screen.getByRole("button", { name: "开始生成" }));
    expect(
      screen.getByRole("dialog", { name: "缺失项清单" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("主操作栏：「前往处理」跳转对应标签页并高亮目标区块", async () => {
    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "跳转高亮测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始生成" }));

    const characterRow = screen
      .getByText("未选择角色版本")
      .closest("li") as HTMLElement;
    fireEvent.click(
      within(characterRow).getByRole("button", { name: "前往处理" }),
    );

    // 模态关闭、目标标签页激活、目标区块高亮。
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen
        .getByRole("tab", { name: /人物设定/ })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.getByRole("region", { name: "角色版本" }).className,
    ).toContain("workspace-highlight");
  });

  it("主操作栏：全部就绪点「开始生成」提示流水线待接入（P0-02-05 行为锁定）", async () => {
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      id: "shot-card-2",
      project_id: "project-1",
      asset_id: null,
      kind: "shot_card",
      version_number: 2,
      payload: {
        source_analysis_version_id: "analysis-1",
        duration_seconds: 8,
        shots: [editableShot],
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...savedScriptVersion,
        payload: {
          ...savedScriptVersion.payload,
          shot_card_version_id: "shot-card-2",
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: {
        id: "prompt-locked-1",
        project_id: "project-1",
        asset_id: null,
        kind: "h3_prompt",
        version_number: 5,
        payload: {
          status: "LOCKED",
          prompt_text: "锁定的 Prompt",
          shot_card_version_id: "shot-card-2",
          first_frame_asset_id: "first-frame-1",
          first_frame_selection_version_id: "first-frame-selection-1",
          character_version_id: "character-version-1",
          character_reference_selection_id: "reference-selection-1",
          output_duration_seconds: 8,
          resolution: "768P",
        },
        created_by_user_id: "employee_1",
        created_at: "2030-01-01T00:00:00Z",
      },
      stale: false,
      stale_reasons: [],
    });

    render(
      <AnalysisWorkspace
        currentUserId="employee_1"
        onAnalysisReady={vi.fn()}
        onBatchCreated={vi.fn()}
        onClose={vi.fn()}
        project={{
          id: "project-1",
          owner_user_id: "employee_1",
          name: "就绪提示测试",
          status: "REFERENCE_READY",
          reference_asset_id: "reference-video-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        }}
      />,
    );

    expect(await screen.findByText("拆解完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));

    // 三段全部就绪（prompt 就绪输入接线后 launch 徽章转 ✓）。
    expect(
      await waitFor(() =>
        within(screen.getByRole("tab", { name: /生成设置/ })).getByText("✓"),
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("就绪 3/3")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "开始生成" }));
    // 流水线在 P0-04 接入；当前提示待接入且不弹模态。
    expect(
      await screen.findByText(/生成流水线将在 P0-04 接入/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
