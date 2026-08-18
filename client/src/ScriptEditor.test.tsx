import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScriptEditor } from "./ScriptEditor";
import type { GenerationBusyAction } from "./useGenerationDrafts";

const originalScript = "原始口播稿文本";

function renderEditor(overrides: Record<string, unknown> = {}) {
  const callbacks = {
    onChooseSource: vi.fn(),
    onScriptTextChange: vi.fn(),
    onSaveScript: vi.fn(),
  };
  const props = {
    busyAction: null as GenerationBusyAction,
    onChooseSource: callbacks.onChooseSource,
    onScriptTextChange: callbacks.onScriptTextChange,
    onSaveScript: callbacks.onSaveScript,
    readOnly: false,
    scriptDirty: false,
    scriptSource: "original" as const,
    scriptStale: false,
    scriptText: originalScript,
    shotMappings: [] as Array<{ shotId: string; text: string }>,
    ...overrides,
  };
  return { callbacks, ...render(<ScriptEditor {...props} />) };
}

describe("ScriptEditor 口播稿编辑（受控组件）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("显示原稿/自定义稿选项与当前文本", () => {
    renderEditor();

    expect(screen.getByRole("radio", { name: "原稿" })).toBeChecked();
    expect(screen.getByLabelText("口播稿内容")).toHaveValue(originalScript);
  });

  it("选择原稿时上抛 onChooseSource（组合层负责重置文本）", () => {
    const { callbacks } = renderEditor({ scriptSource: "custom" });

    fireEvent.click(screen.getByRole("radio", { name: "原稿" }));
    expect(callbacks.onChooseSource).toHaveBeenCalledWith("original");
  });

  it("选择自定义稿时上抛 onChooseSource", () => {
    const { callbacks } = renderEditor();

    fireEvent.click(screen.getByRole("radio", { name: "自定义稿" }));
    expect(callbacks.onChooseSource).toHaveBeenCalledWith("custom");
  });

  it("编辑文本时上抛 onScriptTextChange", () => {
    const { callbacks } = renderEditor({ scriptSource: "custom" });

    fireEvent.change(screen.getByLabelText("口播稿内容"), {
      target: { value: "新的口播稿" },
    });
    expect(callbacks.onScriptTextChange).toHaveBeenCalledWith("新的口播稿");
  });

  it("原稿模式下文本只读", () => {
    renderEditor({ scriptSource: "original" });
    expect(screen.getByLabelText("口播稿内容")).toHaveAttribute("readonly");
  });

  it("readOnly 下所有控件禁用", () => {
    renderEditor({ readOnly: true, scriptSource: "custom" });

    expect(screen.getByRole("radio", { name: "原稿" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "自定义稿" })).toBeDisabled();
    expect(screen.getByLabelText("口播稿内容")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "保存口播稿" })).toBeDisabled();
  });

  it("busyAction=script 时按钮显示正在保存并禁用", () => {
    renderEditor({ busyAction: "script" });

    const button = screen.getByRole("button", { name: "正在保存" });
    expect(button).toBeDisabled();
  });

  it("文本为空时保存按钮禁用", () => {
    renderEditor({ scriptSource: "custom", scriptText: "" });

    expect(screen.getByRole("button", { name: "保存口播稿" })).toBeDisabled();
  });

  it("点击保存触发 onSaveScript", () => {
    const { callbacks } = renderEditor({
      scriptSource: "custom",
      scriptText: "草稿",
    });

    fireEvent.click(screen.getByRole("button", { name: "保存口播稿" }));
    expect(callbacks.onSaveScript).toHaveBeenCalledTimes(1);
  });

  it("scriptStale 时显示镜头卡变化警示", () => {
    renderEditor({ scriptStale: true });
    expect(
      screen.getByText("镜头卡已变化，请重新保存口播稿"),
    ).toBeInTheDocument();
  });

  it("scriptDirty 时显示未保存修改警示", () => {
    renderEditor({ scriptDirty: true });
    expect(
      screen.getByText("口播稿有未保存修改，请先保存"),
    ).toBeInTheDocument();
  });

  it("镜头映射列表正确渲染", () => {
    renderEditor({
      shotMappings: [
        { shotId: "S01", text: "开场镜头" },
        { shotId: "S02", text: "" },
      ],
    });

    const list = screen.getByRole("list", { name: "口播镜头映射" });
    expect(list).toBeInTheDocument();
    expect(screen.getByText("S01：开场镜头")).toBeInTheDocument();
    expect(screen.getByText("S02：（无口播）")).toBeInTheDocument();
  });

  it("空镜头映射不渲染列表", () => {
    renderEditor({ shotMappings: [] });
    expect(
      screen.queryByRole("list", { name: "口播镜头映射" }),
    ).not.toBeInTheDocument();
  });
});
