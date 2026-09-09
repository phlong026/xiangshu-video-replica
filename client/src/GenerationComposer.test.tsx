import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { GenerationComposer } from "./GenerationComposer";
import { ScriptEditor } from "./ScriptEditor";
import { useGenerationDrafts } from "./useGenerationDrafts";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    compileGenerationPrompt: vi.fn(),
    createGenerationBatch: vi.fn(),
    createScriptVersion: vi.fn(),
    getGenerationPriceQuote: vi.fn(),
    getGenerationRuntimeLimits: vi.fn(),
    getLatestGenerationPrompt: vi.fn(),
    getLatestScriptRewriteTask: vi.fn(),
    getLatestScriptVersion: vi.fn(),
    listSavedGenerationPrompts: vi.fn(),
    lockGenerationPrompt: vi.fn(),
    reviseGenerationPrompt: vi.fn(),
    rewriteProjectScript: vi.fn(),
    waitForScriptRewriteTask: vi.fn(),
    applySavedGenerationPrompt: vi.fn(),
    saveGenerationPrompt: vi.fn(),
  };
});

const baseVersion = {
  id: "version-1",
  project_id: "project-1",
  asset_id: null,
  kind: "script",
  version_number: 1,
  payload: {},
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

const props = {
  analysisVersionId: "analysis-1",
  characterVersionId: "character-version-1",
  currentUserId: "employee_1",
  durationSeconds: 10,
  firstFrameAssetId: "first-frame-1",
  firstFrameSelectionVersionId: "first-frame-selection-1",
  onBatchCreated: vi.fn(),
  onBusyChange: vi.fn(),
  originalScript: "原稿第一句。原稿第二句。",
  projectId: "project-1",
  readOnly: false,
  referenceSelectionId: "reference-selection-1",
  shotCardVersionId: "shot-card-1",
  sourceAssetId: null,
};

// P0-02-03：复刻工作区结构——ScriptEditor（标签页①）与 GenerationComposer
// （标签页③）共享同一 useGenerationDrafts 实例；busy 上报迁至宿主，
// 与 AnalysisWorkspace 的提升后行为保持一致。
function WorkspaceHost({
  currentUserId = props.currentUserId,
  durationSeconds = props.durationSeconds,
  firstFrameAssetId = props.firstFrameAssetId,
  identityId,
  projectId = props.projectId,
}: {
  currentUserId?: string;
  durationSeconds?: number;
  firstFrameAssetId?: string;
  identityId?: string;
  projectId?: string;
}) {
  const drafts = useGenerationDrafts({
    characterVersionId: props.characterVersionId,
    currentUserId,
    durationSeconds,
    firstFrameAssetId,
    firstFrameSelectionVersionId: props.firstFrameSelectionVersionId,
    identityId,
    originalScript: props.originalScript,
    projectId,
    readOnly: props.readOnly,
    referenceSelectionId: props.referenceSelectionId,
    shotCardVersionId: props.shotCardVersionId,
    sourceAssetId: props.sourceAssetId,
  });

  useEffect(() => {
    props.onBusyChange(Boolean(drafts.busyAction));
  }, [drafts.busyAction]);

  useEffect(
    () => () => {
      props.onBusyChange(false);
    },
    [],
  );

  return (
    <div>
      {drafts.isLoading ? null : (
        <ScriptEditor
          busyAction={drafts.busyAction}
          onChooseSource={drafts.chooseScriptSource}
          onRewriteScript={drafts.rewriteScriptWithAi}
          onSaveScript={drafts.saveScript}
          onScriptTextChange={drafts.setScriptText}
          readOnly={props.readOnly}
          scriptDirty={drafts.scriptDirty}
          scriptSource={drafts.scriptSource}
          scriptStale={drafts.scriptStale}
          scriptText={drafts.scriptText}
          shotMappings={drafts.shotMappings}
        />
      )}
      {/* P0-04-01：真实工作区由主按钮触发，这里用探针直接调用同一 Hook 动作。 */}
      <button
        onClick={() => drafts.runGenerationPipeline(props.onBatchCreated)}
        type="button"
      >
        触发一键流水线
      </button>
      <GenerationComposer
        analysisVersionId={props.analysisVersionId}
        characterVersionId={props.characterVersionId}
        drafts={drafts}
        firstFrameAssetId={firstFrameAssetId}
        firstFrameSelectionVersionId={props.firstFrameSelectionVersionId}
        onBatchCreated={props.onBatchCreated}
        readOnly={props.readOnly}
        referenceSelectionId={props.referenceSelectionId}
        shotCardVersionId={props.shotCardVersionId}
      />
    </div>
  );
}

function promptVersion(status: "SAVED" | "LOCKED" | "USED" = "SAVED") {
  return {
    ...baseVersion,
    id: "prompt-1",
    kind: "h3_prompt",
    payload: {
      status,
      prompt_text: "编译后的 Prompt",
      script_version_id: "script-1",
      shot_card_version_id: "shot-card-1",
      first_frame_asset_id: "first-frame-1",
      first_frame_selection_version_id: "first-frame-selection-1",
      character_version_id: "character-version-1",
      character_reference_selection_id: "reference-selection-1",
      template_version: "h3.prompt.v1",
      template_hash: "template-hash",
      output_duration_seconds: 15,
      resolution: "768P",
      ratio: "adaptive",
    },
  };
}

describe("GenerationComposer", () => {
  beforeEach(() => {
    // Reset queued mockImplementationOnce values as well as calls. A failed
    // assertion must not leave the next recovery test consuming its response.
    vi.resetAllMocks();
    window.localStorage.clear();
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: null,
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestScriptRewriteTask).mockResolvedValue(null);
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: null,
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getGenerationRuntimeLimits).mockResolvedValue({
      min_quantity: 1,
      max_quantity: 4,
      estimated_cost_per_task: null,
    });
    vi.mocked(api.getGenerationPriceQuote).mockImplementation(
      async (input) => ({
        ...input,
        unit_price_fen_per_second: 9,
        estimated_seconds: input.duration_seconds * input.quantity,
        estimated_price_fen: input.duration_seconds * input.quantity * 9,
      }),
    );
    vi.mocked(api.listSavedGenerationPrompts).mockResolvedValue([]);
    vi.mocked(api.saveGenerationPrompt).mockRejectedValue(
      new Error("library unavailable"),
    );
  });

  it("shows a saved prompt library load failure instead of an empty library", async () => {
    vi.mocked(api.listSavedGenerationPrompts).mockRejectedValue(
      new Error("我的提示词暂时无法读取"),
    );

    render(<WorkspaceHost />);

    expect(
      await screen.findByText("我的提示词暂时无法读取"),
    ).toBeInTheDocument();
  });

  it("AI 改写入队后立即释放页面 busy，并在后台完成后回填结果", async () => {
    const pendingTask: api.ScriptRewriteTask = {
      id: "rewrite-task-1",
      project_id: "project-1",
      identity_id: null,
      ip_profile_hash: null,
      ip_profile_snapshot: null,
      source_asset_id: null,
      source_text: "原稿第一句。原稿第二句。",
      status: "PENDING",
      attempt: 0,
      result: null,
      error_code: null,
      error_message: null,
      retryable: false,
      created_at: "2030-01-01T00:00:00Z",
      updated_at: "2030-01-01T00:00:00Z",
      started_at: null,
      completed_at: null,
    };
    const completedTask: api.ScriptRewriteTask = {
      ...pendingTask,
      status: "SUCCEEDED",
      attempt: 1,
      result: {
        rewritten_text: "后台完成的二创稿。",
        provider: "deepseek",
        model: "deepseek-chat",
      },
      completed_at: "2030-01-01T00:00:05Z",
    };
    let resolveRewrite: ((task: api.ScriptRewriteTask) => void) | undefined;
    vi.mocked(api.rewriteProjectScript).mockResolvedValue(pendingTask);
    vi.mocked(api.waitForScriptRewriteTask).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRewrite = resolve;
        }),
    );

    render(<WorkspaceHost />);
    fireEvent.click(await screen.findByRole("button", { name: "AI 改写" }));

    await waitFor(() =>
      expect(api.rewriteProjectScript).toHaveBeenCalledWith(
        "project-1",
        "原稿第一句。原稿第二句。",
        undefined,
        undefined,
        expect.any(String),
      ),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "AI 改写" })).toBeEnabled(),
    );
    expect(props.onBusyChange).toHaveBeenCalledWith(true);
    expect(props.onBusyChange).toHaveBeenLastCalledWith(false);

    await act(async () => {
      resolveRewrite?.(completedTask);
      await Promise.resolve();
    });
    expect(screen.getByLabelText("口播稿内容")).toHaveValue(
      "后台完成的二创稿。",
    );
    expect(screen.getByRole("radio", { name: "自定义稿" })).toBeChecked();
  });

  it("切换人物后不回填旧人物的异步改写结果", async () => {
    const pendingTask: api.ScriptRewriteTask = {
      id: "rewrite-person-a",
      project_id: "project-1",
      identity_id: "person-a",
      ip_profile_hash: "profile-a",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "设计师",
        service_scope: "乡墅设计",
        target_audience: "返乡建房家庭",
        expression_style: "专业直白",
        profile_version: 1,
      },
      source_asset_id: null,
      source_text: "原稿第一句。原稿第二句。",
      status: "PENDING",
      attempt: 0,
      result: null,
      error_code: null,
      error_message: null,
      retryable: false,
      created_at: "2030-01-01T00:00:00Z",
      updated_at: "2030-01-01T00:00:00Z",
      started_at: null,
      completed_at: null,
    };
    let resolveRewrite: ((task: api.ScriptRewriteTask) => void) | undefined;
    vi.mocked(api.rewriteProjectScript).mockResolvedValue(pendingTask);
    vi.mocked(api.waitForScriptRewriteTask).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRewrite = resolve;
        }),
    );
    const view = render(<WorkspaceHost identityId="person-a" />);
    await waitFor(() =>
      expect(api.getLatestScriptRewriteTask).toHaveBeenCalledWith(
        "project-1",
        "person-a",
        null,
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: "AI 改写" }));
    await waitFor(() =>
      expect(api.rewriteProjectScript).toHaveBeenCalledWith(
        "project-1",
        "原稿第一句。原稿第二句。",
        "person-a",
        undefined,
        expect.any(String),
      ),
    );
    expect(
      screen.getByText(/AI 改写正在后台执行（人物：张工）/),
    ).toBeInTheDocument();

    view.rerender(<WorkspaceHost identityId="person-b" />);
    await waitFor(() =>
      expect(api.getLatestScriptRewriteTask).toHaveBeenCalledWith(
        "project-1",
        "person-b",
        null,
      ),
    );
    await act(async () => {
      resolveRewrite?.({
        ...pendingTask,
        status: "SUCCEEDED",
        result: {
          rewritten_text: "张工的旧人物改写结果。",
          provider: "deepseek",
          model: "deepseek-chat",
        },
        completed_at: "2030-01-01T00:00:05Z",
      });
      await Promise.resolve();
    });

    expect(screen.getByLabelText("口播稿内容")).not.toHaveValue(
      "张工的旧人物改写结果。",
    );
  });

  it("重新进入项目时恢复仍在执行的 AI 改写任务", async () => {
    const pendingTask: api.ScriptRewriteTask = {
      id: "rewrite-task-recovered",
      project_id: "project-1",
      identity_id: null,
      ip_profile_hash: null,
      ip_profile_snapshot: null,
      source_asset_id: null,
      source_text: "原稿第一句。原稿第二句。",
      status: "RUNNING",
      attempt: 1,
      result: null,
      error_code: null,
      error_message: null,
      retryable: false,
      created_at: "2030-01-02T00:00:00Z",
      updated_at: "2030-01-02T00:00:01Z",
      started_at: "2030-01-02T00:00:01Z",
      completed_at: null,
    };
    vi.mocked(api.getLatestScriptRewriteTask).mockResolvedValue(pendingTask);
    vi.mocked(api.waitForScriptRewriteTask).mockResolvedValue({
      ...pendingTask,
      status: "SUCCEEDED",
      result: {
        rewritten_text: "离开页面期间完成的二创稿。",
        provider: "deepseek",
        model: "deepseek-chat",
      },
      completed_at: "2030-01-02T00:00:10Z",
    });

    render(<WorkspaceHost />);

    await waitFor(() =>
      expect(api.waitForScriptRewriteTask).toHaveBeenCalledWith(
        "rewrite-task-recovered",
      ),
    );
    expect(api.getLatestScriptRewriteTask).toHaveBeenCalledWith(
      "project-1",
      undefined,
      null,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("口播稿内容")).toHaveValue(
        "离开页面期间完成的二创稿。",
      ),
    );
  });

  it("一键流水线：Prompt 存在未保存修订时提示先保存且不发起任何请求（P0-04-01）", async () => {
    window.localStorage.clear();
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        payload: {
          source: "original",
          full_text: "原稿第一句。原稿第二句。",
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("SAVED"),
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);

    // 等 drafts 重载完成后锚定已保存文本，再制造人工修订中的脏 Prompt。
    const promptField = await waitFor(() => {
      const field = screen.getByLabelText("视频生成提示词内容");
      expect(field).toHaveValue("编译后的 Prompt");
      return field;
    });
    fireEvent.change(promptField, {
      target: { value: "人工修订中的草稿" },
    });
    fireEvent.click(screen.getByRole("button", { name: "触发一键流水线" }));

    // 红线：不自动接受未保存草稿，也不编译/锁定/建批。
    expect(
      await screen.findByText(
        "Prompt 存在未保存修订，请先在「生成设置」中保存后再开始生成。",
      ),
    ).toBeInTheDocument();
    expect(api.compileGenerationPrompt).not.toHaveBeenCalled();
    expect(api.lockGenerationPrompt).not.toHaveBeenCalled();
    expect(api.createGenerationBatch).not.toHaveBeenCalled();
    expect(props.onBatchCreated).not.toHaveBeenCalled();
  });

  it("runs custom script, prompt revision, lock and max-quantity batch creation", async () => {
    vi.mocked(api.createScriptVersion).mockResolvedValue({
      ...baseVersion,
      id: "script-1",
      payload: {
        source: "custom",
        full_text: "自定义口播稿",
        shot_card_version_id: "shot-card-1",
        shot_mappings: [
          { shot_id: "S01", text: "自定义口播稿", start_time: 0, end_time: 10 },
        ],
      },
    });
    vi.mocked(api.compileGenerationPrompt).mockResolvedValue({
      ...promptVersion(),
      payload: { ...promptVersion().payload, ratio: "16:9" },
    });
    vi.mocked(api.reviseGenerationPrompt).mockResolvedValue({
      ...promptVersion(),
      id: "prompt-2",
      version_number: 2,
      payload: {
        ...promptVersion().payload,
        prompt_text: "人工修订 Prompt",
        ratio: "16:9",
      },
    });
    vi.mocked(api.lockGenerationPrompt).mockResolvedValue({
      ...promptVersion("LOCKED"),
      id: "prompt-2",
      version_number: 2,
      payload: {
        ...promptVersion("LOCKED").payload,
        prompt_text: "人工修订 Prompt",
        ratio: "16:9",
      },
    });
    const batch = {
      id: "batch-1",
      project_id: "project-1",
      prompt_version_id: "prompt-2",
      status: "QUEUED",
      quantity: 4,
      stale: false,
      creation_kind: "replica",
      progress: {
        total_count: 4,
        terminal_count: 0,
        progress_percent: 0,
        counts: {},
      },
      tasks: [],
    };
    vi.mocked(api.createGenerationBatch).mockResolvedValue(batch);

    render(<WorkspaceHost />);

    expect(await screen.findByText("口播稿与 Prompt")).toBeInTheDocument();
    expect(screen.getByText("拆解版本：analysis-1")).toBeInTheDocument();
    expect(
      screen.getByText("人物版本：character-version-1"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("自定义稿"));
    fireEvent.change(screen.getByLabelText("口播稿内容"), {
      target: { value: "自定义口播稿" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存口播稿" }));

    await waitFor(() =>
      expect(api.createScriptVersion).toHaveBeenCalledWith("project-1", {
        source: "custom",
        text: "自定义口播稿",
        shot_card_version_id: "shot-card-1",
      }),
    );
    expect(await screen.findByText("S01：自定义口播稿")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("16:9"));
    fireEvent.click(screen.getByRole("button", { name: "编译视频生成提示词" }));
    await waitFor(() =>
      expect(api.compileGenerationPrompt).toHaveBeenCalledWith("project-1", {
        script_version_id: "script-1",
        shot_card_version_id: "shot-card-1",
        first_frame_asset_id: "first-frame-1",
        output_duration_seconds: 15,
        resolution: "768P",
        ratio: "16:9",
      }),
    );
    expect(
      await screen.findByDisplayValue("编译后的 Prompt"),
    ).toBeInTheDocument();
    expect(screen.getByText("模板版本：h3.prompt.v1")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("视频生成提示词内容"), {
      target: { value: "人工修订 Prompt" },
    });
    expect(
      screen.getByText("当前编辑与已保存版本存在差异"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "另存 Prompt 新版本" }));
    await waitFor(() =>
      expect(api.reviseGenerationPrompt).toHaveBeenCalledWith("project-1", {
        base_prompt_version_id: "prompt-1",
        prompt_text: "人工修订 Prompt",
      }),
    );

    const lockButton = screen.getByRole("button", { name: "锁定 Prompt" });
    await waitFor(() => expect(lockButton).toBeEnabled());
    fireEvent.click(lockButton);
    await waitFor(() =>
      expect(api.lockGenerationPrompt).toHaveBeenCalledWith(
        "project-1",
        "prompt-2",
      ),
    );

    const quantity = screen.getByLabelText("生成数量");
    fireEvent.change(quantity, { target: { value: "4" } });
    expect(screen.getByText("将创建 4 个付费生成任务")).toBeInTheDocument();
    expect(screen.getByText("预计消耗 60 秒额度")).toBeInTheDocument();
    const createFour = screen.getByRole("button", {
      name: "创建 4 个生成任务",
    });
    await waitFor(() => expect(createFour).toBeEnabled());
    fireEvent.click(createFour);

    await waitFor(() =>
      expect(api.createGenerationBatch).toHaveBeenCalledOnce(),
    );
    expect(api.createGenerationBatch).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        quantity: 4,
        prompt_version_id: "prompt-2",
        first_frame_asset_id: "first-frame-1",
        provider: "metaso",
      }),
    );
    expect(props.onBatchCreated).toHaveBeenCalledWith(batch);
  });

  it("offers only the supported 1, 2, and 4 task quantities", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);
    const button = await screen.findByRole("button", {
      name: "创建 1 个生成任务",
    });
    expect(button).toBeEnabled();

    const quantity = screen.getByLabelText("生成数量");
    expect(
      Array.from((quantity as HTMLSelectElement).options).map(
        (option) => option.value,
      ),
    ).toEqual(["1", "2", "4"]);

    fireEvent.change(quantity, { target: { value: "4" } });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "创建 4 个生成任务" }),
      ).toBeEnabled(),
    );
  });

  it("requires prompt recompilation after generation parameters change", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);
    const createButton = await screen.findByRole("button", {
      name: "创建 1 个生成任务",
    });
    expect(createButton).toBeEnabled();

    fireEvent.change(screen.getByLabelText("成片时长"), {
      target: { value: "4" },
    });

    expect(createButton).toBeDisabled();
    expect(
      screen.getByText("生成参数已变化，请重新编译 Prompt"),
    ).toBeInTheDocument();
  });

  it("follows the reference duration once it loads after mount (P0-02-03 提升后行为锁定)", async () => {
    const { rerender } = render(<WorkspaceHost durationSeconds={0} />);

    await screen.findByRole("button", { name: "创建 1 个生成任务" });
    // 拆解时长未就位时用钳制下限，不能固化 0 或 NaN（评审 Major 1）。
    expect(screen.getByLabelText("成片时长")).toHaveValue("4");

    rerender(<WorkspaceHost durationSeconds={12} />);

    await waitFor(() =>
      expect(screen.getByLabelText("成片时长")).toHaveValue("15"),
    );
    // 默认 mock 无锁定 Prompt，创建按钮保持禁用属正常；确认面板加载完成即可。
    expect(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    ).toBeInTheDocument();
  });

  it("normalizes an old arbitrary prompt duration to the conservative 4-second option", async () => {
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: {
        ...promptVersion(),
        payload: { ...promptVersion().payload, output_duration_seconds: 8 },
      },
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);

    expect(await screen.findByLabelText("成片时长")).toHaveValue("4");
    expect(
      screen.getByText("生成参数已变化，请重新编译 Prompt"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/4–15 秒的整数/)).toBeNull();
  });

  it("clears a stale price quote as soon as generation parameters change", async () => {
    let resolveQuote: ((quote: api.GenerationPriceQuote) => void) | undefined;
    vi.mocked(api.getGenerationPriceQuote)
      .mockResolvedValueOnce({
        resolution: "768P",
        duration_seconds: 15,
        quantity: 1,
        unit_price_fen_per_second: 9,
        estimated_seconds: 15,
        estimated_price_fen: 135,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveQuote = resolve;
        }),
      );

    render(<WorkspaceHost />);

    expect(await screen.findByText(/约 ¥1\.35/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("成片时长"), {
      target: { value: "4" },
    });
    expect(screen.queryByText(/约 ¥1\.35/)).toBeNull();
    expect(screen.getByText("预计消耗 4 秒额度")).toBeInTheDocument();

    await act(async () => {
      resolveQuote?.({
        resolution: "768P",
        duration_seconds: 4,
        quantity: 1,
        unit_price_fen_per_second: 9,
        estimated_seconds: 4,
        estimated_price_fen: 36,
      });
    });
  });

  it("blocks both generation actions while the current quote is unavailable and retries it", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getGenerationPriceQuote)
      .mockRejectedValueOnce(new Error("复刻费用暂不可用"))
      .mockResolvedValueOnce({
        resolution: "768P",
        duration_seconds: 15,
        quantity: 1,
        unit_price_fen_per_second: 9,
        estimated_seconds: 15,
        estimated_price_fen: 135,
      });

    render(<WorkspaceHost />);

    expect(await screen.findByText("复刻费用暂不可用")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建 1 个生成任务" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "触发一键流水线" }));
    expect(api.createGenerationBatch).not.toHaveBeenCalled();
    expect(api.createScriptVersion).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重新获取生成报价" }));

    expect(await screen.findByText(/约 ¥1\.35/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建 1 个生成任务" }),
    ).toBeEnabled();
  });

  it("keeps a legacy locked prompt without frozen parameters usable", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    const legacyPayload: Record<string, unknown> = {
      ...promptVersion("LOCKED").payload,
    };
    delete legacyPayload.output_duration_seconds;
    delete legacyPayload.resolution;
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: { ...promptVersion("LOCKED"), payload: legacyPayload },
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);

    expect(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    ).toBeEnabled();
    expect(
      screen.queryByText("生成参数已变化，请重新编译 Prompt"),
    ).not.toBeInTheDocument();
  });

  it("requires saving visible script edits before prompt compilation", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "custom",
          full_text: "已保存的自定义稿",
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);
    const compileButton = await screen.findByRole("button", {
      name: "编译视频生成提示词",
    });
    expect(compileButton).toBeEnabled();
    const createButton = screen.getByRole("button", {
      name: "创建 1 个生成任务",
    });
    expect(createButton).toBeEnabled();

    fireEvent.change(screen.getByLabelText("口播稿内容"), {
      target: { value: "尚未保存的新稿" },
    });

    expect(compileButton).toBeDisabled();
    expect(createButton).toBeDisabled();
    expect(
      screen.getByText("口播稿有未保存修改，请先保存"),
    ).toBeInTheDocument();
  });

  it("freezes prompt editing while a revision is saving", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion(),
      stale: false,
      stale_reasons: [],
    });
    let resolveRevision:
      | ((version: ReturnType<typeof promptVersion>) => void)
      | undefined;
    vi.mocked(api.reviseGenerationPrompt).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRevision = resolve;
        }),
    );

    render(<WorkspaceHost />);
    const prompt = await screen.findByLabelText("视频生成提示词内容");
    fireEvent.change(prompt, { target: { value: "等待保存的修订" } });
    fireEvent.click(screen.getByRole("button", { name: "另存 Prompt 新版本" }));

    expect(prompt).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "正在保存" })).toBeDisabled();

    await act(async () => {
      resolveRevision?.({
        ...promptVersion(),
        id: "prompt-2",
        version_number: 2,
        payload: {
          ...promptVersion().payload,
          prompt_text: "等待保存的修订",
        },
      });
      await Promise.resolve();
    });

    await waitFor(() => expect(prompt).not.toHaveAttribute("readonly"));
    expect(prompt).toHaveValue("等待保存的修订");
  });

  it("blocks prompt recompilation while manual edits are unsaved", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion(),
      stale: false,
      stale_reasons: [],
    });

    render(<WorkspaceHost />);
    const compileButton = await screen.findByRole("button", {
      name: "编译视频生成提示词",
    });
    fireEvent.change(screen.getByLabelText("视频生成提示词内容"), {
      target: { value: "尚未保存的手工修订" },
    });

    expect(compileButton).toBeDisabled();
    fireEvent.click(compileButton);
    expect(api.compileGenerationPrompt).not.toHaveBeenCalled();
  });

  it("freezes prompt editing while recompilation is pending", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion(),
      stale: false,
      stale_reasons: [],
    });
    let resolveCompile:
      | ((version: ReturnType<typeof promptVersion>) => void)
      | undefined;
    vi.mocked(api.compileGenerationPrompt).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCompile = resolve;
        }),
    );

    render(<WorkspaceHost />);
    const prompt = await screen.findByLabelText("视频生成提示词内容");
    fireEvent.click(screen.getByRole("button", { name: "编译视频生成提示词" }));

    expect(prompt).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "正在编译" })).toBeDisabled();

    await act(async () => {
      resolveCompile?.({
        ...promptVersion(),
        id: "prompt-2",
        version_number: 2,
        payload: {
          ...promptVersion().payload,
          prompt_text: "重新编译后的 Prompt",
        },
      });
      await Promise.resolve();
    });

    expect(prompt).not.toHaveAttribute("readonly");
    expect(prompt).toHaveValue("重新编译后的 Prompt");
  });

  it("keeps one idempotency key across duplicate clicks and an offline retry", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    const firstRequest: { reject?: (error: Error) => void } = {};
    vi.mocked(api.createGenerationBatch)
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            firstRequest.reject = reject;
          }),
      )
      .mockResolvedValueOnce({
        id: "batch-recovered",
        project_id: "project-1",
        prompt_version_id: "prompt-1",
        status: "QUEUED",
        quantity: 1,
        stale: false,
        creation_kind: "replica",
        progress: {
          total_count: 1,
          terminal_count: 0,
          progress_percent: 0,
          counts: {},
        },
        tasks: [],
      });

    render(<WorkspaceHost />);
    const button = await screen.findByRole("button", {
      name: "创建 1 个生成任务",
    });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.createGenerationBatch).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(props.onBusyChange).toHaveBeenLastCalledWith(true),
    );

    firstRequest.reject?.(
      new Error("创建视频生成批次失败：网络连接失败，请检查本地服务"),
    );
    expect(await screen.findByText(/网络连接失败/)).toBeInTheDocument();
    await waitFor(() =>
      expect(props.onBusyChange).toHaveBeenLastCalledWith(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "创建 1 个生成任务" }));
    await waitFor(() =>
      expect(api.createGenerationBatch).toHaveBeenCalledTimes(2),
    );

    const firstKey = vi.mocked(api.createGenerationBatch).mock.calls[0][1]
      .idempotency_key;
    const secondKey = vi.mocked(api.createGenerationBatch).mock.calls[1][1]
      .idempotency_key;
    expect(firstKey).toBeTruthy();
    expect(secondKey).toBe(firstKey);
  });

  it("blocks a different paid request until the unresolved batch is recovered", async () => {
    const quote = {
      resolution: "768P" as const,
      duration_seconds: 15,
      quantity: 1,
      unit_price_fen_per_second: 9,
      estimated_seconds: 15,
      estimated_price_fen: 135,
    };
    vi.mocked(api.getGenerationPriceQuote)
      .mockResolvedValueOnce(quote)
      .mockRejectedValueOnce(new Error("恢复请求报价暂不可用"))
      .mockResolvedValue(quote);
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    const recoveredBatch: api.GenerationBatch = {
      id: "batch-original-request",
      project_id: "project-1",
      prompt_version_id: "prompt-1",
      status: "QUEUED",
      quantity: 1,
      stale: false,
      creation_kind: "replica",
      progress: {
        total_count: 1,
        terminal_count: 0,
        progress_percent: 0,
        counts: {},
      },
      tasks: [],
    };
    vi.mocked(api.createGenerationBatch)
      .mockRejectedValueOnce(new Error("网络连接失败"))
      .mockResolvedValueOnce(recoveredBatch);

    render(<WorkspaceHost />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    expect(await screen.findByText("网络连接失败")).toBeInTheDocument();

    const firstRequest = vi.mocked(api.createGenerationBatch).mock.calls[0][1];
    expect(await screen.findByText("恢复请求报价暂不可用")).toBeInTheDocument();
    const recover = screen.getByRole("button", {
      name: "恢复已提交批次",
    });
    expect(recover).toBeDisabled();
    fireEvent.click(recover);
    expect(api.createGenerationBatch).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "重新获取生成报价" }));
    await waitFor(() => expect(recover).toBeEnabled());

    fireEvent.change(screen.getByLabelText("生成数量"), {
      target: { value: "2" },
    });

    const conflictingCreate = screen.getByRole("button", {
      name: "创建 2 个生成任务",
    });
    expect(conflictingCreate).toBeDisabled();
    expect(
      screen.getByText("存在待恢复的已提交批次，请先恢复后再更改生成请求。"),
    ).toBeInTheDocument();
    fireEvent.click(conflictingCreate);
    expect(api.createGenerationBatch).toHaveBeenCalledOnce();
    expect(
      JSON.parse(
        window.localStorage.getItem(
          "generation.idempotency/employee_1/project-1",
        ) ?? "null",
      ).key,
    ).toBe(firstRequest.idempotency_key);

    fireEvent.click(recover);
    await waitFor(() =>
      expect(api.createGenerationBatch).toHaveBeenCalledTimes(2),
    );
    expect(vi.mocked(api.createGenerationBatch).mock.calls[1][1]).toEqual(
      firstRequest,
    );
    expect(props.onBatchCreated).toHaveBeenCalledWith(recoveredBatch);
  });

  it("does not restore another authenticated user's paid recovery record", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.createGenerationBatch).mockRejectedValueOnce(
      new Error("网络连接失败"),
    );
    const projectId = "project-user-scope";
    const employeeStorageKey =
      "generation.idempotency/employee_scope/project-user-scope";

    const { rerender } = render(
      <WorkspaceHost currentUserId="employee_scope" projectId={projectId} />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    expect(await screen.findByText("网络连接失败")).toBeInTheDocument();
    expect(window.localStorage.getItem(employeeStorageKey)).not.toBeNull();

    rerender(
      <WorkspaceHost currentUserId="admin_scope" projectId={projectId} />,
    );
    await screen.findByRole("button", { name: "创建 1 个生成任务" });
    expect(
      screen.queryByRole("button", { name: "恢复已提交批次" }),
    ).not.toBeInTheDocument();
    expect(window.localStorage.getItem(employeeStorageKey)).not.toBeNull();

    rerender(
      <WorkspaceHost currentUserId="employee_scope" projectId={projectId} />,
    );
    expect(
      await screen.findByRole("button", { name: "恢复已提交批次" }),
    ).toBeInTheDocument();
  });

  it("releases the recovery record after a definitive batch rejection", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.createGenerationBatch).mockRejectedValueOnce(
      Object.assign(new Error("上游内容已变化，请重新确认后再试"), {
        status: 409,
        code: "PROMPT_STALE",
      }),
    );

    render(<WorkspaceHost />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );

    expect(
      await screen.findByText("上游内容已变化，请重新确认后再试"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "恢复已提交批次" }),
    ).not.toBeInTheDocument();
    expect(
      window.localStorage.getItem(
        "generation.idempotency/employee_1/project-1",
      ),
    ).toBeNull();
  });

  it.each([401, 403, 404, 429])(
    "keeps the recovery record after ambiguous HTTP %s",
    async (status) => {
      const projectId = `project-${status}`;
      vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
        version: {
          ...baseVersion,
          id: "script-1",
          payload: {
            source: "original",
            full_text: props.originalScript,
            shot_card_version_id: "shot-card-1",
            shot_mappings: [],
          },
        },
        stale: false,
        stale_reasons: [],
      });
      vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
        version: promptVersion("LOCKED"),
        stale: false,
        stale_reasons: [],
      });
      vi.mocked(api.createGenerationBatch).mockRejectedValueOnce(
        Object.assign(new Error("当前无法确认批次结果"), { status }),
      );

      render(<WorkspaceHost projectId={projectId} />);
      fireEvent.click(
        await screen.findByRole("button", { name: "创建 1 个生成任务" }),
      );

      expect(
        await screen.findByText("当前无法确认批次结果"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "恢复已提交批次" }),
      ).toBeInTheDocument();
      expect(
        window.localStorage.getItem(
          `generation.idempotency/employee_1/${projectId}`,
        ),
      ).not.toBeNull();
    },
  );

  it("still creates a batch when browser recovery storage is unavailable", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.createGenerationBatch)
      .mockRejectedValueOnce(new Error("网络连接失败"))
      .mockResolvedValueOnce({
        id: "batch-without-storage",
        project_id: "project-1",
        prompt_version_id: "prompt-1",
        status: "QUEUED",
        quantity: 1,
        stale: false,
        creation_kind: "replica",
        progress: {
          total_count: 1,
          terminal_count: 0,
          progress_percent: 0,
          counts: {},
        },
        tasks: [],
      });
    const storageWrite = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("storage blocked", "SecurityError");
      });

    render(<WorkspaceHost />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );

    expect(await screen.findByText("网络连接失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建 1 个生成任务" }));
    await waitFor(() =>
      expect(api.createGenerationBatch).toHaveBeenCalledTimes(2),
    );
    expect(
      vi.mocked(api.createGenerationBatch).mock.calls[1][1].idempotency_key,
    ).toBe(
      vi.mocked(api.createGenerationBatch).mock.calls[0][1].idempotency_key,
    );
    expect(props.onBatchCreated).toHaveBeenCalledWith(
      expect.objectContaining({ id: "batch-without-storage" }),
    );
    storageWrite.mockRestore();
  });

  it("ignores a completed batch from a project that is no longer active", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt).mockResolvedValue({
      version: promptVersion("LOCKED"),
      stale: false,
      stale_reasons: [],
    });
    let resolveOldBatch: ((batch: api.GenerationBatch) => void) | undefined;
    vi.mocked(api.createGenerationBatch).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldBatch = resolve;
        }),
    );

    const { rerender } = render(<WorkspaceHost projectId="project-old" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    rerender(
      <WorkspaceHost projectId="project-2" firstFrameAssetId="first-frame-2" />,
    );
    await waitFor(() =>
      expect(api.getLatestScriptVersion).toHaveBeenCalledWith("project-2"),
    );

    await act(async () => {
      resolveOldBatch?.({
        id: "batch-from-project-1",
        project_id: "project-1",
        prompt_version_id: "prompt-1",
        status: "QUEUED",
        quantity: 1,
        stale: false,
        creation_kind: "replica",
        progress: {
          total_count: 1,
          terminal_count: 0,
          progress_percent: 0,
          counts: {},
        },
        tasks: [],
      });
      await Promise.resolve();
    });

    expect(props.onBatchCreated).not.toHaveBeenCalled();
  });

  it("recovers an ignored paid batch after storage-blocked composer teardown", async () => {
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: {
        ...baseVersion,
        id: "script-1",
        payload: {
          source: "original",
          full_text: props.originalScript,
          shot_card_version_id: "shot-card-1",
          shot_mappings: [],
        },
      },
      stale: false,
      stale_reasons: [],
    });
    vi.mocked(api.getLatestGenerationPrompt)
      .mockResolvedValueOnce({
        version: promptVersion("LOCKED"),
        stale: false,
        stale_reasons: [],
      })
      .mockResolvedValueOnce({
        version: promptVersion("USED"),
        stale: false,
        stale_reasons: [],
      });
    const recoveredBatch: api.GenerationBatch = {
      id: "batch-after-unmount",
      project_id: "project-1",
      prompt_version_id: "prompt-1",
      status: "QUEUED",
      quantity: 1,
      stale: false,
      creation_kind: "replica",
      progress: {
        total_count: 1,
        terminal_count: 0,
        progress_percent: 0,
        counts: {},
      },
      tasks: [],
    };
    let resolveBatch: ((batch: api.GenerationBatch) => void) | undefined;
    vi.mocked(api.createGenerationBatch)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveBatch = resolve;
          }),
      )
      .mockResolvedValueOnce(recoveredBatch);
    const storageWrite = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("storage blocked", "SecurityError");
      });

    const { unmount } = render(<WorkspaceHost />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    const idempotencyKey = vi.mocked(api.createGenerationBatch).mock.calls[0][1]
      .idempotency_key;
    expect(idempotencyKey).toBeTruthy();
    expect(
      window.localStorage.getItem(
        "generation.idempotency/employee_1/project-1",
      ),
    ).toBeNull();
    unmount();

    await act(async () => {
      resolveBatch?.(recoveredBatch);
      await Promise.resolve();
    });

    expect(props.onBatchCreated).not.toHaveBeenCalled();

    render(<WorkspaceHost />);
    fireEvent.click(
      await screen.findByRole("button", { name: "恢复已提交批次" }),
    );
    await waitFor(() =>
      expect(api.createGenerationBatch).toHaveBeenCalledTimes(2),
    );
    expect(
      vi.mocked(api.createGenerationBatch).mock.calls[1][1].idempotency_key,
    ).toBe(idempotencyKey);
    expect(props.onBatchCreated).toHaveBeenCalledWith(recoveredBatch);
    storageWrite.mockRestore();
  });
});
