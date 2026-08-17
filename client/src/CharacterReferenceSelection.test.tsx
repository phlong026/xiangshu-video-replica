import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterReferenceSelection } from "./CharacterReferenceSelection";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getAssetDownloadUrl: vi.fn(),
    getCharacterReferenceRecommendation: vi.fn(),
    getLatestCharacterReferenceSelection: vi.fn(),
    selectCharacterReferences: vi.fn(),
  };
});

const viewTypes = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "RIGHT_45",
  "LEFT_SIDE",
  "RIGHT_SIDE",
] as const;

const recommendation = {
  source_frame_version_id: "source-selection-1",
  character_version_id: "character-version-3",
  recommended_asset_ids_json: ["asset-LEFT_SIDE", "asset-FRONT_FACE"],
  candidate_assets: viewTypes.map((viewType) => ({
    character_asset_id: `character-asset-${viewType}`,
    asset_id: `asset-${viewType}`,
    view_type: viewType,
  })),
  recommendation_reason_json: {
    body_view_type: "LEFT_SIDE",
  },
  character_version_snapshot_json: {
    main_character_version_id: "main-character-1",
  },
};

const savedSelection = {
  id: "reference-selection-1",
  project_id: "project-1",
  source_frame_version_id: "source-selection-1",
  character_version_id: "character-version-3",
  recommended_asset_ids_json: recommendation.recommended_asset_ids_json,
  selected_asset_ids_json: recommendation.recommended_asset_ids_json,
  recommendation_reason_json: recommendation.recommendation_reason_json,
  character_version_snapshot_json:
    recommendation.character_version_snapshot_json,
  selected_by: "employee_1",
  selected_at: "2030-01-01T00:00:00Z",
};

