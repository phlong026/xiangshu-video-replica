import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { ProjectDetailFlow } from "./ProjectDetailFlow";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    chooseProjectMainCharacterVersion: vi.fn(),
    compileGenerationPrompt: vi.fn(),
    confirmFirstFrame: vi.fn(),
    createGenerationBatch: vi.fn(),
    createScriptVersion: vi.fn(),
    defaultBatchProvider: actual.defaultBatchProvider,
    generateFirstFrames: vi.fn(),
    getGenerationRuntimeLimits: vi.fn(),
    getLatestGenerationPrompt: vi.fn(),
    getLatestProjectAnalysis: vi.fn(),
    getLatestProjectFirstFrameSelection: vi.fn(),
    getLatestProjectShotCards: vi.fn(),
    getLatestProjectFirstFrames: vi.fn(),
    getLatestProjectSourceFrameSelection: vi.fn(),
    getLatestProjectSourceFrames: vi.fn(),
    getLatestScriptVersion: vi.fn(),
    getProjectFirstFrameHistory: vi.fn(),
    getProjectMainCharacter: vi.fn(),
    getAssetDownloadUrl: vi.fn(),
    listProjectCharacterVersions: vi.fn(),
    lockGenerationPrompt: vi.fn(),
    previewGenerationPrompt: vi.fn(),
    reviseGenerationPrompt: vi.fn(),
    saveShotCards: vi.fn(),
    selectCharacterReferences: vi.fn(),
  };
});

const project = {
  id: "project-1",
  name: "乡墅爆款",
  reference_asset_id: "ref-1",
  reference_upload_status: "READY",
  analysis_status: "READY",
} as unknown as api.Project;

// 与服务端落库结构一致：拆解版本 payload 是 { analysis: {...} } 包装结构。
const analysisVersion: api.AnalysisVersion = {
  id: "analysis-1",
  project_id: project.id,
  asset_id: "asset-1",
  kind: "video_analysis",
  version_number: 1,
  payload: {
    analysis: {
      summary: "女子走向乡墅。",
      shots: [
        {
          shot_id: "shot-1",
          start_time: 0,
          end_time: 2.5,
          shot_type: "近景",
          composition: "特写",
          camera_motion: "固定",
          subject: "女子",
          action: "走向别墅",
          scene: "庭院",
          transition: "无",
          spoken_text: "乡下的房子",
        },
      ],
      original_script: "乡下的房子真好。",
      duration_seconds: 10,
    },
  },
  created_by_user_id: null,
  created_at: "2025-01-01T00:00:00Z",
};

const mainCharacter = {
  character_version_id: "cv-1",
  version_number: 1,
  character_snapshot: {
    schema_version: "project-character-selection.v1",
    identity: { display_name: "林夏", authorization_expires_at: null },
    character_version_number: 1,
    persona_snapshot_json: { name: "田园博主" },
  },
} as unknown as api.ProjectMainCharacter;

const sourceFramesVersion: api.AnalysisVersion = {
  id: "sf-version-1",
  project_id: project.id,
  asset_id: null,
  kind: "source_frame_candidates",
  version_number: 1,
  payload: {
    requested_timestamps_seconds: [0.5, 1.5],
    candidates: [
      { asset_id: "cand-1", timestamp_seconds: 0.5, score: 0.9 },
      { asset_id: "cand-2", timestamp_seconds: 1.5, score: 0.8 },
    ],
  },
  created_by_user_id: null,
  created_at: "2025-01-01T00:00:00Z",
};

const sourceFrameSelectionVersion: api.AnalysisVersion = {
  id: "sel-version-1",
  project_id: project.id,
  asset_id: "cand-1",
  kind: "source_frame_selection",
  version_number: 1,
  payload: {
    source_frame_candidates_version_id: "sf-version-1",
    source_frame_asset_id: "cand-1",
    character_features: {
      orientation: "FRONT",
      shot_size: "HALF_BODY",
      face_visible: true,
      body_completeness: "UPPER_BODY",
    },
  },
  created_by_user_id: null,
  created_at: "2025-01-01T00:00:00Z",
};

const firstFrameCandidatesVersion: api.AnalysisVersion = {
  id: "first-frame-candidates-1",
  project_id: project.id,
  asset_id: null,
  kind: "first_frame_candidates",
  version_number: 1,
  payload: {
    provider: "apilio",
    model: "gpt-image-2",
    prompt: "replace person without text",
    candidates: [
      {
        asset_id: "first-frame-1",
        storage_key: "projects/project-1/first-frame-1.png",
        storage_uri: "local://projects/project-1/first-frame-1.png",
        sha256: "first-frame-hash-1",
        size_bytes: 100,
        content_type: "image/png",
      },
    ],
  },
  created_by_user_id: null,
  created_at: "2025-01-01T00:00:00Z",
};

