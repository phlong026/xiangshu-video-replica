import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GenerationRuntimeLimits, GenerationVersion } from "./api";
import { GenerationLauncher } from "./GenerationLauncher";
import type { GenerationBusyAction } from "./useGenerationDrafts";

const DEFAULT_LIMITS: GenerationRuntimeLimits = {
  min_quantity: 1,
  max_quantity: 3,
  estimated_cost_per_task: 2.5,
};

function promptVersionWith(
  payload: Record<string, unknown>,
): GenerationVersion {
  return {
    id: "prompt-v1",
    project_id: "project-1",
    asset_id: null,
    kind: "h3_prompt",
    version_number: 1,
    payload: {
      prompt_text: "编译后的 Prompt",
      status: "LOCKED",
      output_duration_seconds: 8,
      resolution: "768P",
      first_frame_asset_id: "ff-asset-1",
      ...payload,
    },
    created_by_user_id: "employee_1",
    created_at: "2030-01-01T00:00:00Z",
  } as unknown as GenerationVersion;
}

function renderLauncher(overrides: Record<string, unknown> = {}) {
  const callbacks = {
    onCompilePrompt: vi.fn(),
    onCreateBatch: vi.fn(),
    onDurationChange: vi.fn(),
    onLockPrompt: vi.fn(),
    onQuantityChange: vi.fn(),
    onRecoverBatch: vi.fn(),
    onResolutionChange: vi.fn(),
    onSavePromptRevision: vi.fn(),
    onPromptTextChange: vi.fn(),
  };
  const props = {
    analysisVersionId: "analysis-v1",
    busyAction: null as GenerationBusyAction,
    canCompile: true,
    canCreateBatch: false,
    characterVersionId: "char-v1" as string | null,
    durationValid: true,
    firstFrameAssetId: "ff-asset-1",
    firstFrameSelectionVersionId: "ff-selection-v1",
    limits: DEFAULT_LIMITS,
    onCompilePrompt: callbacks.onCompilePrompt,
    onCreateBatch: callbacks.onCreateBatch,
    onDurationChange: callbacks.onDurationChange,
    onLockPrompt: callbacks.onLockPrompt,
    onQuantityChange: callbacks.onQuantityChange,
    onRecoverBatch: callbacks.onRecoverBatch,
    onResolutionChange: callbacks.onResolutionChange,
    onSavePromptRevision: callbacks.onSavePromptRevision,
    onPromptTextChange: callbacks.onPromptTextChange,
    outputDuration: "8",
    promptDirty: false,
    promptParametersMatch: true,
    promptStale: false,
    promptText: "",
    promptVersion: null as GenerationVersion | null,
    quantity: 1 as number | null,
    quantityError: "",
    quantityInput: "1",
    readOnly: false,
    recoveryRecord: null,
    recoveryRecordConflicts: false,
    referenceSelectionId: "ref-v1" as string | null,
    resolution: "768P" as "768P" | "2K",
    savedPromptText: "",
    scriptStale: false,
    shotCardVersionId: "shot-card-v1",
    ...overrides,
  };
  return { callbacks, props, ...render(<GenerationLauncher {...props} />) };
}