const sourceFrameSelection = {
  id: "source-selection-1",
  project_id: "project-1",
  asset_id: "source-1",
  kind: "source_frame_selection",
  version_number: 1,
  payload: { source_frame_asset_id: "source-1" },
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

const characterSelection = {
  project_id: "project-1",
  character_id: null,
  character_version_id: "character-version-3",
  version_id: "main-character-1",
  version_number: 1,
  character_snapshot: {
    schema_version: "project-character-selection.v1",
    name: "林夏",
  },
};

describe("CharacterReferenceSelection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getCharacterReferenceRecommendation).mockResolvedValue(
      recommendation,
    );
    vi.mocked(api.getLatestCharacterReferenceSelection).mockResolvedValue(null);
    vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `https://private.example/${assetId}.png`,
    }));
    vi.mocked(api.selectCharacterReferences).mockResolvedValue(savedSelection);
  });

  it("previews deterministic recommendations without treating them as confirmation", async () => {
    const onSelectionChange = vi.fn();
    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    expect(await screen.findByText("人物参考图")).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox", { checked: true })).toHaveLength(2);
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
    expect(api.selectCharacterReferences).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认人物参考" }));
    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledWith("project-1", {
        selected_asset_ids: ["asset-LEFT_SIDE", "asset-FRONT_FACE"],
        source_frame_selection_version_id: "source-selection-1",
        character_version_id: "character-version-3",
      }),
    );
    expect(onSelectionChange).toHaveBeenLastCalledWith(savedSelection);
  });

  it("restores only a selection bound to the current source and character versions", async () => {
    const onSelectionChange = vi.fn();
    vi.mocked(api.getLatestCharacterReferenceSelection).mockResolvedValue({
      ...savedSelection,
      source_frame_version_id: "source-selection-old",
    });

    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    expect(
      await screen.findByText(/已有选择与当前源画面或角色版本不一致/),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
  });

  it("keeps auditor evidence read-only and never requests protected previews", async () => {
    vi.mocked(api.getLatestCharacterReferenceSelection).mockResolvedValue(
      savedSelection,
    );
    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        projectId="project-1"
        readOnly
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    expect(
      await screen.findByText("当前人物参考图已确认。"),
    ).toBeInTheDocument();
    expect(api.getAssetDownloadUrl).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "确认人物参考" })).toBeNull();
  });

  it("lets the user remove a recommended asset whose protected preview failed", async () => {
    vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => {
      if (assetId === "asset-LEFT_SIDE") {
        throw new Error("签名 URL 不可用");
      }
      return { url: `https://private.example/${assetId}.png` };
    });

    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    const failedRecommended = await screen.findByRole("checkbox", {
      name: /左侧面/,
    });
    expect(failedRecommended).toBeChecked();
    expect(failedRecommended).toBeEnabled();
    fireEvent.click(failedRecommended);
    fireEvent.click(screen.getByRole("button", { name: "确认人物参考" }));

    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledWith("project-1", {
        selected_asset_ids: ["asset-FRONT_FACE"],
        source_frame_selection_version_id: "source-selection-1",
        character_version_id: "character-version-3",
      }),
    );
  });

  it("locks reference choices while confirmation is in flight", async () => {
    let resolveSelection: (() => void) | undefined;
    vi.mocked(api.selectCharacterReferences).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSelection = () => resolve(savedSelection);
        }),
    );
    const onBusyChange = vi.fn();
    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onBusyChange={onBusyChange}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    await screen.findByText("人物参考图");
    fireEvent.click(screen.getByRole("button", { name: "确认人物参考" }));

    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledOnce(),
    );
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    for (const checkbox of screen.getAllByRole("checkbox")) {
      expect(checkbox).toBeDisabled();
    }

    resolveSelection?.();
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
  });

  it("ignores a confirmation response after the source input changes", async () => {
    let resolveSelection:
      | ((selection: typeof savedSelection) => void)
      | undefined;
    const pendingSelection = new Promise<typeof savedSelection>((resolve) => {
      resolveSelection = resolve;
    });
    vi.mocked(api.selectCharacterReferences).mockReturnValue(pendingSelection);
    const onSelectionChange = vi.fn();
    const { rerender } = render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    await screen.findByText("人物参考图");
    fireEvent.click(screen.getByRole("button", { name: "确认人物参考" }));
    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledOnce(),
    );
    rerender(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        sourceFrameSelection={{
          ...sourceFrameSelection,
          id: "source-selection-2",
        }}
      />,
    );

    await act(async () => {
      resolveSelection?.(savedSelection);
      await pendingSelection;
    });

    expect(onSelectionChange).not.toHaveBeenCalledWith(savedSelection);
  });

  // P0-03-03：推荐默认勾选 + 一键确认。默认勾选恰为服务端任务 08 确定性
  // 规则给出的推荐对（身体视图 + FRONT_FACE 身份图），其余候选不勾选；
  // 单击一次「确认人物参考」即以推荐集原样落库。
  it("confirms the recommended pair (body view + FRONT_FACE) in a single click (P0-03-03)", async () => {
    const onSelectionChange = vi.fn();
    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    expect(
      await screen.findByRole("checkbox", { name: /左侧面/ }),
    ).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /正脸近景/ })).toBeChecked();
    for (const label of [
      /正面半身/,
      /正面全身/,
      /左 45°/,
      /右 45°/,
      /右侧面/,
    ]) {
      expect(screen.getByRole("checkbox", { name: label })).not.toBeChecked();
    }
    // 红线：预览与默认勾选不落库、不写审计，确认仍为人工动作。
    expect(api.selectCharacterReferences).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认人物参考" }));
    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledWith("project-1", {
        selected_asset_ids: ["asset-LEFT_SIDE", "asset-FRONT_FACE"],
        source_frame_selection_version_id: "source-selection-1",
        character_version_id: "character-version-3",
      }),
    );
    expect(
      await screen.findByText("当前人物参考图已确认。"),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(savedSelection);
  });

  // P0-03-03：用户仍可改选 1–4 张后确认。勾选第三张视图后确认以改选集落库，
  // 改选时选择回调归 null 并提示重新确认。
  it("still allows reselecting a different set before confirming (P0-03-03)", async () => {
    const onSelectionChange = vi.fn();
    render(
      <CharacterReferenceSelection
        characterSelection={characterSelection}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        sourceFrameSelection={sourceFrameSelection}
      />,
    );

    const extra = await screen.findByRole("checkbox", { name: /右 45°/ });
    await waitFor(() => expect(extra).toBeEnabled());
    // 挂载时 load() 已回调过一次 null，捕获增量才能独立锁定
    // 「改选后通知父级选择失效」的行为（评审 Minor：避免空转断言）。
    const callsBeforeToggle = onSelectionChange.mock.calls.length;
    fireEvent.click(extra);
    expect(onSelectionChange.mock.calls.length).toBe(callsBeforeToggle + 1);
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
    expect(
      screen.getByText("参考图选择已修改，请重新确认。"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认人物参考" }));
    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledWith("project-1", {
        selected_asset_ids: [
          "asset-LEFT_SIDE",
          "asset-FRONT_FACE",
          "asset-RIGHT_45",
        ],
        source_frame_selection_version_id: "source-selection-1",
        character_version_id: "character-version-3",
      }),
    );
    expect(onSelectionChange).toHaveBeenLastCalledWith(savedSelection);
  });
});