const firstFrameSelectionVersion: api.AnalysisVersion = {
  id: "first-frame-selection-1",
  project_id: project.id,
  asset_id: "first-frame-1",
  kind: "first_frame_selection",
  version_number: 1,
  payload: {
    first_frame_candidates_version_id: firstFrameCandidatesVersion.id,
    first_frame_asset_id: "first-frame-1",
  },
  created_by_user_id: null,
  created_at: "2025-01-01T00:00:00Z",
};

const previewResult: api.PromptPreviewResult = {
  prompt_text:
    "生成一条 10 秒、768P、写实短视频，从提供的首帧自然开始。\n[0.0-2.5s] 近景，特写，固定。口播意图：乡下的房子。",
  output_duration_seconds: 10,
  resolution: "768P",
  script_source: "analysis_original",
  shot_card_version_id: null,
};

const referenceSelection = {
  character_version_id: "cv-1",
  selected_asset_ids: ["sheet-1", "photo-1"],
} as unknown as api.CharacterReferenceSelection;

const characterVersions = [
  {
    character_version_id: "cv-1",
    identity_name: "林夏",
    persona_snapshot_json: { name: "田园博主", occupation: "博主" },
    version_number: 1,
    published_at: "2025-01-02T00:00:00Z",
    authorization_expires_at: null,
    assets: [
      "FRONT_FACE",
      "FRONT_HALF",
      "FRONT_FULL",
      "LEFT_45",
      "RIGHT_45",
      "LEFT_SIDE",
      "RIGHT_SIDE",
    ].map((viewType, index) => ({
      character_asset_id: `asset-${viewType}`,
      view_type: viewType,
      index,
    })),
  },
  {
    character_version_id: "cv-2",
    identity_name: "小叮当",
    persona_snapshot_json: { name: "工地管家", occupation: "管家" },
    version_number: 2,
    published_at: "2025-01-03T00:00:00Z",
    authorization_expires_at: null,
    assets: [
      "FRONT_FACE",
      "FRONT_HALF",
      "FRONT_FULL",
      "LEFT_45",
      "RIGHT_45",
      "LEFT_SIDE",
      "RIGHT_SIDE",
    ].map((viewType, index) => ({
      character_asset_id: `asset2-${viewType}`,
      view_type: viewType,
      index,
    })),
  },
] as unknown as api.ProjectCharacterVersionOption[];

