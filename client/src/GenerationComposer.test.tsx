import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { GenerationComposer } from "./GenerationComposer";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    compileGenerationPrompt: vi.fn(),
    createGenerationBatch: vi.fn(),
    createScriptVersion: vi.fn(),
    getGenerationRuntimeLimits: vi.fn(),
    getLatestGenerationPrompt: vi.fn(),
    getLatestScriptVersion: vi.fn(),
    lockGenerationPrompt: vi.fn(),
    reviseGenerationPrompt: vi.fn(),
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
  onWorkflowStepChange: vi.fn(),
  originalScript: "原稿第一句。原稿第二句。",
  projectId: "project-1",
  readOnly: false,
  referenceSelectionId: "reference-selection-1",
  shotCardVersionId: "shot-card-1",
};

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
      output_duration_seconds: 10,
      resolution: "768P",
    },
  };
}

describe("GenerationComposer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.mocked(api.getLatestScriptVersion).mockResolvedValue({
      version: null,
      stale: false,
      stale_reasons: [],
    });
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
    vi.mocked(api.compileGenerationPrompt).mockResolvedValue(promptVersion());
    vi.mocked(api.reviseGenerationPrompt).mockResolvedValue({
      ...promptVersion(),
      id: "prompt-2",
      version_number: 2,
      payload: {
        ...promptVersion().payload,
        prompt_text: "人工修订 Prompt",
      },
    });
    vi.mocked(api.lockGenerationPrompt).mockResolvedValue({
      ...promptVersion("LOCKED"),
      id: "prompt-2",
      version_number: 2,
      payload: {
        ...promptVersion("LOCKED").payload,
        prompt_text: "人工修订 Prompt",
      },
    });
    const batch = {
      id: "batch-1",
      project_id: "project-1",
      prompt_version_id: "prompt-2",
      status: "QUEUED",
      quantity: 4,
      stale: false,
      progress: {
        total_count: 4,
        terminal_count: 0,
        progress_percent: 0,
        counts: {},
      },
      tasks: [],
    };
    vi.mocked(api.createGenerationBatch).mockResolvedValue(batch);

    render(<GenerationComposer {...props} />);

    expect(await screen.findByText("口播稿与 H3 Prompt")).toBeInTheDocument();
    expect(screen.getByText("分析版本：analysis-1")).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "编译 H3 Prompt" }));
    expect(
      await screen.findByDisplayValue("编译后的 Prompt"),
    ).toBeInTheDocument();
    expect(screen.getByText("模板版本：h3.prompt.v1")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("H3 Prompt 内容"), {
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

    fireEvent.click(screen.getByRole("button", { name: "锁定 Prompt" }));
    await waitFor(() =>
      expect(api.lockGenerationPrompt).toHaveBeenCalledWith(
        "project-1",
        "prompt-2",
      ),
    );

    const quantity = screen.getByLabelText("生成数量");
    fireEvent.change(quantity, { target: { value: "4" } });
    expect(screen.getByText("将创建 4 个付费生成任务")).toBeInTheDocument();
    expect(screen.getByText("预计费用暂不可用")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建 4 个生成任务" }));

    await waitFor(() =>
      expect(api.createGenerationBatch).toHaveBeenCalledOnce(),
    );
    expect(api.createGenerationBatch).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        quantity: 4,
        prompt_version_id: "prompt-2",
        first_frame_asset_id: "first-frame-1",
        provider: "fake_h3",
      }),
    );
    expect(props.onBatchCreated).toHaveBeenCalledWith(batch);
  });

  it("rejects decimal and out-of-range quantities while allowing one and the maximum", async () => {
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

    render(<GenerationComposer {...props} />);
    const button = await screen.findByRole("button", {
      name: "创建 1 个生成任务",
    });
    expect(button).toBeEnabled();

    const quantity = screen.getByLabelText("生成数量");
    fireEvent.change(quantity, { target: { value: "1.5" } });
    expect(screen.getByText("生成数量必须是整数")).toBeInTheDocument();
    expect(button).toBeDisabled();

    fireEvent.change(quantity, { target: { value: "5" } });
    expect(screen.getByText("生成数量必须在 1–4 之间")).toBeInTheDocument();
    expect(button).toBeDisabled();

    fireEvent.change(quantity, { target: { value: "4" } });
    expect(
      screen.getByRole("button", { name: "创建 4 个生成任务" }),
    ).toBeEnabled();
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

    render(<GenerationComposer {...props} />);
    const createButton = await screen.findByRole("button", {
      name: "创建 1 个生成任务",
    });
    expect(createButton).toBeEnabled();

    fireEvent.change(screen.getByLabelText("成片时长"), {
      target: { value: "15" },
    });

    expect(createButton).toBeDisabled();
    expect(
      screen.getByText("生成参数已变化，请重新编译 Prompt。"),
    ).toBeInTheDocument();
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

    render(<GenerationComposer {...props} />);

    expect(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    ).toBeEnabled();
    expect(
      screen.queryByText("生成参数已变化，请重新编译 Prompt。"),
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

    render(<GenerationComposer {...props} />);
    const compileButton = await screen.findByRole("button", {
      name: "编译 H3 Prompt",
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
      screen.getByText("口播稿有未保存修改，请先保存。"),
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

    render(<GenerationComposer {...props} />);
    const prompt = await screen.findByLabelText("H3 Prompt 内容");
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

    expect(prompt).not.toHaveAttribute("readonly");
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

    render(<GenerationComposer {...props} />);
    const compileButton = await screen.findByRole("button", {
      name: "编译 H3 Prompt",
    });
    fireEvent.change(screen.getByLabelText("H3 Prompt 内容"), {
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

    render(<GenerationComposer {...props} />);
    const prompt = await screen.findByLabelText("H3 Prompt 内容");
    fireEvent.click(screen.getByRole("button", { name: "编译 H3 Prompt" }));

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
        progress: {
          total_count: 1,
          terminal_count: 0,
          progress_percent: 0,
          counts: {},
        },
        tasks: [],
      });

    render(<GenerationComposer {...props} />);
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
    expect(props.onBusyChange).toHaveBeenLastCalledWith(false);
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

    render(<GenerationComposer {...props} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    expect(await screen.findByText("网络连接失败")).toBeInTheDocument();

    const firstRequest = vi.mocked(api.createGenerationBatch).mock.calls[0][1];
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

    fireEvent.click(screen.getByRole("button", { name: "恢复已提交批次" }));
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
      <GenerationComposer
        {...props}
        currentUserId="employee_scope"
        projectId={projectId}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    expect(await screen.findByText("网络连接失败")).toBeInTheDocument();
    expect(window.localStorage.getItem(employeeStorageKey)).not.toBeNull();

    rerender(
      <GenerationComposer
        {...props}
        currentUserId="admin_scope"
        projectId={projectId}
      />,
    );
    await screen.findByRole("button", { name: "创建 1 个生成任务" });
    expect(
      screen.queryByRole("button", { name: "恢复已提交批次" }),
    ).not.toBeInTheDocument();
    expect(window.localStorage.getItem(employeeStorageKey)).not.toBeNull();

    rerender(
      <GenerationComposer
        {...props}
        currentUserId="employee_scope"
        projectId={projectId}
      />,
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

    render(<GenerationComposer {...props} />);
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

      render(<GenerationComposer {...props} projectId={projectId} />);
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

    render(<GenerationComposer {...props} />);
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

    const { rerender } = render(
      <GenerationComposer {...props} projectId="project-old" />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "创建 1 个生成任务" }),
    );
    rerender(
      <GenerationComposer
        {...props}
        projectId="project-2"
        firstFrameAssetId="first-frame-2"
      />,
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

    const { unmount } = render(<GenerationComposer {...props} />);
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

    render(<GenerationComposer {...props} />);
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
