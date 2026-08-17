import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AnalysisWorkspace } from "./AnalysisWorkspace";
import * as api from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getLatestProjectAnalysis: vi.fn(),
    getLatestProjectShotCards: vi.fn(),
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
    onBusyChange,
    onSelectionChange,
    readOnly,
  }: apiMockProps) => (
    <>
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
  GenerationComposer: ({
    firstFrameAssetId,
    onBatchCreated,
    onBusyChange,
    onWorkflowStepChange,
    originalScript,
    readOnly,
    shotCardVersionId,
  }: generationComposerMockProps) => (
    <div>
      <span>
        生成输入：{shotCardVersionId}/{firstFrameAssetId}/{originalScript}
      </span>
      <textarea
        aria-label="生成草稿"
        defaultValue="草稿初始值"
        readOnly={readOnly}
      />
      <button onClick={() => onWorkflowStepChange?.(9)} type="button">
        模拟锁定 Prompt
      </button>
      <button onClick={() => onBusyChange?.(true)} type="button">
        模拟生成处理中
      </button>
      <button onClick={() => onBusyChange?.(false)} type="button">
        模拟生成完成
      </button>
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
  onBusyChange?: (isBusy: boolean) => void;
  onWorkflowStepChange?: (step: number) => void;
  originalScript?: string;
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
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.queryByText("完成源画面")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    expect(screen.getByTitle("口播与生成")).toHaveAttribute(
      "aria-current",
      "step",
    );

    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(screen.getByTitle("口播与生成")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(screen.getByTitle("口播与生成")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
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

    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
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
    expect(screen.getByTitle("画面与人物")).toHaveAttribute(
      "aria-current",
      "step",
    );
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
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "模拟锁定 Prompt" }));
    expect(screen.getByTitle("口播与生成")).toHaveAttribute(
      "aria-current",
      "step",
    );
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
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿");

    // 生成处理中：切换角色的请求必须被拒绝，下游选择保持不变。
    fireEvent.click(screen.getByRole("button", { name: "模拟生成处理中" }));
    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(generationInput()).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "标记源画面失效" }));
    expect(generationInput()).toBeInTheDocument();

    // 生成完成后，同一操作立即生效（切换角色会清空下游，生成区退回门禁提示）。
    fireEvent.click(screen.getByRole("button", { name: "模拟生成完成" }));
    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(screen.getByText("确认置换首帧后可继续。")).toBeInTheDocument();
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
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("生成草稿"), {
      target: { value: "未保存的生成草稿" },
    });
    fireEvent.click(screen.getByRole("button", { name: "模拟锁定 Prompt" }));
    expect(screen.getByTitle("口播与生成")).toHaveAttribute(
      "aria-current",
      "step",
    );
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

    fireEvent.click(screen.getByRole("button", { name: "模拟生成处理中" }));
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
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "处理中不应写入" },
    });
    expect(screen.getByLabelText("S01 场景")).toHaveValue("咖啡馆");
    fireEvent.click(screen.getByRole("button", { name: "模拟生成完成" }));
    expect(screen.getByLabelText("S01 场景")).toBeEnabled();

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "未保存的新场景" },
    });

    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("生成草稿")).toHaveValue("未保存的生成草稿");
    expect(screen.getByLabelText("生成草稿")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "模拟创建批次" })).toBeDisabled();
    expect(screen.getByText("镜头编辑自动保存中…")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "咖啡馆" },
    });

    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
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
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("生成草稿"), {
      target: { value: "未保存的生成草稿" },
    });

    expect(api.saveShotCards).not.toHaveBeenCalled();
    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
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
});
