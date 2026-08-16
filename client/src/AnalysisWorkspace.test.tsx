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
};

vi.mock("./CharacterSelection", () => ({
  CharacterSelection: ({ onVersionChange }: apiMockProps) => (
    <button onClick={() => onVersionChange?.(characterSelection)} type="button">
      完成角色选择
    </button>
  ),
}));

vi.mock("./SourceFrameSelection", () => ({
  SourceFrameSelection: ({ onSelectionChange }: apiMockProps) => (
    <button onClick={() => onSelectionChange?.(sourceSelection)} type="button">
      完成源画面
    </button>
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
  FirstFrameSelection: ({ onSelectionChange }: apiMockProps) => (
    <button
      onClick={() => onSelectionChange?.(firstFrameSelection)}
      type="button"
    >
      完成置换首帧
    </button>
  ),
}));

type apiMockProps = {
  onSelectionChange?: (value: unknown) => void;
  onVersionChange?: (value: unknown) => void;
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
        analysis: { summary: "拆解完成", duration_seconds: 8, shots: [] },
      },
      created_by_user_id: "employee_1",
      created_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue(null);
  });

  it("opens each downstream gate only after the previous confirmation and rolls back", async () => {
    render(
      <AnalysisWorkspace
        onAnalysisReady={vi.fn()}
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

    fireEvent.click(screen.getByRole("button", { name: "完成角色选择" }));
    expect(screen.getByTitle("选择起始帧")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.queryByText("完成人物参考")).toBeNull();
    expect(screen.queryByText("完成置换首帧")).toBeNull();
  });
});
