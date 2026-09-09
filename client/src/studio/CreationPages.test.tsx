import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StudioAsset, StudioContextValue } from "./types";

const { useStudio } = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
}));

vi.mock("./context", () => ({ useStudio }));

// 复刻模块（模块①）：部分 mock api/live，其余保持原实现。
const replicaApi = vi.hoisted(() => ({
  getAssetDownloadUrl: vi.fn(),
  selectCharacterReferences: vi.fn(),
  startVideoAnalysis: vi.fn(),
  waitForAnalysisTask: vi.fn(),
  getLatestProjectShotCards: vi.fn(),
  getLatestProjectAnalysis: vi.fn(async () => ({ id: "av-x", payload: {} })),
  getLatestGenerationPrompt: vi.fn(),
  getLatestScriptVersion: vi.fn(),
  getLatestScriptRewriteTask: vi.fn<
    (...args: [string, string?, string?]) => Promise<unknown>
  >(async () => null),
  rewriteProjectScript: vi.fn(),
  waitForScriptRewriteTask: vi.fn(),
  getLatestProjectFirstFrameSelection: vi.fn(),
  getGenerationPriceQuote: vi.fn(),
  saveGenerationPrompt: vi.fn(),
  saveShotCards: vi.fn(),
}));
const replicaLive = vi.hoisted(() => ({
  readAudioDuration: vi.fn(),
  uploadOralAudioMaterial: vi.fn(),
  uploadWorkbenchSourceVideo: vi.fn(),
  uploadVideoMaterial: vi.fn(),
  runReplicaGeneration: vi.fn(),
  loadSavedScriptList: vi.fn(async () => []),
  validateOralAudioFile: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...replicaApi,
}));
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...replicaLive,
}));

// 人物替换（模块②）：叶子组件打桩，专测组合与置位链路。
vi.mock("../CharacterSelection", () => ({
  CharacterSelection: (props: {
    onVersionChange?: (s: unknown) => void;
    readOnly?: boolean;
  }) => (
    <button
      data-read-only={String(Boolean(props.readOnly))}
      type="button"
      onClick={() =>
        props.onVersionChange?.({
          character_version_id: "cv-1",
          character_snapshot: { identity: { id: "ident-1" } },
        })
      }
    >
      stub-选择人物
    </button>
  ),
}));
vi.mock("../SourceFrameSelection", () => ({
  SourceFrameSelection: (props: {
    onSelectionChange?: (s: unknown) => void;
    readOnly?: boolean;
  }) => (
    <>
      <button
        data-read-only={String(Boolean(props.readOnly))}
        type="button"
        onClick={() => props.onSelectionChange?.({ id: "sfv-1", payload: {} })}
      >
        stub-确认源画面
      </button>
      <button
        type="button"
        onClick={() => props.onSelectionChange?.({ id: "sfv-2", payload: {} })}
      >
        stub-切换源画面
      </button>
    </>
  ),
}));
vi.mock("../FirstFrameSelection", () => ({
  FirstFrameSelection: (props: {
    onSelectionChange?: (s: unknown) => void;
    referenceSelection?: { id: string } | null;
    readOnly?: boolean;
  }) => (
    <>
      {props.referenceSelection ? (
        <span>stub-参考匹配-{props.referenceSelection.id}</span>
      ) : null}
      <button
        data-read-only={String(Boolean(props.readOnly))}
        type="button"
        onClick={() =>
          props.onSelectionChange?.({
            id: "ffv-1",
            payload: {
              first_frame_candidates_version_id: "cand-1",
              first_frame_asset_id: "ff-asset-1",
            },
          })
        }
      >
        stub-确认置换首帧
      </button>
      <button
        type="button"
        onClick={() =>
          props.onSelectionChange?.({
            id: "ffv-2",
            payload: {
              first_frame_candidates_version_id: "cand-2",
              first_frame_asset_id: "ff-asset-1",
            },
          })
        }
      >
        stub-更新置换首帧版本
      </button>
      <button type="button" onClick={() => props.onSelectionChange?.(null)}>
        stub-撤销置换首帧
      </button>
    </>
  ),
}));

import {
  CopyPage,
  OralPage,
  ReplacementPage,
  ReplicaPage,
  VideoPage,
} from "./CreationPages";

function studio(
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: {
      page: "copy",
      draft: {
        id: "draft-1",
        ipId: "person-1",
        sourceId: "source-1",
        sourceAssetId: "source-1",
        projectId: "project-1",
        selectedShotId: "shot-2",
        originalImageId: "original-1",
        imageId: "target-1",
        firstFrameId: "frame-1",
        avatarId: "avatar-1",
        voiceId: "voice-1",
        audioId: "audio-1",
        script: {
          id: "script-1",
          title: "建房预算",
          original: "原始文案",
          text: "已确认的乡墅口播终稿",
          version: 3,
          confirmed: true,
        },
        prompt: "庭院镜头缓慢推进",
        referenceIds: ["reference-1"],
        resolution: "768P",
        ratio: "16:9",
        duration: 8,
        count: 1,
        frameConfirmed: true,
        style: "standard",
        subtitles: false,
        quoteRevision: 1,
      },
      savedScripts: [],
      favorites: [],
    },
    data: {
      people: [
        {
          id: "person-1",
          name: "张工",
          role: "乡墅设计师",
          version: 1,
          scope: "乡墅设计",
          audience: "自建房家庭",
          expression: "专业通俗",
          photoIds: [],
          avatars: [
            {
              id: "avatar-1",
              name: "设计室讲解",
              imageId: "target-1",
              ready: true,
              origin: "照片制作",
              duration: "00:42",
            },
          ],
          voices: [
            {
              id: "voice-1",
              name: "张工本人音色 V1",
              confirmed: true,
              isDefault: true,
            },
          ],
        },
      ],
      assets: [
        {
          id: "original-1",
          name: "原始画面",
          kind: "image",
          group: "项目",
          source: "视频复刻",
          saved: true,
        },
        {
          id: "target-1",
          name: "张工庭院讲解",
          kind: "image",
          group: "人物照片",
          personId: "person-1",
          source: "人物库",
          saved: true,
        },
        {
          id: "frame-1",
          name: "乡墅首帧",
          kind: "image",
          group: "项目",
          source: "人物置换",
          saved: true,
        },
        {
          id: "reference-1",
          name: "乡墅外观.jpg",
          kind: "image",
          group: "参考素材",
          source: "素材库",
          saved: true,
        },
        {
          id: "audio-1",
          name: "建房预算-录音.wav",
          kind: "audio",
          duration: "00:42",
          group: "完整口播音频",
          source: "素材库",
          saved: true,
        },
      ],
      videos: [],
      tasks: [],
      projects: [],
      errors: [],
      materials: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    },
    review: true,
    user: {
      id: "customer-1",
      username: "customer-1",
      display_name: "客户",
      role: "customer",
    },
    navigate: vi.fn(),
    patchDraft: vi.fn(),
    patchState: vi.fn(),
    updateData: vi.fn(),
    notify: vi.fn(),
    openPicker: vi.fn(),
    openLive: vi.fn(),
    requestGeneration: vi.fn(),
    saveDraft: vi.fn(),
    confirmFinalDraft: vi.fn(),
    extractScriptFromUpload: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  };
}