describe("ProjectDetailFlow", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getLatestProjectAnalysis).mockResolvedValue(analysisVersion);
    vi.mocked(api.getGenerationRuntimeLimits).mockResolvedValue({
      estimated_cost_per_task: 0.5,
    } as unknown as api.GenerationRuntimeLimits);
    vi.mocked(api.previewGenerationPrompt).mockResolvedValue(previewResult);
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(mainCharacter);
    vi.mocked(api.getLatestProjectSourceFrames).mockResolvedValue(
      sourceFramesVersion,
    );
    vi.mocked(api.getLatestProjectSourceFrameSelection).mockResolvedValue({
      version: sourceFrameSelectionVersion,
      stale: false,
    });
    vi.mocked(api.getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `http://127.0.0.1:8000/mock/${assetId}`,
    }));
    vi.mocked(api.getLatestProjectFirstFrames).mockResolvedValue({
      version: null,
      stale: false,
    });
    vi.mocked(api.getLatestProjectFirstFrameSelection).mockResolvedValue({
      version: null,
      stale: false,
    });
    vi.mocked(api.getProjectFirstFrameHistory).mockResolvedValue([]);
    vi.mocked(api.selectCharacterReferences).mockResolvedValue(
      referenceSelection,
    );
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue(
      characterVersions,
    );
    vi.mocked(api.chooseProjectMainCharacterVersion).mockImplementation(
      async (_projectId, versionId) =>
        ({
          character_version_id: versionId,
          version_number: 1,
          character_snapshot: {
            schema_version: "project-character-selection.v1",
            identity: { display_name: "林夏", authorization_expires_at: null },
            character_version_number: 1,
            persona_snapshot_json: { name: "田园博主" },
          },
        }) as unknown as api.ProjectMainCharacter,
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the five flow steps with editable custom copy", async () => {
    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly={false}
      />,
    );

    expect(
      await screen.findByText("① 解析提示词", { selector: "legend" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("② 源画面与人物", { selector: "legend" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("③ 人物置换首帧", { selector: "legend" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("④ 自定义文案", { selector: "legend" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("⑤ 提交生成", { selector: "legend" }),
    ).toBeInTheDocument();
    // 第一段展示由拆解结果自动编译的提示词（Markdown 预览）。
    expect(await screen.findByText("0.0-2.5s")).toBeInTheDocument();
    // 第四段带入原文，用户可直接修改，不再选择 AI 二创模式。
    expect(await screen.findByLabelText("自定义文案")).toHaveValue(
      "乡下的房子真好。",
    );
    expect(screen.queryByRole("button", { name: "AI 二创改写" })).toBeNull();
    expect(screen.queryByRole("button", { name: "使用原文案" })).toBeNull();
    // 第二段区头是内联角色下拉，简化模式不渲染特征/模型/提示词表单。
    const roleSelect = await screen.findByLabelText("角色版本");
    expect(roleSelect).toHaveValue("cv-1");
    expect(
      screen.getByRole("option", { name: /林夏 · 田园博主 V1/ }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("人物朝向")).toBeNull();
    expect(screen.queryByLabelText("首帧模型")).toBeNull();
    expect(screen.queryByLabelText("首帧编辑提示词")).toBeNull();
  });

  it("persists the character choice from the inline dropdown and re-matches references", async () => {
    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly={false}
      />,
    );

    const roleSelect = await screen.findByLabelText("角色版本");
    // 等版本列表加载完成且下拉可用后再交互（恢复/加载窗口内 select 禁用）。
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: /小叮当 · 工地管家 V2/ }),
      ).toBeInTheDocument(),
    );
    await waitFor(() => expect(roleSelect).toBeEnabled());
    fireEvent.change(roleSelect, { target: { value: "cv-2" } });

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        "cv-2",
      ),
    );
    // 角色变化触发 stale 级联：对新角色版本重新自动匹配人物参考。
    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledWith("project-1", {
        character_version_id: "cv-2",
        source_frame_selection_version_id: "sel-version-1",
      }),
    );
  });

  it("auto-matches character references once character and source frame are restored", async () => {
    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly={false}
      />,
    );

    await waitFor(() =>
      expect(api.selectCharacterReferences).toHaveBeenCalledWith("project-1", {
        character_version_id: "cv-1",
        source_frame_selection_version_id: "sel-version-1",
      }),
    );
    expect(await screen.findByText(/已自动匹配人物参考/)).toBeInTheDocument();
    // 同一组合只自动匹配一次。
    expect(api.selectCharacterReferences).toHaveBeenCalledTimes(1);
  });

  it("shows a retryable error when auto-matching fails", async () => {
    vi.mocked(api.selectCharacterReferences)
      .mockRejectedValueOnce(new Error("人物参考暂不可用。"))
      .mockResolvedValueOnce(referenceSelection);

    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly={false}
      />,
    );

    expect(await screen.findByText(/人物参考暂不可用。/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试匹配" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "重试匹配" }));

    expect(await screen.findByText(/已自动匹配人物参考/)).toBeInTheDocument();
    expect(api.selectCharacterReferences).toHaveBeenCalledTimes(2);
  });

  it("keeps read-only visitors from auto-matching or submitting", async () => {
    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly
      />,
    );

    await waitFor(() => expect(api.getProjectMainCharacter).toHaveBeenCalled());
    expect(api.selectCharacterReferences).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动匹配人物参考/)).toBeNull();
    // 只读身份没有提示词编辑入口。
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
  });

  it("prefills the original script and lets the user edit it in place", async () => {
    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly={false}
      />,
    );

    const scriptArea = await screen.findByLabelText("自定义文案");
    await waitFor(() => expect(scriptArea).toHaveValue("乡下的房子真好。"));

    fireEvent.change(scriptArea, {
      target: { value: "这栋乡下别墅真让人心动。" },
    });
    expect(screen.getByLabelText("自定义文案")).toHaveValue(
      "这栋乡下别墅真让人心动。",
    );
    expect(screen.queryByText(/AI 二创/)).toBeNull();
  });

  it("compiles custom copy into the video prompt without restoring an older manual prompt", async () => {
    vi.mocked(api.getLatestProjectFirstFrames).mockResolvedValue({
      version: firstFrameCandidatesVersion,
      stale: false,
    });
    vi.mocked(api.getLatestProjectFirstFrameSelection).mockResolvedValue({
      version: firstFrameSelectionVersion,
      stale: false,
    });
    vi.mocked(api.getProjectFirstFrameHistory).mockResolvedValue([
      firstFrameCandidatesVersion,
    ]);
    vi.mocked(api.getLatestProjectShotCards).mockResolvedValue({
      ...analysisVersion,
      id: "shot-card-1",
      kind: "shot_card",
      payload: { source_analysis_version_id: analysisVersion.id },
    });
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: null,
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.createScriptVersion).mockResolvedValue({
      ...analysisVersion,
      id: "script-custom-1",
      kind: "script",
      payload: { full_text: "这栋乡下别墅真让人心动。" },
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: {
        ...analysisVersion,
        id: "prompt-old-1",
        kind: "generation_prompt",
        payload: { prompt_text: "旧的手工 Prompt" },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.reviseGenerationPrompt).mockResolvedValue({
      ...analysisVersion,
      id: "prompt-manual-1",
      kind: "generation_prompt",
      payload: { prompt_text: "保存过但已过时的手工 Prompt" },
    });
    vi.mocked(api.compileGenerationPrompt).mockResolvedValue({
      ...analysisVersion,
      id: "prompt-compiled-custom-1",
      kind: "generation_prompt",
      payload: { prompt_text: "含新自定义文案的编译 Prompt" },
    });
    vi.mocked(api.lockGenerationPrompt).mockResolvedValue({
      ...analysisVersion,
      id: "prompt-locked-custom-1",
      kind: "generation_prompt",
      payload: { prompt_text: "含新自定义文案的编译 Prompt" },
    });
    const batch = {
      id: "batch-custom-1",
      project_id: project.id,
      prompt_version_id: "prompt-locked-custom-1",
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
    } as api.GenerationBatch;
    vi.mocked(api.createGenerationBatch).mockResolvedValue(batch);
    const onBatchCreated = vi.fn();

    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={onBatchCreated}
        project={project}
        readOnly={false}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("提示词源码"), {
      target: { value: "保存过但已过时的手工 Prompt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "另存 Prompt 新版本" }));
    await waitFor(() =>
      expect(api.reviseGenerationPrompt).toHaveBeenCalledOnce(),
    );

    fireEvent.change(await screen.findByLabelText("自定义文案"), {
      target: { value: "这栋乡下别墅真让人心动。" },
    });
    expect(screen.getByText(/不会覆盖本次文案/)).toBeInTheDocument();

    const startButton = screen.getByRole("button", {
      name: "开始生成（1 个付费任务）",
    });
    await waitFor(() => expect(startButton).toBeEnabled());
    fireEvent.click(startButton);

    await waitFor(() =>
      expect(api.createScriptVersion).toHaveBeenCalledWith(project.id, {
        source: "custom",
        text: "这栋乡下别墅真让人心动。",
        shot_card_version_id: "shot-card-1",
      }),
    );
    expect(api.compileGenerationPrompt).toHaveBeenCalledWith(project.id, {
      script_version_id: "script-custom-1",
      shot_card_version_id: "shot-card-1",
      first_frame_asset_id: "first-frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
    });
    expect(api.reviseGenerationPrompt).toHaveBeenCalledTimes(1);
    expect(api.lockGenerationPrompt).toHaveBeenCalledWith(
      project.id,
      "prompt-compiled-custom-1",
    );
    await waitFor(() => expect(onBatchCreated).toHaveBeenCalledWith(batch));
  });

  it("keeps project navigation available while a first frame continues in the background", async () => {
    let resolveGeneration: ((version: api.AnalysisVersion) => void) | undefined;
    const pendingGeneration = new Promise<api.AnalysisVersion>((resolve) => {
      resolveGeneration = resolve;
    });
    vi.mocked(api.generateFirstFrames).mockReturnValue(pendingGeneration);
    const workspaceBusy = vi.fn();

    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        onBusyChange={workspaceBusy}
        project={project}
        readOnly={false}
      />,
    );

    const generateButton = await screen.findByRole("button", {
      name: "重新生成候选首帧",
    });
    await waitFor(() => expect(generateButton).toBeEnabled());
    workspaceBusy.mockClear();
    fireEvent.click(generateButton);

    expect(
      await screen.findByRole("progressbar", {
        name: "人物置换首帧生成进度",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回项目列表" })).toBeEnabled();
    expect(workspaceBusy).not.toHaveBeenCalledWith(true);
    expect(screen.getByLabelText("角色版本")).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新提取候选" })).toBeDisabled();
    expect(screen.getByText(/生成结束前暂不能更改/)).toBeInTheDocument();

    await act(async () => {
      resolveGeneration?.(firstFrameCandidatesVersion);
      await pendingGeneration;
    });
  });

  it("keeps the paid submission disabled until a first frame is confirmed", async () => {
    render(
      <ProjectDetailFlow
        onBack={vi.fn()}
        onBatchCreated={vi.fn()}
        project={project}
        readOnly={false}
      />,
    );

    // 未确认首帧时第五段保持禁用引导态：不出现付费警告与开始生成按钮。
    expect(
      await screen.findByText("确认首帧后即可提交生成。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /开始生成/ })).toBeNull();
    expect(api.createGenerationBatch).not.toHaveBeenCalled();
  });
});
