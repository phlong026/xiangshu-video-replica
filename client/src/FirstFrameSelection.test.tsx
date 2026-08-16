import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  confirmFirstFrame,
  generateFirstFrames,
  getAssetDownloadUrl,
  getLatestProjectFirstFrameSelection,
  getLatestProjectFirstFrames,
  getProjectFirstFrameHistory,
} from "./api";
import { FirstFrameSelection } from "./FirstFrameSelection";

const referenceSelection = {
  id: "reference-selection-1",
  project_id: "project-1",
  source_frame_version_id: "source-selection-1",
  character_version_id: "character-version-3",
  recommended_asset_ids_json: ["character-front"],
  selected_asset_ids_json: ["character-front"],
  recommendation_reason_json: {},
  character_version_snapshot_json: {},
  selected_by: "employee_1",
  selected_at: "2030-01-01T00:00:00Z",
};

vi.mock("./api", () => ({
  confirmFirstFrame: vi.fn(),
  generateFirstFrames: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getLatestProjectFirstFrames: vi.fn(),
  getLatestProjectFirstFrameSelection: vi.fn(),
  getProjectFirstFrameHistory: vi.fn(),
  readFirstFrameCandidates: vi.fn((version) => version.payload),
  readFirstFrameSelectionPayload: vi.fn((version) => version.payload),
}));

const candidatesVersion = {
  id: "first-frame-candidates-2",
  project_id: "project-1",
  asset_id: "source-1",
  kind: "first_frame_candidates",
  version_number: 2,
  payload: {
    provider: "apilio",
    model: "nano-banana-pro-2k",
    prompt: "replace the person",
    candidates: [
      {
        asset_id: "first-1",
        storage_key: "projects/project-1/first-1.png",
        storage_uri: "local://first-1",
        sha256: "hash-1",
        size_bytes: 100,
        content_type: "image/png",
      },
      {
        asset_id: "first-2",
        storage_key: "projects/project-1/first-2.png",
        storage_uri: "local://first-2",
        sha256: "hash-2",
        size_bytes: 100,
        content_type: "image/png",
      },
    ],
  },
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

describe("FirstFrameSelection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: candidatesVersion,
      stale: false,
    });
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([
      candidatesVersion,
    ]);
    vi.mocked(getLatestProjectFirstFrameSelection).mockResolvedValue({
      version: null,
      stale: false,
    });
    vi.mocked(getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `https://private.example/${assetId}.png`,
    }));
    vi.mocked(confirmFirstFrame).mockResolvedValue({
      ...candidatesVersion,
      id: "first-frame-selection-1",
      kind: "first_frame_selection",
      payload: { first_frame_asset_id: "first-1" },
    });
    vi.mocked(generateFirstFrames).mockResolvedValue(candidatesVersion);
  });

  it("shows the Apilio model, candidates, and requires a visible choice before confirmation", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
      />,
    );

    expect(await screen.findByText("人物置换首帧")).toBeInTheDocument();
    expect(
      screen.getByText("当前模型：Nano Banana Pro 2K · Apilio"),
    ).toBeInTheDocument();
    expect(screen.getByAltText("首帧候选 1")).toHaveAttribute(
      "src",
      "https://private.example/first-1.png",
    );
    expect(
      screen.getByRole("button", { name: "确认用于 H3 的首帧" }),
    ).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认用于 H3 的首帧" }));

    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-1"),
    );
    expect(
      await screen.findByText(
        "已确认首帧候选 1。保存镜头卡片并锁定 H3 提示词后，才能创建视频批次。",
      ),
    ).toBeInTheDocument();
  });

  it("allows the employee to change the model, edit the prompt, and regenerate candidates", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
      />,
    );
    await screen.findByText("人物置换首帧");

    fireEvent.change(screen.getByLabelText("首帧模型"), {
      target: { value: "gpt-image-2" },
    });
    fireEvent.change(screen.getByLabelText("首帧编辑提示词"), {
      target: { value: "Use the selected character identity." },
    });
    fireEvent.change(screen.getByLabelText("候选数量"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重新生成候选首帧" }));

    await waitFor(() =>
      expect(generateFirstFrames).toHaveBeenCalledWith("project-1", {
        model: "gpt-image-2",
        prompt: "Use the selected character identity.",
        quantity: 2,
        character_version_id: "character-version-3",
        character_reference_selection_id: "reference-selection-1",
      }),
    );
  });

  it("clearly marks local fake output instead of presenting it as a provider result", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        payload: { ...candidatesVersion.payload, provider: "fake" },
      },
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
      />,
    );

    expect(
      await screen.findByText("模拟输出：尚未调用 Apilio 真实模型。"),
    ).toBeInTheDocument();
  });

  it("does not treat a selection from another candidate version as current", async () => {
    vi.mocked(getLatestProjectFirstFrameSelection).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        id: "first-frame-selection-1",
        kind: "first_frame_selection",
        payload: {
          first_frame_candidates_version_id: "first-frame-candidates-older",
          first_frame_asset_id: "first-1",
        },
      },
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
      />,
    );

    expect(
      await screen.findByText(
        "已确认首帧与当前候选不一致，请重新确认最新候选。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).not.toBeChecked();
  });

  it("does not request protected preview downloads for a read-only auditor", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        readOnly
        referenceSelection={referenceSelection}
      />,
    );

    expect(
      await screen.findByText(/只读身份不加载素材预览/),
    ).toBeInTheDocument();
    expect(getAssetDownloadUrl).not.toHaveBeenCalled();
  });

  it("treats a stale latest generation as an upstream gate instead of a load error", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: null,
      stale: true,
    });

    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
      />,
    );

    expect(
      await screen.findByText("上游输入已更新，请重新生成人物置换首帧。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/读取候选首帧失败/)).toBeNull();
  });
});
