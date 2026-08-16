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
    shotCardVersionId,
  }: generationComposerMockProps) => (
    <div>
      <span>
        生成输入：{shotCardVersionId}/{firstFrameAssetId}/{originalScript}
      </span>
      <button onClick={() => onWorkflowStepChange?.(9)} type="button">
        模拟锁定 Prompt
      </button>
      <button
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
});
