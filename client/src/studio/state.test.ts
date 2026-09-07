import { describe, expect, it } from "vitest";
import {
  buildOralInput,
  createDraft,
  createState,
  draftFromTask,
  patchStudioDraft,
  routeFromHash,
  withImportedProject,
} from "./state";

describe("V1.4 交接合同", () => {
  it("记录 Prompt 与文案的本地编辑状态，包括主动清空", () => {
    const draft = createDraft();
    const withPrompt = patchStudioDraft(draft, { prompt: "待编辑" });
    const clearedPrompt = patchStudioDraft(withPrompt, { prompt: "" });
    const changedScript = patchStudioDraft(clearedPrompt, {
      script: { ...clearedPrompt.script, title: "本地标题" },
    });

    expect(clearedPrompt.promptEdited).toBe(true);
    expect(changedScript.scriptEdited).toBe(true);
  });

  it("切换项目时清空未显式交接的旧项目文本与编辑标记", () => {
    const draft = {
      ...createDraft(),
      projectId: "project-a",
      prompt: "A Prompt",
      promptEdited: true,
      script: {
        ...createDraft().script,
        title: "A 标题",
        original: "A 原文",
        text: "A 文案",
      },
      scriptEdited: true,
    };

    const next = patchStudioDraft(draft, { projectId: "project-b" });

    expect(next.prompt).toBe("");
    expect(next.promptEdited).toBe(false);
    expect(next.script).toMatchObject({ title: "", original: "", text: "" });
    expect(next.scriptEdited).toBe(false);
  });

  it("项目、人物或目标图变化时使旧首帧确认与版本失效", () => {
    const confirmed = {
      ...createDraft(),
      projectId: "project-a",
      ipId: "person-a",
      imageId: "photo-a",
      firstFrameId: "frame-a",
      firstFrameSelectionVersionId: "ffv-a",
      frameConfirmed: true,
    };

    for (const patch of [
      { projectId: "project-b" },
      { ipId: "person-b" },
      { imageId: "photo-b" },
    ]) {
      const next = patchStudioDraft(confirmed, patch);
      expect(next.firstFrameId).toBeUndefined();
      expect(next.firstFrameSelectionVersionId).toBeUndefined();
      expect(next.frameConfirmed).toBe(false);
    }
  });

  it("同项目返回保留人物和未保存终稿，不回退到旧版本", () => {
    const state = createState("copy");
    state.draft = {
      ...state.draft,
      projectId: "p1",
      ipId: "zhang",
      voiceId: "voice",
      imageId: "photo",
      script: {
        ...state.draft.script,
        id: "script",
        text: "本地终稿",
        version: 3,
      },
    };
    const imported = {
      ...createDraft(),
      projectId: "p1",
      script: { ...state.draft.script, text: "旧版本", version: 2 },
    };
    const next = withImportedProject(state, imported);
    expect(next.draft.id).toBe(state.draft.id);
    expect(next.draft.ipId).toBe("zhang");
    expect(next.draft.voiceId).toBe("voice");
    expect(next.draft.imageId).toBe("photo");
    expect(next.draft.script.text).toBe("本地终稿");
  });
  it("切换项目清除上一项目的选择和返回上下文", () => {
    const state = {
      ...createState(),
      selectedTaskId: "old-task",
      selectedAssetId: "old-audio",
      selectedPersonId: "old-ip",
      selectedVideoId: "old-video",
      returnTo: "oral" as const,
    };
    const imported = { ...createDraft(), projectId: "new-project" };
    const next = withImportedProject(state, imported);
    expect(next.draft).toEqual(imported);
    for (const key of [
      "selectedTaskId",
      "selectedAssetId",
      "selectedPersonId",
      "selectedVideoId",
      "returnTo",
    ] as const)
      expect(next[key]).toBeUndefined();
  });
  it("历史任务再创作使用独立草稿，不把成片当上游视频", () => {
    const original = createDraft();
    original.sourceId = "source-original";
    original.script = {
      ...original.script,
      text: "旧任务的脚本",
      confirmed: true,
    };
    const next = draftFromTask({
      id: "task",
      title: "任务",
      type: "数字人口播",
      status: "completed",
      submitted: "today",
      driverMode: "audio",
      audioId: "speech-original",
      resultId: "final-video",
      ipId: "person-original",
      draftSnapshot: original,
    });
    expect(next.id).not.toBe(original.id);
    expect(next.sourceId).toBe("source-original");
    expect(next.audioId).toBe("speech-original");
    expect(next.voiceId).toBeUndefined();
    expect(next.script.confirmed).toBe(false);
    expect(next.script.text).toBe("旧任务的脚本");
  });
  it("首页默认工作台，旧路由有确定映射", () => {
    expect(routeFromHash("")).toBe("workbench");
    expect(routeFromHash("#projects")).toBe("replica");
    expect(routeFromHash("#studio/oral-audio")).toBe("oral-audio");
    expect(routeFromHash("#admin")).toBe("workbench");
  });
  it("更换IP清除旧分身与声音并使报价失效", () => {
    const draft = {
      ...createDraft(),
      ipId: "a",
      avatarId: "avatar-a",
      voiceId: "voice-a",
    };
    const next = patchStudioDraft(draft, { ipId: "b" });
    expect(next.avatarId).toBeUndefined();
    expect(next.voiceId).toBeUndefined();
    expect(next.quoteRevision).toBe(draft.quoteRevision + 1);
    expect(next.id).toBe(draft.id);
  });
  it("照片带入仅修改目标，不覆盖原始画面与选中镜头", () => {
    const draft = {
      ...createDraft(),
      originalImageId: "original",
      selectedShotId: "shot-2",
    };
    const next = patchStudioDraft(draft, { imageId: "target" });
    expect(next.originalImageId).toBe("original");
    expect(next.selectedShotId).toBe("shot-2");
  });
  it("音频驱动不携带文案/TTS/声音/模板字段", () => {
    const draft = {
      ...createDraft(),
      audioId: "speech",
      avatarId: "avatar",
      voiceId: "voice",
      ipId: "ip",
    };
    expect(buildOralInput(draft, "audio")).toEqual({
      draftId: draft.id,
      mode: "audio",
      ipId: "ip",
      avatarId: "avatar",
      audioAssetId: "speech",
    });
  });
  it("文案模式需要已确认终稿和声音，不提交音频", () => {
    const draft = createDraft();
    expect(() => buildOralInput(draft, "text")).toThrow("终稿");
    const ready = {
      ...draft,
      ipId: "ip",
      avatarId: "avatar",
      voiceId: "voice",
      audioId: "irrelevant",
      script: { ...draft.script, confirmed: true, text: "我是张工" },
    };
    const input = buildOralInput(ready, "text");
    expect(input).not.toHaveProperty("audioAssetId");
    expect(input).toHaveProperty("scriptVersion", ready.script.version);
  });
});
