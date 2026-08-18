import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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
      payload: {
        source_frame_asset_id: "source-1",
        character_features: {
          orientation: "FRONT",
          shot_size: "HALF_BODY",
          face_visible: true,
          body_completeness: "UPPER_BODY",
        },
      },
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
    fireEvent.change(screen.getByLabelText("人物朝向"), {
      target: { value: "FRONT" },
    });
    fireEvent.change(screen.getByLabelText("人物景别"), {
      target: { value: "HALF_BODY" },
    });
    fireEvent.change(screen.getByLabelText("面部可见性"), {
      target: { value: "VISIBLE" },
    });
    fireEvent.change(screen.getByLabelText("身体完整度"), {
      target: { value: "UPPER_BODY" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认源画面" }));

    await waitFor(() =>
      expect(confirmSourceFrame).toHaveBeenCalledWith("project-1", "source-1", {
        orientation: "FRONT",
        shot_size: "HALF_BODY",
        face_visible: true,
        body_completeness: "UPPER_BODY",
      }),
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

  it("requires a legacy selection without character features to be confirmed again", async () => {
    const onSelectionChange = vi.fn();
    vi.mocked(getLatestProjectSourceFrameSelection).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        id: "source-selection-legacy",
        kind: "source_frame_selection",
        payload: { source_frame_asset_id: "source-1" },
      },
    });

    render(
      <SourceFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText(/缺少人物特征，请重新确认源画面/),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
    expect(screen.getByRole("button", { name: "确认源画面" })).toBeDisabled();
  });

  it("locks candidate choice while confirmation is in flight", async () => {
    let resolveConfirmation: (() => void) | undefined;
    vi.mocked(confirmSourceFrame).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveConfirmation = () =>
            resolve({
              ...candidatesVersion,
              id: "source-selection-1",
              kind: "source_frame_selection",
              payload: {
                source_frame_asset_id: "source-1",
                character_features: {
                  orientation: "FRONT",
                  shot_size: "HALF_BODY",
                  face_visible: true,
                  body_completeness: "UPPER_BODY",
                },
              },
            });
        }),
    );
    const onBusyChange = vi.fn();
    render(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByText("候选源画面");
    fireEvent.click(screen.getByRole("radio", { name: /候选 1/ }));
    fireEvent.change(screen.getByLabelText("人物朝向"), {
      target: { value: "FRONT" },
    });
    fireEvent.change(screen.getByLabelText("人物景别"), {
      target: { value: "HALF_BODY" },
    });
    fireEvent.change(screen.getByLabelText("面部可见性"), {
      target: { value: "VISIBLE" },
    });
    fireEvent.change(screen.getByLabelText("身体完整度"), {
      target: { value: "UPPER_BODY" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认源画面" }));

    await waitFor(() => expect(confirmSourceFrame).toHaveBeenCalledOnce());
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByRole("radio", { name: /候选 1/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /候选 2/ })).toBeDisabled();

    resolveConfirmation?.();
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
  });

  it("ignores a confirmation response after the project input changes", async () => {
    const savedSelection = {
      ...candidatesVersion,
      id: "source-selection-1",
      kind: "source_frame_selection",
      payload: {
        source_frame_asset_id: "source-1",
        character_features: {
          orientation: "FRONT",
          shot_size: "HALF_BODY",
          face_visible: true,
          body_completeness: "UPPER_BODY",
        },
      },
    };
    let resolveConfirmation:
      | ((selection: typeof savedSelection) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof savedSelection>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmSourceFrame).mockReturnValue(pendingConfirmation);
    const onSelectionChange = vi.fn();
    const { rerender } = render(
      <SourceFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    fireEvent.click(screen.getByRole("radio", { name: /候选 1/ }));
    fireEvent.change(screen.getByLabelText("人物朝向"), {
      target: { value: "FRONT" },
    });
    fireEvent.change(screen.getByLabelText("人物景别"), {
      target: { value: "HALF_BODY" },
    });
    fireEvent.change(screen.getByLabelText("面部可见性"), {
      target: { value: "VISIBLE" },
    });
    fireEvent.change(screen.getByLabelText("身体完整度"), {
      target: { value: "UPPER_BODY" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认源画面" }));
    await waitFor(() => expect(confirmSourceFrame).toHaveBeenCalledOnce());

    rerender(
      <SourceFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await act(async () => {
      resolveConfirmation?.(savedSelection);
      await pendingConfirmation;
    });

    expect(onSelectionChange).not.toHaveBeenCalledWith(savedSelection);
  });

  it("does not reload an old project after a stale extraction completes", async () => {
    let resolveExtraction:
      | ((version: typeof candidatesVersion) => void)
      | undefined;
    const pendingExtraction = new Promise<typeof candidatesVersion>(
      (resolve) => {
        resolveExtraction = resolve;
      },
    );
    vi.mocked(extractSourceFrames).mockReturnValue(pendingExtraction);
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByText("候选源画面");
    fireEvent.click(screen.getByRole("button", { name: "重新提取候选" }));
    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());
    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await waitFor(() =>
      expect(getLatestProjectSourceFrames).toHaveBeenCalledWith("project-2"),
    );
    const loadCount = vi.mocked(getLatestProjectSourceFrames).mock.calls.length;

    await act(async () => {
      resolveExtraction?.(candidatesVersion);
      await pendingExtraction;
    });

    expect(getLatestProjectSourceFrames).toHaveBeenCalledTimes(loadCount);
  });

  // P0-03-02：候选自动提取与特征预填（红灯先行）。

  it("auto-extracts default candidates when the project has none", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-1",
        "reference-1",
        [0.5, 1.5, 2.5],
      ),
    );
    expect(await screen.findByAltText("候选源画面 1")).toBeInTheDocument();
  });

  it("keeps the auto-extraction notice visible after candidates reload", async () => {
    // P0-05-02 评审 M1：自动提取成功后递归 loadCandidates 走无确认分支，
    // 提示须保留至人工确认，不被空文案覆盖（先等候选重载完成再断言，
    // 避免只测到递归 await 窗口内的瞬态显示）。
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByAltText("候选源画面 1")).toBeInTheDocument();
    expect(
      screen.getByText("已自动提取候选源画面，请核对后确认。"),
    ).toBeInTheDocument();
  });

  it("does not auto-extract for a read-only auditor", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    render(
      <SourceFrameSelection
        projectId="project-1"
        readOnly
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByText("尚未提取候选源画面。")).toBeInTheDocument();
    expect(extractSourceFrames).not.toHaveBeenCalled();
  });

  it("auto-extracts again after switching to another project without candidates", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());

    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-2",
        "reference-2",
        [0.5, 1.5, 2.5],
      ),
    );
  });

  it("preselects the highest-scored candidate after loading", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    expect(screen.getByRole("radio", { name: /候选 1/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /候选 2/ })).not.toBeChecked();
  });

  it("prefills feature suggestions once and keeps user edits", async () => {
    const { rerender } = render(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "FACE_ONLY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "CLOSE_UP",
        }}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    expect(screen.getByLabelText("人物朝向")).toHaveValue("FRONT");
    expect(screen.getByLabelText("人物景别")).toHaveValue("CLOSE_UP");
    expect(screen.getByLabelText("面部可见性")).toHaveValue("VISIBLE");
    expect(screen.getByLabelText("身体完整度")).toHaveValue("FACE_ONLY");

    // 用户修改后，新的建议值（镜头卡自动保存）不得覆盖用户输入。
    fireEvent.change(screen.getByLabelText("人物朝向"), {
      target: { value: "LEFT_45" },
    });
    rerender(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "FULL_BODY",
          face_visible: false,
          orientation: "RIGHT_SIDE",
          shot_size: "FULL_BODY",
        }}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    expect(screen.getByLabelText("人物朝向")).toHaveValue("LEFT_45");
    expect(screen.getByLabelText("人物景别")).toHaveValue("CLOSE_UP");
    expect(screen.getByLabelText("面部可见性")).toHaveValue("VISIBLE");
    expect(screen.getByLabelText("身体完整度")).toHaveValue("FACE_ONLY");
  });

  it("keeps user edits across parent re-renders with a changing busy callback", async () => {
    // 评审 M-1 回归锁定：宿主链路（App 内联回调 + busy 翻转重渲染）会
    // 让 onBusyChange 身份每次渲染变化，不得因此重载候选或清空用户已填特征。
    const { rerender } = render(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "UPPER_BODY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "HALF_BODY",
        }}
        onBusyChange={vi.fn()}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    const initialLoadCount = vi.mocked(getLatestProjectSourceFrames).mock.calls
      .length;
    fireEvent.change(screen.getByLabelText("人物朝向"), {
      target: { value: "LEFT_45" },
    });

    rerender(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "UPPER_BODY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "HALF_BODY",
        }}
        onBusyChange={vi.fn()}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(screen.getByLabelText("人物朝向")).toHaveValue("LEFT_45");
    expect(getLatestProjectSourceFrames).toHaveBeenCalledTimes(
      initialLoadCount,
    );
  });

  it("keeps confirmed features over incoming suggestions", async () => {
    vi.mocked(getLatestProjectSourceFrameSelection).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        id: "source-selection-1",
        kind: "source_frame_selection",
        payload: {
          source_frame_asset_id: "source-2",
          character_features: {
            orientation: "LEFT_45",
            shot_size: "HALF_BODY",
            face_visible: false,
            body_completeness: "UPPER_BODY",
          },
        },
      },
    });
    render(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "FULL_BODY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "FULL_BODY",
        }}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("当前候选源画面已确认。"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("人物朝向")).toHaveValue("LEFT_45");
    expect(screen.getByLabelText("人物景别")).toHaveValue("HALF_BODY");
    expect(screen.getByLabelText("面部可见性")).toHaveValue("HIDDEN");
    expect(screen.getByLabelText("身体完整度")).toHaveValue("UPPER_BODY");
  });
});