describe("V1.4 创作页面", () => {
  beforeEach(() => {
    useStudio.mockReset();
    replicaLive.loadSavedScriptList.mockReset().mockResolvedValue([]);
    replicaApi.getAssetDownloadUrl.mockReset();
    replicaApi.selectCharacterReferences.mockReset();
    replicaApi.getLatestScriptRewriteTask.mockReset();
    replicaApi.getLatestScriptRewriteTask.mockResolvedValue(null);
    replicaApi.rewriteProjectScript.mockReset();
    replicaApi.waitForScriptRewriteTask.mockReset();
    replicaLive.uploadVideoMaterial.mockReset();
    replicaLive.readAudioDuration.mockReset();
    replicaLive.uploadOralAudioMaterial.mockReset();
    replicaLive.validateOralAudioFile.mockReset();
    replicaLive.readAudioDuration.mockResolvedValue(42);
    replicaLive.validateOralAudioFile.mockReturnValue(undefined);
    replicaApi.getAssetDownloadUrl.mockImplementation(async (assetId) => ({
      url: `https://signed.example/${assetId}.png`,
    }));
  });

  it("文案终稿可带入数字人口播并保留同一草稿", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "用于数字人口播" }));
    expect(value.navigate).toHaveBeenCalledWith("oral", { returnTo: "copy" });
  });

  it("审计员可查看创作内容但文案、视频与口播提交控件只读", () => {
    const value = studio({
      review: false,
      user: {
        id: "auditor-1",
        username: "auditor-1",
        display_name: "审计员",
        role: "auditor",
      },
    });
    value.state.savedScripts = [
      {
        id: "saved-audit-script",
        title: "审计历史文案",
        original: "历史原稿",
        text: "历史终稿",
        version: 2,
        confirmed: true,
      },
    ];
    useStudio.mockReturnValue(value);
    const view = render(<CopyPage />);

    expect(screen.getByLabelText("二创文案")).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存版本" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认终稿" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "更换人物" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "更换人物" }));
    expect(value.openPicker).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
    const savedScript = screen.getByRole("button", {
      name: /审计历史文案/,
    });
    expect(savedScript).toBeDisabled();
    fireEvent.click(savedScript);
    expect(value.patchDraft).not.toHaveBeenCalled();

    value.state = { ...value.state, page: "video" };
    view.rerender(<VideoPage />);
    expect(screen.getByLabelText("提示词")).toBeDisabled();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();

    value.data.assets.push({
      id: "invalid-reference",
      name: "无效参考视频.mp4",
      kind: "video",
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-1", "invalid-reference"],
      },
    };
    view.rerender(<VideoPage />);
    expect(screen.getByRole("button", { name: "整理参考图" })).toBeDisabled();
    vi.mocked(value.patchDraft).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "整理参考图" }));
    expect(value.patchDraft).not.toHaveBeenCalled();

    value.state = { ...value.state, page: "oral" };
    view.rerender(<OralPage />);
    expect(screen.getByRole("button", { name: "生成口播视频" })).toBeDisabled();
    expect(value.saveDraft).not.toHaveBeenCalled();
    expect(value.confirmFinalDraft).not.toHaveBeenCalled();
    expect(value.requestGeneration).not.toHaveBeenCalled();
  });

  it("视频复刻入口复用已有成熟工作区", () => {
    const value = studio();
    value.state = { ...value.state, page: "replica" };
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "进入分镜工作区" }));
    expect(value.openLive).toHaveBeenCalledWith("analysis");
    expect(screen.getByRole("tab", { name: "视频复刻" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("视频复刻优先显示草稿来源，不被历史列表选择覆盖", () => {
    const value = studio();
    value.data.assets.push({
      id: "source-1",
      name: "当前草稿来源",
      kind: "video",
      group: "项目",
      source: "用户上传",
      saved: true,
    });
    value.data.videos.push({
      id: "history-video",
      title: "历史浏览视频",
      author: "作者",
      platform: "抖音",
      category: "建房预算",
      poster: "",
      duration: "00:30",
      likes: 1,
      collections: 0,
      shares: 0,
      description: "历史内容",
    });
    value.state = { ...value.state, selectedVideoId: "history-video" };
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    expect(screen.getByText(/当前草稿来源/)).toBeInTheDocument();
    expect(screen.queryByText(/历史浏览视频/)).not.toBeInTheDocument();
  });

  it("审核模式展示完整三镜头并用当前IP人物图作为目标首帧", () => {
    const value = studio();
    value.data.assets.push({
      id: "source-1",
      name: "来源视频",
      kind: "video",
      group: "项目",
      source: "上传",
      saved: true,
    });
    const target = value.data.assets.find((asset) => asset.id === "target-1");
    const frame = value.data.assets.find((asset) => asset.id === "frame-1");
    if (target) target.url = "/target-person.png";
    if (frame) frame.url = "/unrelated-frame.png";
    useStudio.mockReturnValue(value);

    const { container } = render(<ReplicaPage />);

    // 新复刻页：审核样例分镜以行卡呈现，Prompt 编辑区预填样例提示词。
    expect(container.querySelectorAll(".creation-shot-row")).toHaveLength(3);
    expect(
      (container.querySelector("textarea") as HTMLTextAreaElement).value.length,
    ).toBeGreaterThan(0);
  });

  it("文案工坊可直接更换参与二创的人物IP", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    fireEvent.click(screen.getByRole("button", { name: "更换人物" }));

    expect(value.openPicker).toHaveBeenCalledWith("person");
  });

  it("按当前项目、来源、已保存正文和人物IP发起异步二创", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, scriptEdited: false },
    };
    replicaApi.rewriteProjectScript.mockResolvedValue({
      id: "rewrite-1",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      ip_profile_snapshot: { profile_version: 7 },
      source_text: "已确认的乡墅口播终稿",
      status: "PENDING",
      result: null,
    });
    replicaApi.waitForScriptRewriteTask.mockResolvedValue({
      id: "rewrite-1",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      ip_profile_snapshot: { profile_version: 7 },
      source_text: "已确认的乡墅口播终稿",
      status: "SUCCEEDED",
      result: { rewritten_text: "张工定位的二创稿" },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    fireEvent.click(screen.getByRole("button", { name: "按 IP 二创" }));

    expect(replicaApi.rewriteProjectScript).toHaveBeenCalledWith(
      "project-1",
      "已确认的乡墅口播终稿",
      "person-1",
      "source-1",
      expect.any(String),
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        script: expect.objectContaining({
          text: "张工定位的二创稿",
          confirmed: false,
        }),
        scriptEdited: true,
      }),
    );
  });

  it.each([
    ["缺少来源项目", { projectId: undefined }, /来源项目/],
    ["缺少来源素材", { sourceId: undefined }, /来源视频/],
    ["缺少人物IP", { ipId: undefined }, /人物 IP/],
    [
      "正文为空",
      { script: { ...studio().state.draft.script, text: "" } },
      /待改写正文/,
    ],
    ["正文尚未保存", { scriptEdited: true }, /先保存当前编辑/],
  ])("%s时禁用二创并显示原因", (_name, draftPatch, message) => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, ...draftPatch },
    };
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    expect(screen.getByRole("button", { name: "按 IP 二创" })).toBeDisabled();
    expect(screen.getAllByText(message).length).toBeGreaterThan(0);
    expect(replicaApi.rewriteProjectScript).not.toHaveBeenCalled();
  });

  it("改写失败保留原稿并可重试", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, scriptEdited: false },
    };
    replicaApi.rewriteProjectScript
      .mockRejectedValueOnce(new Error("临时失败"))
      .mockResolvedValueOnce({
        id: "rewrite-retry",
        project_id: "project-1",
        identity_id: "person-1",
        source_asset_id: "source-1",
        source_text: "已确认的乡墅口播终稿",
        status: "SUCCEEDED",
        result: { rewritten_text: "重试成功稿" },
      });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    const rewrite = screen.getByRole("button", { name: "按 IP 二创" });

    fireEvent.click(rewrite);
    await waitFor(() => expect(value.notify).toHaveBeenCalledWith("临时失败"));
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(screen.getByLabelText("二创文案")).toHaveValue(
      "已确认的乡墅口播终稿",
    );

    fireEvent.click(rewrite);
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({ text: "重试成功稿" }),
        }),
      ),
    );
    expect(replicaApi.rewriteProjectScript).toHaveBeenCalledTimes(2);
    expect(replicaApi.rewriteProjectScript.mock.calls[1]?.[4]).toBe(
      replicaApi.rewriteProjectScript.mock.calls[0]?.[4],
    );
  });

  it("仅恢复同人物且同正文的未完成改写任务", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, scriptEdited: false },
    };
    replicaApi.getLatestScriptRewriteTask.mockResolvedValue({
      id: "rewrite-restored",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "已确认的乡墅口播终稿",
      status: "RUNNING",
      result: null,
    });
    replicaApi.waitForScriptRewriteTask.mockResolvedValue({
      id: "rewrite-restored",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "已确认的乡墅口播终稿",
      status: "SUCCEEDED",
      result: { rewritten_text: "恢复完成的同稿结果" },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    expect(replicaApi.getLatestScriptRewriteTask).toHaveBeenCalledWith(
      "project-1",
      "person-1",
      "source-1",
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({ text: "恢复完成的同稿结果" }),
        }),
      ),
    );
  });

  it("A到B再回A后旧人物请求不得覆盖当前稿", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    const oldResult = new Promise((resolve) => {
      resolveOld = resolve;
    });
    replicaApi.rewriteProjectScript.mockResolvedValue({
      id: "rewrite-old",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "已确认的乡墅口播终稿",
      status: "PENDING",
      result: null,
    });
    replicaApi.waitForScriptRewriteTask.mockReturnValue(oldResult);
    const original = studio({ review: false });
    original.state = {
      ...original.state,
      draft: { ...original.state.draft, scriptEdited: false },
    };
    let current = original;
    useStudio.mockImplementation(() => current);
    const view = render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "按 IP 二创" }));
    await waitFor(() =>
      expect(replicaApi.waitForScriptRewriteTask).toHaveBeenCalled(),
    );

    current = {
      ...original,
      state: {
        ...original.state,
        draft: { ...original.state.draft, ipId: "person-2" },
      },
    };
    view.rerender(<CopyPage />);
    current = original;
    view.rerender(<CopyPage />);
    resolveOld?.({
      id: "rewrite-old",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "已确认的乡墅口播终稿",
      status: "SUCCEEDED",
      result: { rewritten_text: "迟到的旧A稿" },
    });

    await Promise.resolve();
    expect(original.patchDraft).not.toHaveBeenCalled();
    expect(original.notify).not.toHaveBeenCalledWith(
      expect.stringContaining("完成"),
    );
  });

  it("同项目同人物同正文切换来源后不得恢复旧来源任务", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    replicaApi.getLatestScriptRewriteTask.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const sourceA = studio({ review: false });
    sourceA.state = {
      ...sourceA.state,
      draft: {
        ...sourceA.state.draft,
        sourceId: "source-a",
        sourceAssetId: "source-a",
        scriptEdited: false,
      },
    };
    const sourceB = {
      ...sourceA,
      state: {
        ...sourceA.state,
        draft: {
          ...sourceA.state.draft,
          sourceId: "source-b",
          sourceAssetId: "source-b",
        },
      },
    };
    let current = sourceA;
    useStudio.mockImplementation(() => current);
    const view = render(<CopyPage />);
    current = sourceB;
    view.rerender(<CopyPage />);
    resolveOld?.({
      id: "rewrite-source-a",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-a",
      source_text: "已确认的乡墅口播终稿",
      status: "RUNNING",
      result: null,
    });

    await Promise.resolve();
    expect(replicaApi.waitForScriptRewriteTask).not.toHaveBeenCalled();
    expect(sourceA.patchDraft).not.toHaveBeenCalled();
  });

  it("审核示例与只读账号不调用改写接口", () => {
    const reviewValue = studio({ review: true });
    useStudio.mockReturnValue(reviewValue);
    const view = render(<CopyPage />);
    expect(screen.getByRole("button", { name: "按 IP 二创" })).toBeDisabled();
    expect(screen.getByText(/审核示例/)).toBeInTheDocument();
    view.unmount();

    const auditor = studio({
      review: false,
      user: {
        id: "auditor-1",
        username: "audit",
        display_name: "审核",
        role: "auditor",
      },
    });
    auditor.state = {
      ...auditor.state,
      draft: { ...auditor.state.draft, scriptEdited: false },
    };
    useStudio.mockReturnValue(auditor);
    render(<CopyPage />);
    expect(screen.getByRole("button", { name: "按 IP 二创" })).toBeDisabled();
    expect(screen.getByText(/只读权限/)).toBeInTheDocument();
    expect(replicaApi.rewriteProjectScript).not.toHaveBeenCalled();
  });

  function replacementStudio() {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: "project-1" },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "替换测试项目",
          owner_user_id: "employee_1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    return value;
  }

  it("人物替换：无项目时引导先准备项目", () => {
    const value = studio({ review: false });
    value.state = { ...value.state, page: "replacement" };
    value.data = { ...value.data, projects: [] };
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);
    expect(screen.getByText(/先在视频复刻中准备好项目/)).toBeInTheDocument();
  });

  it("人物替换：确认置换首帧后置位草稿并可跳转视频生成", async () => {
    const value = replacementStudio();
    replicaApi.selectCharacterReferences.mockResolvedValue({
      id: "crs-1",
      payload: {},
    });
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledWith(
        "project-1",
        {
          character_version_id: "cv-1",
          source_frame_selection_version_id: "sfv-1",
        },
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-确认置换首帧" }));

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        firstFrameId: "ff-asset-1",
        firstFrameSelectionVersionId: "ffv-1",
        frameConfirmed: true,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "用于文/图生视频" }));
    expect(value.navigate).toHaveBeenCalledWith("video");
  });

  it("人物替换：同资产新确认版本会更新，撤销后清空交接", async () => {
    const value = replacementStudio();
    replicaApi.selectCharacterReferences.mockResolvedValue({
      id: "crs-1",
      payload: {},
    });
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-确认置换首帧" }));
    fireEvent.click(
      screen.getByRole("button", { name: "stub-更新置换首帧版本" }),
    );

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        firstFrameId: "ff-asset-1",
        firstFrameSelectionVersionId: "ffv-2",
        frameConfirmed: true,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-撤销置换首帧" }));
    expect(value.patchDraft).toHaveBeenLastCalledWith({
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      frameConfirmed: false,
    });
  });

  it("人物参考匹配失败后重试会发起第二次请求并呈现成功结果", async () => {
    const value = replacementStudio();
    replicaApi.selectCharacterReferences
      .mockRejectedValueOnce(new Error("匹配服务暂时不可用"))
      .mockResolvedValueOnce({ id: "crs-retry", payload: {} });
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    expect(await screen.findByText("匹配服务暂时不可用")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试匹配人物参考" }));

    expect(
      await screen.findByText("stub-参考匹配-crs-retry"),
    ).toBeInTheDocument();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("匹配服务暂时不可用")).toBeNull();
  });

  it("人物参考匹配防重复重试并忽略切换源帧前的迟到结果", async () => {
    let resolveRetry:
      | ((value: { id: string; payload: object }) => void)
      | undefined;
    replicaApi.selectCharacterReferences
      .mockRejectedValueOnce(new Error("首次匹配失败"))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRetry = resolve;
          }),
      )
      .mockResolvedValueOnce({ id: "crs-new-source", payload: {} });
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    const retry = await screen.findByRole("button", {
      name: "重试匹配人物参考",
    });
    fireEvent.click(retry);
    fireEvent.click(retry);
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(2),
    );

    fireEvent.click(screen.getByRole("button", { name: "stub-切换源画面" }));
    expect(
      await screen.findByText("stub-参考匹配-crs-new-source"),
    ).toBeInTheDocument();
    resolveRetry?.({ id: "crs-old-source", payload: {} });
    await Promise.resolve();

    expect(screen.queryByText("stub-参考匹配-crs-old-source")).toBeNull();
    expect(replicaApi.selectCharacterReferences).toHaveBeenLastCalledWith(
      "project-1",
      {
        character_version_id: "cv-1",
        source_frame_selection_version_id: "sfv-2",
      },
    );
  });

  it("同项目 ID 的数据对象刷新不会取消正在进行的匹配", async () => {
    let resolveMatch:
      | ((value: { id: string; payload: object }) => void)
      | undefined;
    replicaApi.selectCharacterReferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMatch = resolve;
        }),
    );
    const initial = replacementStudio();
    let current = initial;
    useStudio.mockImplementation(() => current);
    const view = render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1),
    );
    current = {
      ...current,
      data: {
        ...current.data,
        projects: current.data.projects.map((project) => ({ ...project })),
      },
    };
    view.rerender(<ReplacementPage />);
    resolveMatch?.({ id: "crs-same-project", payload: {} });

    expect(
      await screen.findByText("stub-参考匹配-crs-same-project"),
    ).toBeInTheDocument();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1);
  });

  it("源帧 A 切到 B 再返回 A 时会重新匹配并隔离前两次迟到响应", async () => {
    const resolvers: Array<(value: { id: string; payload: object }) => void> =
      [];
    replicaApi.selectCharacterReferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvers.push(resolve);
        }),
    );
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() => expect(resolvers).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "stub-切换源画面" }));
    await waitFor(() => expect(resolvers).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() => expect(resolvers).toHaveLength(3));

    resolvers[2]?.({ id: "crs-a-current", payload: {} });
    expect(
      await screen.findByText("stub-参考匹配-crs-a-current"),
    ).toBeInTheDocument();
    resolvers[0]?.({ id: "crs-a-old", payload: {} });
    resolvers[1]?.({ id: "crs-b-old", payload: {} });
    await Promise.resolve();

    expect(screen.queryByText("stub-参考匹配-crs-a-old")).toBeNull();
    expect(screen.queryByText("stub-参考匹配-crs-b-old")).toBeNull();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(3);
  });

  it("源帧 A 匹配已失败后切到 B 再返回 A 会发起新请求", async () => {
    replicaApi.selectCharacterReferences
      .mockRejectedValueOnce(new Error("A 匹配失败"))
      .mockResolvedValueOnce({ id: "crs-b", payload: {} })
      .mockResolvedValueOnce({ id: "crs-a-retry", payload: {} });
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    expect(await screen.findByText("A 匹配失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "stub-切换源画面" }));
    expect(await screen.findByText("stub-参考匹配-crs-b")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));

    expect(
      await screen.findByText("stub-参考匹配-crs-a-retry"),
    ).toBeInTheDocument();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(3);
  });

  it("同一源帧版本重复回调不重复匹配也不清除已确认首帧", async () => {
    let resolveMatch:
      | ((value: { id: string; payload: object }) => void)
      | undefined;
    replicaApi.selectCharacterReferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMatch = resolve;
        }),
    );
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-确认置换首帧" }));
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenLastCalledWith({
        firstFrameId: "ff-asset-1",
        firstFrameSelectionVersionId: "ffv-1",
        frameConfirmed: true,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1);
    expect(value.patchDraft).toHaveBeenLastCalledWith({
      firstFrameId: "ff-asset-1",
      firstFrameSelectionVersionId: "ffv-1",
      frameConfirmed: true,
    });

    resolveMatch?.({ id: "crs-same-source", payload: {} });
    expect(
      await screen.findByText("stub-参考匹配-crs-same-source"),
    ).toBeInTheDocument();
  });

  it("视频页按确认首帧 ID 恢复签名预览并交给生成请求", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId: "ff-asset-new",
        firstFrameSelectionVersionId: "ffv-new",
        frameConfirmed: true,
      },
    };
    value.data = {
      ...value.data,
      assets: value.data.assets.filter((asset) => asset.id !== "frame-1"),
    };
    value.updateData = vi.fn((update) => {
      value.data = update(value.data);
    });
    useStudio.mockReturnValue(value);

    render(<VideoPage />);

    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(
      await screen.findByRole("img", { name: "首帧预览" }),
    ).toHaveAttribute("src", "https://signed.example/ff-asset-new.png");
    expect(replicaApi.getAssetDownloadUrl).toHaveBeenCalledWith("ff-asset-new");
    expect(value.data.assets[0]).toMatchObject({
      id: "ff-asset-new",
      assetId: "ff-asset-new",
      kind: "image",
      source: "人物置换",
    });

    const generate = screen.getByRole("button", { name: "生成视频" });
    expect(generate).toBeEnabled();
    fireEvent.click(generate);
    expect(value.requestGeneration).toHaveBeenCalledWith("视频生成");
    expect(value.state.draft.firstFrameId).toBe("ff-asset-new");
  });

  it("首帧 ID 切换后忽略旧签名请求的迟到响应", async () => {
    let resolveOld: ((value: { url: string }) => void) | undefined;
    let resolveNew: ((value: { url: string }) => void) | undefined;
    replicaApi.getAssetDownloadUrl.mockImplementation(
      (assetId: string) =>
        new Promise((resolve) => {
          if (assetId === "ff-old") resolveOld = resolve;
          if (assetId === "ff-new") resolveNew = resolve;
        }),
    );
    const initial = studio({ review: false });
    initial.state = {
      ...initial.state,
      page: "video",
      draft: { ...initial.state.draft, firstFrameId: "ff-old" },
    };
    initial.data = {
      ...initial.data,
      assets: initial.data.assets.filter((asset) => asset.id !== "frame-1"),
    };
    let current = initial;
    current.updateData = vi.fn((update) => {
      current.data = update(current.data);
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);

    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, firstFrameId: "ff-new" },
      },
    };
    view.rerender(<VideoPage />);
    resolveNew?.({ url: "https://signed.example/new.png" });
    expect(
      await screen.findByRole("img", { name: "首帧预览" }),
    ).toHaveAttribute("src", "https://signed.example/new.png");

    resolveOld?.({ url: "https://signed.example/old.png" });
    await Promise.resolve();
    expect(screen.getByRole("img", { name: "首帧预览" })).toHaveAttribute(
      "src",
      "https://signed.example/new.png",
    );
    expect(current.data.assets.some((asset) => asset.id === "ff-old")).toBe(
      false,
    );
  });

  it("首帧签名读取失败后可重试并恢复预览", async () => {
    replicaApi.getAssetDownloadUrl
      .mockRejectedValueOnce(new Error("签名服务暂时不可用"))
      .mockResolvedValueOnce({ url: "https://signed.example/retried.png" });
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: { ...value.state.draft, firstFrameId: "ff-retry" },
    };
    value.data = {
      ...value.data,
      assets: value.data.assets.filter((asset) => asset.id !== "frame-1"),
    };
    value.data.assets.push({
      id: "ff-retry",
      assetId: "ff-retry",
      name: "待恢复的已确认首帧",
      kind: "image",
      group: "置换首帧",
      source: "人物置换",
      saved: true,
    });
    useStudio.mockReturnValue(value);

    render(<VideoPage />);

    expect(await screen.findByText("首帧预览加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试加载首帧" }));
    expect(
      await screen.findByRole("img", { name: "首帧预览" }),
    ).toHaveAttribute("src", "https://signed.example/retried.png");
    expect(replicaApi.getAssetDownloadUrl).toHaveBeenCalledTimes(2);
  });

  it("历史视频任务不遮挡新首帧加载失败与重试", async () => {
    replicaApi.getAssetDownloadUrl.mockRejectedValue(
      new Error("签名服务暂时不可用"),
    );
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId: "ff-after-task",
        videoBatchId: "old-video-task",
      },
    };
    value.data = {
      ...value.data,
      assets: value.data.assets.filter((asset) => asset.id !== "frame-1"),
      tasks: [
        {
          id: "old-video-task",
          title: "上一次视频生成",
          type: "视频生成",
          status: "completed",
          submitted: "今天 10:00",
        },
      ],
    };
    useStudio.mockReturnValue(value);

    render(<VideoPage />);

    expect(await screen.findByText("首帧预览加载失败")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重试加载首帧" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
  });

  it("视频生成在文图和多参考两种模式之间切换", () => {
    const value = studio({
      state: { ...studio().state, page: "video" },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);
    fireEvent.click(screen.getByRole("tab", { name: "参考生视频" }));
    expect(value.navigate).toHaveBeenCalledWith("reference");
  });

  it("文图与参考模式都将素材、参数和预览分为三栏", () => {
    const value = studio({
      state: { ...studio().state, page: "video" },
    });
    useStudio.mockReturnValue(value);
    const view = render(<VideoPage />);

    let grid = view.container.querySelector(".creation-video-grid");
    expect(grid?.children).toHaveLength(3);
    expect(grid?.querySelector(":scope > .creation-video-form")).not.toBeNull();
    const controls = grid?.querySelector(":scope > .creation-video-controls");
    expect(controls).not.toBeNull();
    expect(
      within(controls as HTMLElement).getByRole("button", {
        name: "生成视频",
      }),
    ).toBeInTheDocument();
    expect(
      grid?.querySelector(":scope > .creation-video-preview"),
    ).not.toBeNull();

    value.state = { ...value.state, page: "reference" };
    view.rerender(<VideoPage />);
    grid = view.container.querySelector(".creation-video-grid");
    expect(grid?.children).toHaveLength(3);
    expect(
      grid?.querySelector(":scope > .creation-video-controls"),
    ).not.toBeNull();
  });

  it("参考素材只接受图片并提示整理旧草稿中的无效类型", () => {
    const value = studio();
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-1", "reference-video", "reference-audio"],
      },
    };
    value.data.assets.push(
      {
        id: "reference-video",
        name: "庭院运镜.mp4",
        kind: "video",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
      {
        id: "reference-audio",
        name: "环境声.wav",
        kind: "audio",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
    );
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    expect(screen.getByText("图片 · 素材库")).toBeInTheDocument();
    expect(screen.queryByText("视频 · 素材库")).toBeNull();
    expect(screen.queryByText("音频 · 素材库")).toBeNull();
    expect(
      screen.getByText("参考图仅支持图片，旧草稿中有 2 项无效素材。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "整理参考图" }));
    expect(value.patchDraft).toHaveBeenCalledWith({
      referenceIds: ["reference-1"],
    });
  });

  it("参考图本机上传会在发请求前拒绝视频文件", () => {
    const value = studio({
      review: false,
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考图"), {
      target: {
        files: [new File(["video"], "庭院.mp4", { type: "video/mp4" })],
      },
    });

    expect(replicaLive.uploadVideoMaterial).not.toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("仅支持 PNG 或 JPEG 图片。");
  });

  it("参考图上传完成时按最新草稿追加而不复活已移除引用", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: ["reference-1"] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考图"), {
      target: {
        files: [new File(["image"], "庭院.jpg", { type: "image/jpeg" })],
      },
    });
    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, referenceIds: ["reference-b"] },
      },
      data: {
        ...current.data,
        assets: [
          ...current.data.assets,
          {
            id: "reference-b",
            name: "参考图 B.jpg",
            kind: "image",
            group: "参考素材",
            source: "素材库",
            saved: true,
          },
        ],
      },
    };
    view.rerender(<VideoPage />);
    resolveUpload?.({
      id: "reference-new",
      name: "庭院.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });

    await waitFor(() =>
      expect(current.patchDraft).toHaveBeenCalledWith({
        referenceIds: ["reference-b", "reference-new"],
      }),
    );
  });

  it("参考图上传期间达到上限时不再回填迟到结果", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 1,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);
    fireEvent.change(screen.getByLabelText("上传参考图"), {
      target: {
        files: [new File(["image"], "迟到.jpg", { type: "image/jpeg" })],
      },
    });
    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, referenceIds: ["reference-1"] },
      },
    };
    view.rerender(<VideoPage />);
    vi.mocked(current.patchDraft).mockClear();
    resolveUpload?.({
      id: "reference-late",
      name: "迟到.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });

    await waitFor(() =>
      expect(current.notify).toHaveBeenCalledWith("当前最多选择 1 张参考图。"),
    );
    expect(current.patchDraft).not.toHaveBeenCalled();
  });

  it("离开参考生视频后忽略仍在上传的参考图", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);
    fireEvent.change(screen.getByLabelText("上传参考图"), {
      target: {
        files: [new File(["image"], "离开.jpg", { type: "image/jpeg" })],
      },
    });
    current = { ...current, state: { ...current.state, page: "video" } };
    view.rerender(<VideoPage />);
    vi.mocked(current.patchDraft).mockClear();
    resolveUpload?.({
      id: "reference-abandoned",
      name: "离开.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    await Promise.resolve();

    expect(current.patchDraft).not.toHaveBeenCalled();
  });

  it("切换草稿后不把旧草稿仍在上传的参考图写入新草稿", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);
    fireEvent.change(screen.getByLabelText("上传参考图"), {
      target: {
        files: [new File(["image"], "旧草稿.jpg", { type: "image/jpeg" })],
      },
    });
    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, id: "draft-2" },
      },
    };
    view.rerender(<VideoPage />);
    vi.mocked(current.patchDraft).mockClear();
    resolveUpload?.({
      id: "reference-old-draft",
      name: "旧草稿.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    await Promise.resolve();

    expect(current.patchDraft).not.toHaveBeenCalled();
  });

  it("音频驱动不显示终稿、声音、TTS和模板字段", () => {
    const value = studio({
      state: { ...studio().state, page: "oral-audio" },
    });
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    expect(screen.getByText("口播音频")).toBeInTheDocument();
    expect(screen.queryByText("口播文案")).not.toBeInTheDocument();
    expect(screen.queryByText("声音档案")).not.toBeInTheDocument();
    expect(screen.queryByText("网感模板")).not.toBeInTheDocument();
    expect(screen.queryByText("文字转语音")).not.toBeInTheDocument();
  });

  it("口播模式切换只占左侧输入栏，不下推右侧人物预览", () => {
    useStudio.mockReturnValue(studio());

    const { container } = render(<OralPage />);

    expect(
      container.querySelector(
        ".creation-oral-grid > .creation-oral-left > .studio-tabs",
      ),
    ).not.toBeNull();
  });

  it("音频驱动上传完成后写入真实云资产且不需要TTS声音", async () => {
    const value = studio({
      state: {
        ...studio().state,
        page: "oral-audio",
        draft: { ...studio().state.draft, audioId: "target-1" },
      },
      review: false,
    });
    replicaLive.uploadOralAudioMaterial.mockImplementation(
      async (_file, purpose, duration, onProgress) => {
        expect(purpose).toBe("oral_audio");
        expect(duration).toBe(42);
        onProgress(60);
        return {
          id: "uploaded-audio",
          name: "完整口播.mp3",
          kind: "audio",
          duration: "00:42",
          group: "完整口播音频",
          source: "我的上传",
          saved: true,
          allowedUses: ["oral_audio"],
        };
      },
    );
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    expect(screen.getByRole("button", { name: "生成口播视频" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("选择口播音频"), {
      target: {
        files: [new File(["ID3audio"], "完整口播.mp3", { type: "audio/mpeg" })],
      },
    });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        audioId: "uploaded-audio",
        voiceId: undefined,
      }),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.openPicker).not.toHaveBeenCalled();
  });

  it("取消口播音频上传后忽略迟到完成结果", async () => {
    const value = studio({
      state: { ...studio().state, page: "oral-audio" },
      review: false,
    });
    let finishUpload!: (asset: StudioAsset) => void;
    replicaLive.uploadOralAudioMaterial.mockImplementation(
      () =>
        new Promise<StudioAsset>((resolve) => {
          finishUpload = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    fireEvent.change(screen.getByLabelText("选择口播音频"), {
      target: {
        files: [new File(["ID3audio"], "完整口播.mp3", { type: "audio/mpeg" })],
      },
    });
    expect(
      await screen.findByRole("button", { name: "取消上传" }),
    ).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "取消上传" }));
    finishUpload({
      id: "late-audio",
      name: "迟到音频.mp3",
      kind: "audio",
      group: "完整口播音频",
      source: "我的上传",
      saved: true,
    });
    await Promise.resolve();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.updateData).not.toHaveBeenCalled();
  });

  it("错误格式和上传失败不会写入口播草稿", async () => {
    const value = studio({
      state: { ...studio().state, page: "oral-audio" },
      review: false,
    });
    replicaLive.validateOralAudioFile.mockReturnValueOnce("仅支持 MP3 音频。");
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    fireEvent.change(screen.getByLabelText("选择口播音频"), {
      target: { files: [new File(["bad"], "错误.wav", { type: "audio/wav" })] },
    });
    expect(value.notify).toHaveBeenCalledWith("仅支持 MP3 音频。");
    expect(replicaLive.uploadOralAudioMaterial).not.toHaveBeenCalled();

    replicaLive.validateOralAudioFile.mockReturnValue(undefined);
    replicaLive.uploadOralAudioMaterial.mockRejectedValue(
      new Error("上传失败"),
    );
    fireEvent.change(screen.getByLabelText("选择口播音频"), {
      target: {
        files: [new File(["ID3audio"], "失败.mp3", { type: "audio/mpeg" })],
      },
    });
    await waitFor(() => expect(value.notify).toHaveBeenCalledWith("上传失败"));
    expect(value.patchDraft).not.toHaveBeenCalled();
  });

  it("更换口播IP只打开人物选择器，由统一草稿层执行防串人清理", () => {
    const value = studio({
      state: { ...studio().state, page: "oral" },
    });
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    fireEvent.click(screen.getByRole("button", { name: "更换 IP" }));
    expect(value.openPicker).toHaveBeenCalledWith("person");
  });

  it("文案口播正文只读，修改时返回唯一文案工坊", () => {
    const value = studio({
      state: { ...studio().state, page: "oral" },
    });
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    expect(
      screen.queryByRole("textbox", { name: "口播文案" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "去文案工坊修改" }));
    expect(value.navigate).toHaveBeenCalledWith("copy", { returnTo: "oral" });
  });

  it("口播分身名称已含人物名时不重复拼接", () => {
    const value = studio();
    value.data.people[0] = {
      ...value.data.people[0],
      avatars: [
        {
          ...value.data.people[0].avatars[0],
          name: "张工 · 设计室讲解",
        },
      ],
    };
    useStudio.mockReturnValue(value);

    render(<OralPage />);

    expect(screen.getAllByText("张工 · 设计室讲解")).toHaveLength(2);
    expect(screen.queryByText("张工 · 张工 · 设计室讲解")).toBeNull();
  });

  it("补充声音与分身时带入当前IP和当前口播模式", () => {
    const missingVoice = studio();
    missingVoice.state = {
      ...missingVoice.state,
      page: "oral",
      draft: { ...missingVoice.state.draft, voiceId: undefined },
    };
    useStudio.mockReturnValue(missingVoice);
    const view = render(<OralPage />);
    fireEvent.click(screen.getByRole("button", { name: "管理声音" }));
    expect(missingVoice.navigate).toHaveBeenCalledWith("person-voices", {
      returnTo: "oral",
      selectedPersonId: "person-1",
    });

    view.unmount();
    const missingAvatar = studio();
    missingAvatar.state = {
      ...missingAvatar.state,
      page: "oral-audio",
      draft: { ...missingAvatar.state.draft, avatarId: undefined },
    };
    useStudio.mockReturnValue(missingAvatar);
    render(<OralPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "去人物库制作口播分身" }),
    );
    expect(missingAvatar.navigate).toHaveBeenCalledWith("person-avatars", {
      returnTo: "oral-audio",
      selectedPersonId: "person-1",
    });
  });
});