describe("GenerationLauncher Prompt 编译修订锁定（受控组件）", () => {
  it("无 Prompt 时仅显示参数与编译入口", () => {
    renderLauncher();

    expect(screen.getByText("2. 编译、修订并锁定 Prompt")).toBeInTheDocument();
    expect(screen.getByLabelText("成片时长")).toBeInTheDocument();
    expect(screen.getByLabelText("分辨率")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "编译 H3 Prompt" }),
    ).toBeEnabled();
    expect(screen.queryByLabelText("H3 Prompt 内容")).not.toBeInTheDocument();
  });

  it("时长非法时显示错误提示", () => {
    renderLauncher({ durationValid: false, canCompile: false });
    expect(
      screen.getByText("成片时长必须是 4–15 秒的整数。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "编译 H3 Prompt" }),
    ).toBeDisabled();
  });

  it("scriptStale 显示镜头卡变化警示", () => {
    renderLauncher({ scriptStale: true });
    expect(
      screen.getByText("镜头卡已变化，请重新保存口播稿"),
    ).toBeInTheDocument();
  });

  it("promptStale 显示上游变化警示", () => {
    renderLauncher({ promptStale: true });
    expect(
      screen.getByText("上游输入已变化，请重新编译 Prompt"),
    ).toBeInTheDocument();
  });

  it("参数不匹配显示重编译警示", () => {
    renderLauncher({
      promptParametersMatch: false,
      promptVersion: promptVersionWith({}),
    });
    expect(
      screen.getByText("生成参数已变化，请重新编译 Prompt"),
    ).toBeInTheDocument();
  });

  it("编辑时长/分辨率上抛回调", () => {
    const { callbacks } = renderLauncher();

    fireEvent.change(screen.getByLabelText("成片时长"), {
      target: { value: "10" },
    });
    expect(callbacks.onDurationChange).toHaveBeenCalledWith("10");

    fireEvent.change(screen.getByLabelText("分辨率"), {
      target: { value: "2K" },
    });
    expect(callbacks.onResolutionChange).toHaveBeenCalledWith("2K");
  });

  it("存在 Prompt 时展示内容/差异/锁定与数量区", () => {
    renderLauncher({
      canCreateBatch: true,
      promptText: "当前编辑文本",
      promptVersion: promptVersionWith({}),
      savedPromptText: "已保存文本",
    });

    expect(screen.getByLabelText("H3 Prompt 内容")).toHaveValue("当前编辑文本");
    expect(screen.getByText("已保存版本")).toBeInTheDocument();
    expect(screen.getByText("当前编辑")).toBeInTheDocument();
    expect(screen.getByText("当前状态：LOCKED")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建 1 个生成任务" }),
    ).toBeEnabled();
  });

  it("数量区显示付费提醒与预计费用", () => {
    renderLauncher({
      canCreateBatch: true,
      promptVersion: promptVersionWith({}),
      quantity: 2,
    });

    expect(screen.getByText("将创建 2 个付费生成任务")).toBeInTheDocument();
    expect(screen.getByText("预计费用：¥5.00")).toBeInTheDocument();
  });

  it("数量错误显示提示", () => {
    renderLauncher({
      quantity: null,
      quantityError: "生成数量必须在 1–3 之间",
    });

    expect(screen.getByText("生成数量必须在 1–3 之间")).toBeInTheDocument();
  });

  it("恢复记录存在时显示恢复按钮并触发回调", () => {
    const { callbacks } = renderLauncher({
      promptVersion: promptVersionWith({}),
      recoveryRecord: { fingerprint: "f", key: "k", request: {} },
    });

    const button = screen.getByRole("button", { name: "恢复已提交批次" });
    fireEvent.click(button);
    expect(callbacks.onRecoverBatch).toHaveBeenCalledTimes(1);
  });

  it("恢复冲突时显示冲突警示", () => {
    renderLauncher({ recoveryRecordConflicts: true });
    expect(
      screen.getByText("存在待恢复的已提交批次，请先恢复后再更改生成请求。"),
    ).toBeInTheDocument();
  });

  it("readOnly 下全部控件禁用", () => {
    renderLauncher({
      canCompile: false,
      promptText: "文本",
      promptVersion: promptVersionWith({}),
      readOnly: true,
      savedPromptText: "文本",
    });

    expect(screen.getByLabelText("成片时长")).toBeDisabled();
    expect(screen.getByLabelText("分辨率")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "编译 H3 Prompt" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("H3 Prompt 内容")).toHaveAttribute("readonly");
    expect(
      screen.getByRole("button", { name: "另存 Prompt 新版本" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "锁定 Prompt" })).toBeDisabled();
    expect(screen.getByLabelText("生成数量")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /创建 \d 个生成任务/ }),
    ).toBeDisabled();
  });

  it("点击编译/锁定/创建触发对应回调", () => {
    const { callbacks } = renderLauncher({
      canCreateBatch: true,
      promptText: "编辑中",
      promptVersion: promptVersionWith({ status: "SAVED" }),
      savedPromptText: "编辑中",
    });

    fireEvent.click(screen.getByRole("button", { name: "编译 H3 Prompt" }));
    expect(callbacks.onCompilePrompt).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "锁定 Prompt" }));
    expect(callbacks.onLockPrompt).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /创建 \d 个生成任务/ }));
    expect(callbacks.onCreateBatch).toHaveBeenCalledTimes(1);
  });

  it("promptDirty 时点击另存触发回调", () => {
    const { callbacks } = renderLauncher({
      promptDirty: true,
      promptText: "编辑中",
      promptVersion: promptVersionWith({ status: "SAVED" }),
      savedPromptText: "旧文本",
    });

    fireEvent.click(screen.getByRole("button", { name: "另存 Prompt 新版本" }));
    expect(callbacks.onSavePromptRevision).toHaveBeenCalledTimes(1);
  });

  it("冻结来源摘要展示六项输入", () => {
    renderLauncher();

    expect(screen.getByText("分析版本：analysis-v1")).toBeInTheDocument();
    expect(screen.getByText("镜头卡版本：shot-card-v1")).toBeInTheDocument();
    expect(screen.getByText("人物版本：char-v1")).toBeInTheDocument();
    expect(screen.getByText("人物参考：ref-v1")).toBeInTheDocument();
    expect(screen.getByText("首帧选择：ff-selection-v1")).toBeInTheDocument();
    expect(screen.getByText("首帧素材：ff-asset-1")).toBeInTheDocument();
  });

  it("legacy 人物路径显示历史兼容文案", () => {
    renderLauncher({ characterVersionId: null, referenceSelectionId: null });
    expect(screen.getByText("人物版本：历史兼容人物")).toBeInTheDocument();
    expect(screen.getByText("人物参考：历史兼容参考")).toBeInTheDocument();
  });
});
