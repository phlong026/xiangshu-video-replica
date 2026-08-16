import { fireEvent, render, screen } from "@testing-library/react";
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
  CharacterSelection: ({ onVersionChange }: apiMockProps) => (
    <>
      <button
        onClick={() => onVersionChange?.(characterSelection)}
        type="button"
      >
        完成角色选择
      </button>
      <button
        onClick={() => onVersionChange?.(changedCharacterSelection)}
        type="button"
      >
        切换角色版本
      </button>
      <button
        onClick={() => onVersionChange?.(legacyCharacterSelection)}
        type="button"
      >
        恢复历史兼容人物
      </button>
    </>
  ),
}));

vi.mock("./SourceFrameSelection", () => ({
  SourceFrameSelection: ({ onSelectionChange }: apiMockProps) => (
    <>
      <button
        onClick={() => onSelectionChange?.(sourceSelection)}
        type="button"
      >
        完成源画面
      </button>
      <button onClick={() => onSelectionChange?.(null)} type="button">
        标记源画面失效
      </button>
    </>
  ),
}));

vi.mock("./CharacterReferenceSelection", () => ({
  CharacterReferenceSelection: ({ onSelectionChange }: apiMockProps) => (
    <button
      onClick={() => onSelectionChange?.(referenceSelection)}
      type="button"
    >
      完成人物参考
    </button>
  ),
}));

vi.mock("./FirstFrameSelection", () => ({
  FirstFrameSelection: ({
    legacyCharacterSelected,
    onSelectionChange,
    referenceSelection: currentReferenceSelection,
  }: apiMockProps) => (
    <>
      <span>首帧历史可查看</span>
      {legacyCharacterSelected && !currentReferenceSelection ? (
        <span>历史兼容首帧可用</span>
      ) : null}
      <button
        onClick={() => onSelectionChange?.(firstFrameSelection)}
        type="button"
      >
        完成置换首帧
      </button>
    </>
  ),
}));

vi.mock("./GenerationComposer", () => ({
  GenerationComposer: ({
    firstFrameAssetId,
    onBatchCreated,
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
  onSelectionChange?: (value: unknown) => void;
  onVersionChange?: (value: unknown) => void;
  referenceSelection?: unknown;
};

type generationComposerMockProps = {
  firstFrameAssetId?: string;
  onBatchCreated?: (value: unknown) => void;
  onWorkflowStepChange?: (step: number) => void;
  originalScript?: string;
  readOnly?: boolean;
  shotCardVersionId?: string;
};

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
    expect(screen.getByTitle("选择角色版本")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.queryByText("完成源画面")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(screen.getByTitle("选择起始帧")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "完成源画面" }));
    expect(screen.getByTitle("确认人物参考")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(screen.getByTitle("确认置换首帧")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "完成置换首帧" }));
    expect(screen.getByTitle("确认口播稿")).toHaveAttribute(
      "aria-current",
      "step",
    );

    fireEvent.click(screen.getByRole("button", { name: "完成人物参考" }));
    expect(screen.getByTitle("确认口播稿")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(screen.getByTitle("确认口播稿")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByText("首帧历史可查看")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "切换角色版本" }));
    expect(screen.getByTitle("选择起始帧")).toHaveAttribute(
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

    expect(screen.getByTitle("选择起始帧")).toHaveAttribute(
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
    expect(screen.getByTitle("确认置换首帧")).toHaveAttribute(
      "aria-current",
      "step",
    );
  });

  it("opens generation only with a saved shot card and forwards the created batch", async () => {
    const onBatchCreated = vi.fn();
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
    expect(screen.getByTitle("设置数量并生成")).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "模拟创建批次" }));
    expect(onBatchCreated).toHaveBeenCalledWith(
      expect.objectContaining({ id: "batch-1" }),
    );
  });

  it("closes generation when the visible shot cards have unsaved edits", async () => {
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

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "未保存的新场景" },
    });

    expect(
      screen.queryByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("镜头卡片已编辑，请保存后再继续生成。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "咖啡馆" },
    });

    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存镜头卡片" }));
    expect(api.saveShotCards).not.toHaveBeenCalled();
    expect(
      screen.getByText("镜头卡片没有改动，无需重复保存。"),
    ).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "保存镜头卡片" }));

    expect(api.saveShotCards).not.toHaveBeenCalled();
    expect(
      screen.getByText("生成输入：shot-card-2/first-frame-1/原始口播稿"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("生成草稿")).toHaveValue("未保存的生成草稿");
    expect(screen.getByRole("button", { name: "模拟创建批次" })).toBeEnabled();
    expect(
      screen.getByText("镜头卡片没有改动，无需重复保存。"),
    ).toBeInTheDocument();
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
    fireEvent.change(screen.getByLabelText("S01 场景"), {
      target: { value: "已修改的场景" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存镜头卡片" }));

    expect(api.saveShotCards).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("S01 场景")).toBeDisabled();
    expect(screen.getByRole("button", { name: "正在保存" })).toBeDisabled();

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
    expect(
      await screen.findByText(/镜头卡片已保存为版本 #3/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("S01 场景")).toBeEnabled();
  });
});
