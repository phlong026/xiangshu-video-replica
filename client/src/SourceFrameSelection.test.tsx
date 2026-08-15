import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  confirmSourceFrame,
  extractSourceFrames,
  getAssetDownloadUrl,
  getLatestProjectSourceFrameSelection,
  getLatestProjectSourceFrames,
} from "./api";
import { SourceFrameSelection } from "./SourceFrameSelection";

vi.mock("./api", () => ({
  confirmSourceFrame: vi.fn(),
  extractSourceFrames: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getLatestProjectSourceFrameSelection: vi.fn(),
  getLatestProjectSourceFrames: vi.fn(),
  readSourceFrameCandidates: vi.fn((version) => version.payload),
}));

const candidatesVersion = {
  id: "source-candidates-1",
  project_id: "project-1",
  asset_id: "reference-1",
  kind: "source_frame_candidates",
  version_number: 1,
  payload: {
    requested_timestamps_seconds: [0.5, 1.5, 2.5],
    candidates: [
      { asset_id: "source-1", timestamp_seconds: 1.5, score: 0.83 },
      { asset_id: "source-2", timestamp_seconds: 0.5, score: 0.52 },
    ],
  },
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

describe("SourceFrameSelection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValue(
      candidatesVersion,
    );
    vi.mocked(getLatestProjectSourceFrameSelection).mockResolvedValue({
      version: null,
      stale: false,
    });
    vi.mocked(getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `https://private.example/${assetId}.jpg`,
    }));
    vi.mocked(confirmSourceFrame).mockResolvedValue({
      ...candidatesVersion,
      id: "source-selection-1",
      kind: "source_frame_selection",
      payload: { source_frame_asset_id: "source-1" },
    });
    vi.mocked(extractSourceFrames).mockResolvedValue(candidatesVersion);
  });

  it("shows ranked candidates and requires explicit confirmation", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByText("候选源画面")).toBeInTheDocument();
    expect(screen.getByAltText("候选源画面 1")).toHaveAttribute(
      "src",
      "https://private.example/source-1.jpg",
    );
    expect(screen.getByText("技术画质参考 0.83")).toBeInTheDocument();
    expect(screen.getByText("技术画质参考 0.52")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认源画面" })).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: /候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认源画面" }));

    await waitFor(() =>
      expect(confirmSourceFrame).toHaveBeenCalledWith("project-1", "source-1"),
    );
    expect(await screen.findByText("已确认候选源画面 1。")).toBeInTheDocument();
  });

  it("rejects malformed manual timestamps instead of silently dropping them", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByText("候选源画面");
    fireEvent.change(screen.getByLabelText("重新取帧时间点（秒）"), {
      target: { value: "0.5, invalid" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重新提取候选" }));

    expect(
      await screen.findByText(/请输入 1–3 个首 3 秒内且不重复的时间点/),
    ).toBeInTheDocument();
    expect(extractSourceFrames).not.toHaveBeenCalled();
  });

  it("does not allow an unseen candidate to be confirmed", async () => {
    vi.mocked(getAssetDownloadUrl).mockRejectedValueOnce(
      new Error("签名 URL 不可用"),
    );
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("预览加载失败，请重新提取"),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /候选 1/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认源画面" })).toBeDisabled();
  });

  it("does not request protected preview downloads for a read-only auditor", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        readOnly
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText(/只读身份不加载素材预览/),
    ).toBeInTheDocument();
    expect(getAssetDownloadUrl).not.toHaveBeenCalled();
  });
});