describe("视频复刻（模块①）", () => {
  beforeEach(() => {
    useStudio.mockReset();
    replicaApi.getLatestProjectShotCards.mockReset();
    replicaApi.getLatestProjectAnalysis.mockReset();
    replicaApi.getLatestGenerationPrompt.mockReset();
    replicaApi.getLatestScriptVersion.mockReset();
    replicaApi.saveGenerationPrompt.mockReset();
    replicaApi.getGenerationPriceQuote.mockReset();
    replicaApi.getLatestProjectShotCards.mockResolvedValue(null);
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-empty",
      payload: {},
    });
    replicaApi.getLatestGenerationPrompt.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: null,
    });
    replicaApi.getLatestScriptVersion.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: null,
    });
    replicaApi.getGenerationPriceQuote.mockResolvedValue({
      resolution: "768P",
      duration_seconds: 4,
      quantity: 1,
      unit_price_fen_per_second: 120,
      estimated_seconds: 4,
      estimated_price_fen: 480,
    });
  });

  const shot = {
    shot_id: "s1",
    start_time: 0,
    end_time: 8,
    shot_type: "中景",
    composition: "",
    camera_motion: "推进",
    subject: "院落",
    action: "镜头缓推庭院",
    scene: "乡墅庭院",
    spoken_text: "这栋房子的采光设计",
    transition: "切镜",
  };

  function replicaStudio() {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: "project-1",
        sourceAssetId: "asset-1",
        prompt: "",
      },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "复刻测试项目",
          owner_user_id: "employee_1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    return value;
  }

  function mockAnalysisSuccess(options: { existingShotCards?: boolean } = {}) {
    replicaApi.startVideoAnalysis.mockResolvedValue({
      id: "task-1",
      status: "RUNNING",
    });
    replicaApi.waitForAnalysisTask.mockResolvedValue({
      id: "task-1",
      status: "SUCCEEDED",
    });
    replicaApi.getLatestProjectShotCards.mockResolvedValue(
      options.existingShotCards
        ? {
            id: "scv-existing",
            payload: {
              source_analysis_version_id: "av-1",
              duration_seconds: 8,
              shots: [shot],
            },
          }
        : null,
    );
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-1",
      payload: {
        analysis: {
          summary: "庭院复刻",
          duration_seconds: 8,
          original_script: "这栋房子的采光设计非常好",
          shots: [shot],
        },
      },
    });
    replicaApi.saveShotCards.mockResolvedValue({
      id: "scv-1",
      payload: {
        source_analysis_version_id: "av-1",
        duration_seconds: 4,
        shots: [shot],
      },
    });
  }

  function mockSavedReplicaVersions() {
    replicaApi.getLatestProjectShotCards.mockResolvedValue({
      id: "scv-saved",
      payload: {
        source_analysis_version_id: "av-saved",
        duration_seconds: 4,
        shots: [shot],
      },
    });
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-saved",
      payload: {
        analysis: {
          original_script: "保存的分析原文",
          shots: [shot],
        },
      },
    });
    replicaApi.getLatestGenerationPrompt.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: {
        id: "prompt-saved",
        version_number: 4,
        payload: { prompt_text: "保存的复刻 Prompt" },
      },
    });
    replicaApi.getLatestScriptVersion.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: {
        id: "script-saved",
        version_number: 3,
        payload: { full_text: "保存的二创终稿" },
      },
    });
  }

  it("返回复刻页时自动恢复分镜、脚本与 Prompt，不重新发起拆解", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        prompt: "",
        script: { ...value.state.draft.script, text: "", confirmed: false },
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    expect(await screen.findByText(/院落/)).toBeInTheDocument();
    expect(screen.getByLabelText("拆解 Prompt")).toHaveValue(
      "保存的复刻 Prompt",
    );
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: "保存的复刻 Prompt",
        script: expect.objectContaining({
          id: "script-saved",
          text: "保存的二创终稿",
          version: 3,
        }),
      }),
    );
    expect(replicaApi.startVideoAnalysis).not.toHaveBeenCalled();
  });

  it("返回复刻页时保留当前项目未保存的工作区草稿", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        prompt: "尚未保存的 Prompt 编辑",
        promptEdited: true,
        script: {
          ...value.state.draft.script,
          text: "尚未保存的文案编辑",
          version: 7,
        },
        scriptEdited: true,
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("拆解 Prompt")).toHaveValue(
      "尚未保存的 Prompt 编辑",
    );
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: "尚未保存的 Prompt 编辑",
        script: expect.objectContaining({
          text: "尚未保存的文案编辑",
          version: 7,
        }),
      }),
    );
  });

  it("StrictMode 重放 effect 后仍能完成首次版本恢复", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        prompt: "",
        script: { ...value.state.draft.script, text: "", confirmed: false },
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(
      <StrictMode>
        <ReplicaPage />
      </StrictMode>,
    );

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/正在读取已保存/)).toBeNull();
  });

  it("恢复请求等待期间主动清空 Prompt 时不被旧版本覆盖", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, prompt: "" },
    };
    mockSavedReplicaVersions();
    let resolveShots:
      | ((
          value: Awaited<
            ReturnType<typeof replicaApi.getLatestProjectShotCards>
          >,
        ) => void)
      | undefined;
    replicaApi.getLatestProjectShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveShots = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    const textarea = screen.getByLabelText("拆解 Prompt");
    fireEvent.change(textarea, { target: { value: "加载期间的编辑" } });
    fireEvent.change(textarea, { target: { value: "" } });
    resolveShots?.({
      id: "scv-delayed",
      payload: {
        source_analysis_version_id: "av-saved",
        duration_seconds: 8,
        shots: [shot],
      },
    });

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(textarea).toHaveValue("");
    expect(value.patchDraft).toHaveBeenLastCalledWith(
      expect.objectContaining({ prompt: "", promptEdited: true }),
    );
  });

  it("主动清空 Prompt 后离页再返回仍保留空草稿", async () => {
    const first = replicaStudio();
    useStudio.mockReturnValue(first);
    const firstView = render(<ReplicaPage />);
    const textarea = screen.getByLabelText("拆解 Prompt");
    fireEvent.change(textarea, { target: { value: "准备清空" } });
    fireEvent.change(textarea, { target: { value: "" } });
    expect(first.patchDraft).toHaveBeenLastCalledWith({
      prompt: "",
      promptEdited: true,
    });
    firstView.unmount();

    const reopened = replicaStudio();
    reopened.state = {
      ...reopened.state,
      draft: {
        ...reopened.state.draft,
        prompt: "",
        promptEdited: true,
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(reopened);
    render(<ReplicaPage />);

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("拆解 Prompt")).toHaveValue("");
    expect(reopened.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: "", promptEdited: true }),
    );
  });

  it("同项目本地文案保留标题、来源原文和主动清空内容", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        script: {
          ...value.state.draft.script,
          title: "本地改过的作品名",
          original: "音频提取的来源原文",
          text: "",
          version: 9,
        },
        scriptEdited: true,
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        script: expect.objectContaining({
          title: "本地改过的作品名",
          original: "音频提取的来源原文",
          text: "",
          version: 9,
        }),
        scriptEdited: true,
      }),
    );
  });

  it("空态挂载后云端编辑稿迟到，版本恢复不覆盖云端 Prompt", async () => {
    const initial = replicaStudio();
    initial.state = {
      ...initial.state,
      draft: { ...initial.state.draft, prompt: "", promptEdited: false },
    };
    let current = initial;
    let resolveShots:
      | ((
          value: Awaited<
            ReturnType<typeof replicaApi.getLatestProjectShotCards>
          >,
        ) => void)
      | undefined;
    mockSavedReplicaVersions();
    replicaApi.getLatestProjectShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveShots = resolve;
        }),
    );
    useStudio.mockImplementation(() => current);
    const view = render(<ReplicaPage />);

    current = {
      ...initial,
      state: {
        ...initial.state,
        draft: {
          ...initial.state.draft,
          prompt: "云端迟到的本地编辑稿",
          promptEdited: true,
        },
      },
    };
    view.rerender(<ReplicaPage />);
    resolveShots?.({
      id: "scv-cloud-late",
      payload: {
        source_analysis_version_id: "av-saved",
        duration_seconds: 8,
        shots: [shot],
      },
    });

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("拆解 Prompt")).toHaveValue(
      "云端迟到的本地编辑稿",
    );
    expect(current.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: "云端迟到的本地编辑稿",
        promptEdited: true,
      }),
    );
  });

  it("项目尚无分析版本时按空态恢复，不显示读取失败", async () => {
    const value = replicaStudio();
    const notFound = Object.assign(
      new Error("Project has no analysis version."),
      {
        status: 404,
        code: "ANALYSIS_NOT_FOUND",
      },
    );
    replicaApi.getLatestProjectAnalysis.mockRejectedValue(notFound);
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() =>
      expect(replicaApi.getLatestProjectAnalysis).toHaveBeenCalledWith(
        "project-1",
      ),
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      screen.getByRole("button", { name: "启动 AI 拆解" }),
    ).toBeInTheDocument();
  });

  it("恢复失败时显示重试，重试成功后载入原版本", async () => {
    const value = replicaStudio();
    replicaApi.getLatestProjectShotCards
      .mockRejectedValueOnce(new Error("读取超时"))
      .mockResolvedValueOnce({
        id: "scv-retry",
        payload: {
          source_analysis_version_id: "av-retry",
          duration_seconds: 8,
          shots: [shot],
        },
      });
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-retry",
      payload: { analysis: { original_script: "重试恢复", shots: [shot] } },
    });
    replicaApi.getLatestGenerationPrompt.mockResolvedValue({
      stale: true,
      stale_reasons: ["shot cards changed"],
      version: null,
    });
    replicaApi.getLatestScriptVersion.mockResolvedValue({
      stale: true,
      stale_reasons: ["analysis changed"],
      version: null,
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    expect(await screen.findByText(/读取超时/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试读取历史分镜" }));

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(2);
    expect(replicaApi.startVideoAnalysis).not.toHaveBeenCalled();
  });

  async function openReplicaAndAnalyze(
    options: { existingShotCards?: boolean } = {},
  ) {
    const value = replicaStudio();
    mockAnalysisSuccess(options);
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));
    await screen.findAllByText(/院落/);
    return value;
  }

  it("上传参考视频后写入草稿的项目与来源", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: undefined,
        sourceId: undefined,
      },
    };
    value.data = { ...value.data, projects: [] };
    replicaLive.uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "project-upload-1",
      assetId: "asset-upload-1",
      project: {
        id: "project-upload-1",
        owner_user_id: "user-1",
        name: "a",
        status: "DRAFT",
        reference_asset_id: "asset-upload-1",
        reference_upload_status: "READY",
        analysis_status: "NOT_READY",
      },
      asset: {
        id: "asset-upload-1",
        name: "a · 来源视频",
        kind: "video",
        group: "a",
        source: "项目上传",
        saved: true,
      },
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(screen.getByRole("button", { name: "上传参考视频" }));
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    expect(input).not.toBeNull();
    Object.defineProperty(input, "files", { value: [new File([], "a.mp4")] });
    fireEvent.change(input);

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-upload-1",
          sourceId: "asset-upload-1",
          sourceAssetId: "asset-upload-1",
          prompt: "",
          script: expect.objectContaining({ text: "" }),
        }),
      ),
    );
    expect(value.updateData).toHaveBeenCalledOnce();
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    const updated = update(value.data);
    expect(updated.projects).toContainEqual(
      expect.objectContaining({ id: "project-upload-1" }),
    );
    expect(updated.assets).toContainEqual(
      expect.objectContaining({ id: "asset-upload-1" }),
    );
    expect(screen.queryByText("先导入参考视频")).toBeNull();
    expect(
      screen.getByRole("button", { name: "启动 AI 拆解" }),
    ).toBeInTheDocument();
  });

  it("上传 B 项目时清空 A 项目的 Prompt，并阻止重渲染重启 A 的恢复", async () => {
    const initial = replicaStudio();
    initial.state = {
      ...initial.state,
      draft: {
        ...initial.state.draft,
        prompt: "A 项目的 Prompt",
        script: { ...initial.state.draft.script, text: "A 项目的文案" },
      },
    };
    let current = initial;
    let resolveRestore:
      | ((
          value: Awaited<
            ReturnType<typeof replicaApi.getLatestProjectShotCards>
          >,
        ) => void)
      | undefined;
    let resolveUpload:
      | ((
          value: Awaited<
            ReturnType<typeof replicaLive.uploadWorkbenchSourceVideo>
          >,
        ) => void)
      | undefined;
    replicaApi.getLatestProjectShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRestore = resolve;
        }),
    );
    replicaLive.uploadWorkbenchSourceVideo.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    useStudio.mockImplementation(() => current);
    const view = render(<ReplicaPage />);
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "更换来源视频" }));
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File([], "b.mp4")] });
    fireEvent.change(input);

    current = { ...initial, patchDraft: vi.fn() };
    view.rerender(<ReplicaPage />);
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(1);

    resolveUpload?.({ projectId: "project-b", assetId: "asset-b" });
    await waitFor(() =>
      expect(initial.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-b",
          prompt: "",
          script: expect.objectContaining({ text: "" }),
        }),
      ),
    );
    resolveRestore?.({
      id: "scv-a-late",
      payload: {
        source_analysis_version_id: "av-a",
        duration_seconds: 8,
        shots: [shot],
      },
    });
    await Promise.resolve();

    expect(current.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "project-1" }),
    );
    expect(screen.getByLabelText("拆解 Prompt")).toHaveValue("");
  });

  it("选择已有项目后忽略仍在上传的旧来源", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: undefined,
        sourceId: undefined,
      },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          owner_user_id: "user-1",
          name: "已有项目",
          status: "READY",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    let resolveUpload:
      | ((value: { projectId: string; assetId: string }) => void)
      | undefined;
    replicaLive.uploadWorkbenchSourceVideo.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(screen.getByRole("button", { name: "上传参考视频" }));
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File([], "a.mp4")] });
    fireEvent.change(input);
    fireEvent.change(screen.getByLabelText("选择已有项目"), {
      target: { value: "project-1" },
    });

    expect(
      replicaLive.uploadWorkbenchSourceVideo.mock.calls[0]?.[2]?.aborted,
    ).toBe(true);
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-1",
          sourceId: "asset-1",
          sourceAssetId: "asset-1",
        }),
      ),
    );

    resolveUpload?.({ projectId: "late-project", assetId: "late-asset" });
    await Promise.resolve();
    expect(value.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "late-project" }),
    );
  });

  it("启动 AI 拆解后生成分镜行与逐镜头 Prompt", async () => {
    const value = await openReplicaAndAnalyze();

    expect(screen.getAllByText(/院落/).length).toBeGreaterThan(0);
    const textarea = screen.getByLabelText(
      "拆解 Prompt",
    ) as HTMLTextAreaElement;
    expect(textarea.value).toContain("【镜头 1】");
    expect(textarea.value).toContain("【原片口播稿】");
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: expect.stringContaining("【镜头 1】"),
        promptEdited: false,
      }),
    );
  });

  it("编辑后的 Prompt 可保存为用户自定义提示词", async () => {
    const value = await openReplicaAndAnalyze();
    replicaApi.saveGenerationPrompt.mockResolvedValue({ id: "sp-1" });

    const textarea = screen.getByLabelText("拆解 Prompt");
    fireEvent.change(textarea, { target: { value: "我改过的复刻提示词" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.change(screen.getByLabelText("自定义提示词名称"), {
      target: { value: "我的复刻" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() =>
      expect(replicaApi.saveGenerationPrompt).toHaveBeenCalledWith(
        "project-1",
        { name: "我的复刻", prompt_text: "我改过的复刻提示词" },
      ),
    );
    expect(value.notify).toHaveBeenCalledWith(
      expect.stringContaining("我的提示词"),
    );
  });

  it("保存 A 期间继续编辑 B，A 的迟到响应不清除 B 的编辑标记", async () => {
    const value = replicaStudio();
    let resolveSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveGenerationPrompt.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    const textarea = screen.getByLabelText("拆解 Prompt");
    fireEvent.change(textarea, { target: { value: "准备保存的 A" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    fireEvent.change(textarea, { target: { value: "继续编辑的 B" } });
    resolveSave?.({ id: "prompt-a" });

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "提交时的 Prompt 已保存，当前修改仍需再次保存。",
      ),
    );
    expect(value.patchDraft).not.toHaveBeenCalledWith({ promptEdited: false });
    expect(textarea).toHaveValue("继续编辑的 B");
  });

  it("新 Prompt 保存成功后，迟到的历史恢复只补分镜且不覆盖新内容", async () => {
    const value = replicaStudio();
    let resolveShots:
      | ((
          value: Awaited<
            ReturnType<typeof replicaApi.getLatestProjectShotCards>
          >,
        ) => void)
      | undefined;
    mockSavedReplicaVersions();
    replicaApi.getLatestProjectShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveShots = resolve;
        }),
    );
    replicaApi.saveGenerationPrompt.mockResolvedValue({ id: "prompt-new" });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    const textarea = screen.getByLabelText("拆解 Prompt");
    fireEvent.change(textarea, { target: { value: "刚保存的新 Prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({ promptEdited: false }),
    );

    resolveShots?.({
      id: "scv-old-late",
      payload: {
        source_analysis_version_id: "av-saved",
        duration_seconds: 8,
        shots: [shot],
      },
    });

    expect((await screen.findAllByText(/院落/)).length).toBeGreaterThan(0);
    expect(textarea).toHaveValue("刚保存的新 Prompt");
    expect(value.patchDraft).toHaveBeenLastCalledWith(
      expect.objectContaining({
        prompt: "刚保存的新 Prompt",
        promptEdited: false,
      }),
    );
    expect(screen.queryByText(/正在读取已保存/)).toBeNull();
  });

  it("保存响应在切换项目后返回，不清除新项目编辑标记", async () => {
    const value = replicaStudio();
    let current = value;
    let resolveSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveGenerationPrompt.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    useStudio.mockImplementation(() => current);
    const view = render(<ReplicaPage />);

    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    current = {
      ...value,
      state: {
        ...value.state,
        draft: { ...value.state.draft, projectId: "project-2" },
      },
      data: {
        ...value.data,
        projects: [
          ...value.data.projects,
          {
            ...value.data.projects[0],
            id: "project-2",
            name: "第二项目",
            reference_asset_id: "asset-2",
          },
        ],
      },
    };
    view.rerender(<ReplicaPage />);
    resolveSave?.({ id: "prompt-a" });
    await Promise.resolve();

    expect(value.patchDraft).not.toHaveBeenCalledWith({ promptEdited: false });
  });

  it("保存响应在页面卸载后返回，不再修改工作区标记", async () => {
    const value = replicaStudio();
    let resolveSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveGenerationPrompt.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);

    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    view.unmount();
    resolveSave?.({ id: "prompt-a" });
    await Promise.resolve();

    expect(value.patchDraft).not.toHaveBeenCalledWith({ promptEdited: false });
  });

  it("送生成：无确认首帧时引导到人物置换", async () => {
    const value = await openReplicaAndAnalyze();
    replicaApi.getLatestProjectFirstFrameSelection.mockResolvedValue({
      version: null,
      stale: false,
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并送生成" }),
    );

    await waitFor(() => expect(value.notify).toHaveBeenCalled());
    expect(
      vi.mocked(value.notify).mock.calls.map((call) => String(call[0])),
    ).toContainEqual(
      expect.stringContaining("请先到「人物置换」生成并确认首帧"),
    );
    expect(replicaLive.runReplicaGeneration).not.toHaveBeenCalled();
  });

  it("送生成：报价失败时禁止建批并可重试取得当前参数报价", async () => {
    replicaApi.getGenerationPriceQuote
      .mockRejectedValueOnce(new Error("复刻报价暂不可用"))
      .mockResolvedValueOnce({
        resolution: "768P",
        duration_seconds: 4,
        quantity: 1,
        unit_price_fen_per_second: 120,
        estimated_seconds: 4,
        estimated_price_fen: 480,
      });
    await openReplicaAndAnalyze();

    expect(await screen.findByText("复刻报价暂不可用")).toBeInTheDocument();
    const submit = screen.getByRole("button", {
      name: "确认费用并送生成",
    });
    expect(submit).toBeDisabled();
    expect(replicaLive.runReplicaGeneration).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重新获取复刻报价" }));

    expect(await screen.findByText(/4\.80 元/)).toBeInTheDocument();
    expect(submit).toBeEnabled();
    expect(replicaApi.getGenerationPriceQuote).toHaveBeenCalledTimes(2);
  });

  it("送生成准备期间离开页面后不再发起旧项目付费请求", async () => {
    const value = replicaStudio();
    mockAnalysisSuccess();
    let resolveSelection:
      | ((value: {
          version: {
            payload: {
              first_frame_candidates_version_id: string;
              first_frame_asset_id: string;
            };
          };
          stale: boolean;
        }) => void)
      | undefined;
    replicaApi.getLatestProjectFirstFrameSelection.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSelection = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));
    await screen.findAllByText(/院落/);
    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并送生成" }),
    );
    await waitFor(() =>
      expect(replicaApi.getLatestProjectFirstFrameSelection).toHaveBeenCalled(),
    );

    view.unmount();
    resolveSelection?.({
      version: {
        payload: {
          first_frame_candidates_version_id: "cand-old",
          first_frame_asset_id: "ff-old",
        },
      },
      stale: false,
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(replicaLive.runReplicaGeneration).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalledWith("tasks");
  });

  it("送生成：有确认首帧时走完整管线建批", async () => {
    const value = await openReplicaAndAnalyze();
    replicaApi.getLatestProjectFirstFrameSelection.mockResolvedValue({
      version: {
        payload: {
          first_frame_candidates_version_id: "cand-1",
          first_frame_asset_id: "ff-1",
        },
      },
      stale: false,
    });
    replicaLive.runReplicaGeneration.mockResolvedValue({
      id: "batch-9",
      status: "QUEUED",
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并送生成" }),
    );

    await waitFor(() =>
      expect(replicaLive.runReplicaGeneration).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          shotCardVersionId: "scv-1",
          firstFrameAssetId: "ff-1",
          confirmedScriptText: "已确认的乡墅口播终稿",
        }),
      ),
    );
    expect(value.navigate).toHaveBeenCalledWith("tasks");
  });

  it("审计员人物替换链路只读且不自动写入参考选择", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: "project-1" },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "只读替换项目",
          owner_user_id: "auditor-1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    value.user = {
      id: value.user.id,
      username: value.user.username,
      display_name: "审计员",
      role: "auditor",
    };
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    expect(
      screen.getByRole("button", { name: "stub-选择人物" }),
    ).toHaveAttribute("data-read-only", "true");
    expect(
      screen.getByRole("button", { name: "stub-确认源画面" }),
    ).toHaveAttribute("data-read-only", "true");
    vi.mocked(value.patchDraft).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "stub-确认置换首帧" }),
    );
    expect(
      screen.getByRole("button", { name: "用于文/图生视频" }),
    ).toBeInTheDocument();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(replicaApi.selectCharacterReferences).not.toHaveBeenCalled();
  });

  it("审计员不能从空人物替换页切换项目", () => {
    const value = studio({
      review: false,
      user: {
        id: "auditor-1",
        username: "auditor-1",
        display_name: "审计员",
        role: "auditor",
      },
    });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: undefined },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "可查看项目",
          owner_user_id: "auditor-1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    const selector = screen.getByLabelText("选择项目");
    expect(selector).toBeDisabled();
    fireEvent.change(selector, { target: { value: "project-1" } });
    expect(value.patchDraft).not.toHaveBeenCalled();
  });

  it("切换为审计员后保留已恢复的人物与首帧展示", async () => {
    replicaApi.selectCharacterReferences.mockResolvedValue({
      id: "reference-selection-1",
    });
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: "project-1" },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "角色切换项目",
          owner_user_id: "customer-1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    useStudio.mockReturnValue(value);
    const view = render(<ReplacementPage />);
    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await screen.findByRole("button", { name: "stub-确认置换首帧" });

    value.user = {
      id: "auditor-1",
      username: "auditor-1",
      display_name: "审计员",
      role: "auditor",
    };
    vi.mocked(value.patchDraft).mockClear();
    replicaApi.selectCharacterReferences.mockClear();
    view.rerender(<ReplacementPage />);

    expect(
      screen.getByRole("button", { name: "stub-确认置换首帧" }),
    ).toHaveAttribute("data-read-only", "true");
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(replicaApi.selectCharacterReferences).not.toHaveBeenCalled();
  });
});
