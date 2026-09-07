import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CurrentUser,
  GenerationBatch,
  GenerationBatchListPage,
  GenerationTask,
  MaterialItem,
  OralAvatarRecord,
  OralTaskRecord,
  OralVoiceRecord,
  Project,
  SimpleLibraryEntry,
  SimpleSceneLook,
} from "../api";
import * as live from "./live";
import { createDraft } from "./state";

const api = vi.hoisted(() => ({
  compileGenerationPrompt: vi.fn(),
  createGenerationBatch: vi.fn(),
  createScriptVersion: vi.fn(),
  defaultBatchProvider: vi.fn(async () => "metaso"),
  lockGenerationPrompt: vi.fn(),
  reviseGenerationPrompt: vi.fn(),
  createGenerationResultPreviewUrl: vi.fn(),
  createGenerationTaskPreviewUrl: vi.fn(),
  downloadMaterialAsset: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getCachedCharacterAssetUrl: vi.fn(),
  getGenerationBatch: vi.fn(),
  getOralTask: vi.fn(),
  getLatestProjectAnalysis: vi.fn(),
  getStudioDraft: vi.fn(),
  getStudioAnalytics: vi.fn(async () => null),
  getStudioStats: vi.fn(async () => null),
  getLatestScriptVersion: vi.fn(),
  listCharacterSceneLooks: vi.fn(),
  listCharacterSceneLooksPage: vi.fn(),
  listGenerationBatches: vi.fn(),
  listMaterials: vi.fn(),
  listOralAvatars: vi.fn<() => Promise<OralAvatarRecord[]>>(async () => []),
  listOralTasks: vi.fn<() => Promise<OralTaskRecord[]>>(async () => []),
  listOralTasksPage: vi.fn(),
  listOralVoices: vi.fn<() => Promise<OralVoiceRecord[]>>(async () => []),
  listViralVideos: vi.fn(),
  listProjects: vi.fn(),
  listSimpleCharacterLibrary: vi.fn(),
  listSimpleCharacterLibraryPage: vi.fn(),
  readAnalysisPayload: vi.fn(),
  cancelGenerationBatch: vi.fn(),
  cancelOralTask: vi.fn(),
  retryOralTask: vi.fn(),
  retryOralTaskArchive: vi.fn(),
  resolveMaterials: vi.fn(),
}));

vi.mock("../api", () => api);

import {
  cancelStudioTask,
  downloadStudioTaskResult,
  loadCloudDraft,
  loadDraftMaterials,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadMorePeople,
  loadPersonAssets,
  loadProjectDraft,
  loadStudioData,
  loadStudioTaskDetail,
  loadTaskPreview,
  reloadTasks,
  retryStudioTask,
} from "./live";
import type { StudioTask } from "./types";

const user: CurrentUser = {
  id: "user-1",
  username: "owner",
  display_name: "业主",
  role: "customer",
};

const project: Project = {
  id: "project-1",
  owner_user_id: "user-1",
  name: "三层新中式乡墅",
  status: "READY",
  reference_asset_id: "source-video-1",
  reference_upload_status: "READY",
  analysis_status: "READY",
};

const person: SimpleLibraryEntry = {
  identity_id: "person-1",
  persona_id: "persona-1",
  version_number: 1,
  display_name: "张工",
  role: "乡墅设计师",
  service_scope: "乡墅设计",
  target_audience: "准备建房家庭",
  expression_style: "专业直白",
  owner_user_id: "user-1",
  status: "PUBLISHED",
  contact_sheet_asset_id: "sheet-1",
  generation_source: "image_provider",
  views: [
    { view_type: "FRONT_FACE", asset_id: "face-1" },
    { view_type: "FRONT_FULL", asset_id: "full-1" },
  ],
};

describe("任务详情按稳定对象类型读取", () => {
  it("普通生成按 batch id、口播按 oral task id 加载且不串型", async () => {
    api.getGenerationBatch.mockResolvedValue({
      id: "batch-deep",
      project_id: "project-1",
      prompt_version_id: "prompt-1",
      status: "SUCCEEDED",
      quantity: 1,
      stale: false,
      display_name: "深页普通生成",
      creation_kind: "independent",
      progress: {
        total_count: 1,
        terminal_count: 1,
        progress_percent: 100,
        counts: { SUCCEEDED: 1 },
      },
      tasks: [
        {
          id: "generation-task-1",
          status: "SUCCEEDED",
          archive_status: "ARCHIVED",
          quality_status: "PASSED",
          quality_issue_codes: [],
          result_asset_id: "result-1",
          direct_result_available: false,
          stage: "COMPLETED",
          provider: "metaso",
          model: "h3",
          provider_task_id_tail: null,
          attempt: 1,
          archive_retry_count: 0,
          estimated_cost: null,
          actual_cost: null,
          error_code: null,
          error_message_redacted: null,
          submitted_at: "2026-09-08T00:00:00Z",
          started_at: null,
          completed_at: "2026-09-08T00:01:00Z",
          duration_seconds: 8,
          retry_of_task_id: null,
          superseded_by_task_id: null,
          superseded_at: null,
          retry_reason: null,
          retry_requested_at: null,
          available_actions: [],
          prompt_snapshot: null,
        },
      ],
    } satisfies GenerationBatch);
    api.getOralTask.mockResolvedValue({
      id: "oral-deep",
      status: "SUCCEEDED",
      title: "深页口播",
      mode: "AUDIO",
      identity_id: "person-1",
      avatar_id: "avatar-1",
      voice_id: null,
      script_text: null,
      audio_asset_id: "audio-1",
      result_asset_id: "oral-result",
      duration_sec: 30,
      estimated_cost_fen: 100,
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:01:00Z",
    } satisfies OralTaskRecord);

    await expect(
      loadStudioTaskDetail("generation_batch", "batch-deep"),
    ).resolves.toMatchObject({
      id: "batch-deep",
      backendKind: "generation_batch",
      backendId: "batch-deep",
      type: "视频生成",
    });
    await expect(
      loadStudioTaskDetail("oral_task", "oral-deep"),
    ).resolves.toMatchObject({
      id: "oral-oral-deep",
      backendKind: "oral_task",
      backendId: "oral-deep",
      type: "数字人口播",
    });
    expect(api.getGenerationBatch).toHaveBeenCalledWith("batch-deep");
    expect(api.getOralTask).toHaveBeenCalledWith("oral-deep");
  });
});

const batchPage = {
  next_cursor: null,
  items: [
    {
      id: "batch-1",
      project_id: "project-1",
      project_name: "三层新中式乡墅",
      created_by_user_id: "user-1",
      created_by_display_name: "业主",
      prompt_version_id: "prompt-1",
      status: "RUNNING",
      quantity: 2,
      created_at: "2026-09-05T09:30:00+08:00",
      updated_at: "2026-09-05T09:31:00+08:00",
      display_name: "庭院镜头生成",
      creation_kind: "replica",
      progress: {
        total_count: 2,
        terminal_count: 0,
        progress_percent: 37,
        counts: { RUNNING: 2 },
      },
      total_estimated_cost: null,
      total_actual_cost: null,
      needs_attention_count: 0,
      has_results: false,
      tasks: [],
    },
  ],
} as GenerationBatchListPage;

function generationTask(
  overrides: Partial<GenerationTask> = {},
): GenerationTask {
  return {
    id: "generation-task-1",
    status: "SUCCEEDED",
    archive_status: "ARCHIVED",
    quality_status: "PASSED",
    quality_issue_codes: [],
    result_asset_id: "result-1",
    direct_result_available: false,
    stage: "COMPLETED",
    provider: "minimax",
    model: "h3",
    provider_task_id_tail: "task-1",
    attempt: 1,
    archive_retry_count: 0,
    estimated_cost: null,
    actual_cost: null,
    error_code: null,
    error_message_redacted: null,
    submitted_at: "2026-09-05T09:30:00+08:00",
    started_at: "2026-09-05T09:30:10+08:00",
    completed_at: "2026-09-05T09:31:00+08:00",
    duration_seconds: 8,
    retry_of_task_id: null,
    superseded_by_task_id: null,
    superseded_at: null,
    retry_reason: null,
    retry_requested_at: null,
    available_actions: [],
    prompt_snapshot: null,
    ...overrides,
  };
}

function generationBatch(
  tasks: GenerationTask[],
  overrides: Partial<GenerationBatch> = {},
): GenerationBatch {
  return {
    id: "batch-1",
    project_id: "project-1",
    prompt_version_id: "prompt-1",
    status: "SUCCEEDED",
    quantity: tasks.length,
    stale: false,
    creation_kind: "replica",
    progress: {
      total_count: tasks.length,
      terminal_count: tasks.length,
      progress_percent: 100,
      counts: { succeeded: tasks.length },
    },
    tasks,
    ...overrides,
  };
}

const studioTask: StudioTask = {
  id: "batch-1",
  batchId: "batch-1",
  projectId: "project-1",
  title: "庭院镜头生成",
  type: "视频生成",
  status: "completed",
  submitted: "2026-09-05T09:30:00+08:00",
};

describe("真实 Studio 只读适配器", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.listProjects.mockResolvedValue([project]);
    api.listSimpleCharacterLibrary.mockResolvedValue([person]);
    api.listSimpleCharacterLibraryPage.mockImplementation(async (filters) => {
      const items = await api.listSimpleCharacterLibrary();
      const limit = filters?.limit ?? items.length;
      return {
        items: items.slice(0, limit),
        next_cursor: items.length > limit ? "people-next" : null,
        total: items.length,
      };
    });
    api.listCharacterSceneLooksPage.mockImplementation(async () => {
      const items = await api.listCharacterSceneLooks();
      return { items, total: items.length, limit: 12, offset: 0 };
    });
    api.listOralTasksPage.mockImplementation(async () => {
      const items = await api.listOralTasks();
      return { items, total: items.length, limit: 20, offset: 0 };
    });
    api.listOralAvatars.mockResolvedValue([]);
    api.listOralVoices.mockResolvedValue([]);
    api.listViralVideos.mockImplementation((platform: string) =>
      Promise.resolve({
        platform,
        sort: "hot",
        categories: [],
        items: [],
        fetchedAt: null,
      }),
    );
    api.listGenerationBatches.mockResolvedValue(batchPage);
    api.listMaterials.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 60,
      total: 0,
    });
    api.getAssetDownloadUrl.mockResolvedValue({ url: "https://signed/source" });
    api.getCachedCharacterAssetUrl.mockResolvedValue({
      url: "https://signed/character",
    });
    api.getLatestProjectAnalysis.mockResolvedValue({ id: "analysis-1" });
    api.createGenerationResultPreviewUrl.mockResolvedValue(
      "https://signed/result",
    );
    api.createGenerationTaskPreviewUrl.mockResolvedValue(
      "https://signed/direct",
    );
    api.readAnalysisPayload.mockReturnValue({
      summary: "预算拆解",
      duration_seconds: 42,
      original_script: "分析得到的原始口播。",
      shots: [],
    });
    api.getLatestScriptVersion.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: {
        id: "script-version-3",
        project_id: "project-1",
        asset_id: null,
        kind: "script",
        version_number: 3,
        payload: { full_text: "已经保存的二创终稿。" },
        created_by_user_id: "user-1",
        created_at: "2026-09-05T09:00:00+08:00",
      },
    });
  });

  it("兼容没有编辑标记的旧云端草稿，保留已有 Prompt 与文案", async () => {
    api.getStudioDraft.mockResolvedValue({
      draft_kind: "copy",
      payload: {
        id: "legacy-draft",
        projectId: "project-1",
        selectedShotId: "shot-1",
        prompt: "升级前保存的 Prompt",
        script: {
          id: "legacy-script",
          title: "升级前作品名",
          original: "升级前 ASR 原文",
          text: "升级前文案",
          version: 2,
          confirmed: false,
        },
        referenceIds: [],
        resolution: "768P",
        ratio: "16:9",
        duration: 8,
        count: 1,
        frameConfirmed: false,
        style: "standard",
        subtitles: false,
        quoteRevision: 3,
      },
      script_confirmed: false,
      revision: 4,
      updated_at: "2026-09-07T10:00:00+08:00",
    });

    const restored = await loadCloudDraft();

    expect(restored?.draft).toMatchObject({
      prompt: "升级前保存的 Prompt",
      promptEdited: true,
      scriptEdited: true,
      script: {
        title: "升级前作品名",
        original: "升级前 ASR 原文",
        text: "升级前文案",
      },
    });
  });

  it("恢复云端草稿时批量解析其中的素材引用", async () => {
    const material = {
      id: "asset:image-1",
      owner_user_id: "user-1",
      asset_id: "image-1",
      generation_task_id: null,
      project_id: null,
      person_id: null,
      title: "云端首帧.png",
      group: "我的上传",
      media_type: "image",
      source: "upload",
      status: "ready",
      delivery: "stored",
      content_type: "image/png",
      size_bytes: 128,
      duration_seconds: null,
      created_at: "2026-09-06 10:00:00",
      hidden: false,
      saved: true,
      allowed_uses: ["first_frame", "tail_frame", "reference"],
      allowed_actions: ["preview", "download", "rename", "hide"],
    } satisfies MaterialItem;
    api.resolveMaterials.mockResolvedValue({
      items: [material],
      unavailable_ids: ["asset:missing-audio"],
    });
    const draft = createDraft();
    draft.firstFrameId = "image-1";
    draft.tailFrameId = "image-1";
    draft.audioId = "missing-audio";
    draft.referenceIds = ["image-1"];

    const result = await loadDraftMaterials(draft);

    expect(api.resolveMaterials).toHaveBeenCalledWith([
      "asset:image-1",
      "asset:missing-audio",
    ]);
    expect(result.assets).toEqual([
      expect.objectContaining({
        id: "image-1",
        materialId: "asset:image-1",
        name: "云端首帧.png",
        kind: "image",
        saved: true,
        url: "https://signed/source",
      }),
    ]);
    expect(result.unavailableIds).toEqual(["missing-audio"]);
  });

  it("详情按需读取批次并优先预览成功的直出结果", async () => {
    api.getGenerationBatch.mockResolvedValue(
      generationBatch([
        generationTask({ id: "archived-first", result_asset_id: "asset-1" }),
        generationTask({
          id: "direct-second",
          result_asset_id: null,
          direct_result_available: true,
          archive_status: "NOT_ARCHIVED",
        }),
      ]),
    );

    const asset = await loadTaskPreview(studioTask);

    expect(api.getGenerationBatch).toHaveBeenCalledWith("batch-1");
    expect(api.createGenerationTaskPreviewUrl).toHaveBeenCalledWith(
      "direct-second",
    );
    expect(api.createGenerationResultPreviewUrl).not.toHaveBeenCalled();
    expect(asset).toEqual({
      id: "direct-task-direct-second",
      name: "庭院镜头生成 · 首个可用结果",
      kind: "video",
      url: "https://signed/direct",
      group: "任务结果",
      source: "任务中心",
      saved: false,
    });
  });

  it("没有直出结果时使用首个成功归档资产", async () => {
    api.getGenerationBatch.mockResolvedValue(
      generationBatch([
        generationTask({ status: "FAILED", result_asset_id: "failed-asset" }),
        generationTask({ id: "archived-ok", result_asset_id: "asset-ok" }),
      ]),
    );

    const asset = await loadTaskPreview(studioTask);

    expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledWith(
      "asset-ok",
    );
    expect(api.createGenerationTaskPreviewUrl).not.toHaveBeenCalled();
    expect(asset).toMatchObject({
      id: "asset-ok",
      url: "https://signed/result",
      saved: true,
      source: "任务中心",
    });
  });

  it("无批次或批次无成功结果时返回空且不签发预览", async () => {
    await expect(
      loadTaskPreview({ ...studioTask, batchId: undefined }),
    ).resolves.toBeUndefined();
    expect(api.getGenerationBatch).not.toHaveBeenCalled();

    api.getGenerationBatch.mockResolvedValue(
      generationBatch([
        generationTask({
          status: "FAILED",
          result_asset_id: null,
          direct_result_available: false,
        }),
      ]),
    );
    await expect(loadTaskPreview(studioTask)).resolves.toBeUndefined();
    expect(api.createGenerationResultPreviewUrl).not.toHaveBeenCalled();
    expect(api.createGenerationTaskPreviewUrl).not.toHaveBeenCalled();
  });

  it("口播成片预览和下载都直接签发 result_asset_id", async () => {
    const oralResult: StudioTask = {
      id: "oral-visible",
      backendKind: "oral_task",
      backendId: "oral-backend",
      backendStatus: "SUCCEEDED",
      title: "张工口播",
      type: "数字人口播",
      status: "completed",
      submitted: "2026-09-06T09:00:00Z",
      resultId: "oral-result-asset",
    };
    api.getAssetDownloadUrl.mockResolvedValue({
      url: "https://signed/oral-result",
    });
    await expect(loadTaskPreview(oralResult)).resolves.toMatchObject({
      id: "oral-result-asset",
      url: "https://signed/oral-result",
    });
    await downloadStudioTaskResult(oralResult);

    expect(api.getAssetDownloadUrl).toHaveBeenCalledWith("oral-result-asset");
    expect(api.downloadMaterialAsset).toHaveBeenCalledWith(
      "oral-result-asset",
      "张工口播.mp4",
    );
    expect(api.getGenerationBatch).not.toHaveBeenCalled();
  });

  it("批次或签名请求失败时向上抛出以便界面明确重试", async () => {
    api.getGenerationBatch.mockRejectedValueOnce(new Error("batch timeout"));
    await expect(loadTaskPreview(studioTask)).rejects.toThrow("batch timeout");

    api.getGenerationBatch.mockResolvedValue(
      generationBatch([generationTask()]),
    );
    api.createGenerationResultPreviewUrl.mockRejectedValueOnce(
      new Error("sign timeout"),
    );
    await expect(loadTaskPreview(studioTask)).rejects.toThrow("sign timeout");
  });

  it("从真实分析与非过期脚本版本创建全新项目草稿", async () => {
    const result = await loadProjectDraft(project);

    expect(result.draft).toMatchObject({
      projectId: "project-1",
      sourceId: "source-video-1",
      script: {
        id: "script-version-3",
        title: "三层新中式乡墅",
        original: "分析得到的原始口播。",
        text: "已经保存的二创终稿。",
        version: 3,
        confirmed: false,
      },
    });
    expect(result.draft.id).toMatch(/^draft-/);
    expect(result.errors).toEqual([]);
  });

  it("没有保存版本时只把分析原文带入未确认的本地草稿", async () => {
    api.getLatestScriptVersion.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: null,
    });

    const result = await loadProjectDraft(project);

    expect(result.draft.script).toMatchObject({
      title: "三层新中式乡墅",
      original: "分析得到的原始口播。",
      text: "分析得到的原始口播。",
      version: 1,
      confirmed: false,
    });
    expect(result.draft.script.id).toMatch(/^script-/);
  });

  it("分析未就绪时不请求无意义端点，脚本失败只返回新草稿和明确错误", async () => {
    api.getLatestScriptVersion.mockRejectedValue(new Error("script 404"));

    const result = await loadProjectDraft({
      ...project,
      analysis_status: "PENDING",
    });

    expect(api.getLatestProjectAnalysis).not.toHaveBeenCalled();
    expect(result.draft).toMatchObject({
      projectId: "project-1",
      sourceId: "source-video-1",
      script: { original: "", text: "", confirmed: false },
    });
    expect(result.errors).toEqual(["读取项目已保存文案失败：script 404"]);
  });

  it("过期或字段不明的脚本版本不作为终稿恢复", async () => {
    api.getLatestScriptVersion.mockResolvedValue({
      stale: true,
      stale_reasons: ["shot cards changed"],
      version: {
        id: "stale-script",
        version_number: 9,
        payload: { text: "不能猜成 full_text" },
      },
    });

    const result = await loadProjectDraft(project);

    expect(result.draft.script.id).toMatch(/^script-/);
    expect(result.draft.script).toMatchObject({
      original: "分析得到的原始口播。",
      text: "分析得到的原始口播。",
      version: 1,
      confirmed: false,
    });
  });

  it("映射项目、单张五视图合成图和真实批次进度", async () => {
    const data = await loadStudioData(user);

    expect(api.listGenerationBatches).toHaveBeenCalledWith({
      limit: 20,
    });
    expect(data.projects).toEqual([project]);
    expect(data.people).toHaveLength(1);
    expect(data.people[0]?.sheetId).toBe("sheet-1");
    expect(data.assets.filter((asset) => asset.composite)).toEqual([
      expect.objectContaining({
        id: "sheet-1",
        url: "https://signed/character",
      }),
    ]);
    expect(data.tasks).toEqual([
      expect.objectContaining({
        id: "batch-1",
        batchId: "batch-1",
        progress: 37,
        status: "running",
        title: "庭院镜头生成",
        type: "视频复刻",
      }),
    ]);
    expect(data.stats).toBeNull();
    expect(data.errors).toEqual([]);
  });

  it("四类历史使用独立分页范围并保留服务端 total", async () => {
    const people = Array.from({ length: 9 }, (_, index) => ({
      ...person,
      identity_id: `person-${index + 1}`,
      display_name: `人物${index + 1}`,
    }));
    const generationItems = Array.from({ length: 21 }, (_, index) => ({
      ...batchPage.items[0],
      id: `batch-${index + 1}`,
      created_at: `2026-09-${String(30 - index).padStart(2, "0")}T09:30:00+08:00`,
    }));
    const oralItems: OralTaskRecord[] = Array.from(
      { length: 21 },
      (_, index) => ({
        id: `oral-${index + 1}`,
        status: "SUCCEEDED",
        title: `口播${index + 1}`,
        mode: "TTS",
        identity_id: "person-1",
        avatar_id: "avatar-1",
        voice_id: "voice-1",
        script_text: "测试口播",
        audio_asset_id: null,
        result_asset_id: `oral-result-${index + 1}`,
        duration_sec: 8,
        estimated_cost_fen: 8,
        created_at: `2026-08-${String(30 - index).padStart(2, "0")}T09:30:00+08:00`,
        updated_at: "2026-09-01T09:30:00+08:00",
      }),
    );
    const scenes: SimpleSceneLook[] = Array.from(
      { length: 13 },
      (_, index) => ({
        identity_id: "person-1",
        persona_id: `scene-${index + 1}`,
        character_version_id: `scene-version-${index + 1}`,
        scene_name: `场景${index + 1}`,
        scene_description: "庭院",
        costume_description: "工装",
        contact_sheet_asset_id: `scene-sheet-${index + 1}`,
        generation_source: "image_provider",
        views: [
          {
            view_type: "FRONT_FACE",
            asset_id: `scene-asset-${index + 1}`,
          },
        ],
        published_at: "2026-09-01T09:30:00+08:00",
      }),
    );
    api.listSimpleCharacterLibraryPage
      .mockResolvedValueOnce({
        items: people.slice(0, 8),
        next_cursor: "people-next",
        total: 9,
      })
      .mockResolvedValueOnce({
        items: people.slice(8),
        next_cursor: null,
        total: 9,
      });
    api.listGenerationBatches
      .mockResolvedValueOnce({
        ...batchPage,
        items: generationItems.slice(0, 20),
        next_cursor: "batch-next",
        total: 21,
      })
      .mockResolvedValueOnce({
        ...batchPage,
        items: generationItems.slice(20),
        next_cursor: null,
        total: 21,
      });
    api.listOralTasksPage
      .mockResolvedValueOnce({
        items: oralItems.slice(0, 20),
        total: 21,
        limit: 20,
        offset: 0,
      })
      .mockResolvedValueOnce({
        items: oralItems.slice(20),
        total: 21,
        limit: 20,
        offset: 20,
      });
    api.listCharacterSceneLooksPage
      .mockResolvedValueOnce({
        items: scenes.slice(0, 12),
        total: 13,
        limit: 12,
        offset: 0,
      })
      .mockResolvedValueOnce({
        items: scenes.slice(12),
        total: 13,
        limit: 12,
        offset: 12,
      });

    const data = await loadStudioData(user);
    expect(data.pagination).toEqual({
      people: { nextCursor: "people-next", total: 9 },
      scenes: {},
      generationTasks: { nextCursor: "batch-next", total: 21 },
      oralTasks: { loaded: 20, total: 21 },
    });

    const nextPeople = await loadMorePeople("people-next");
    expect(nextPeople).toMatchObject({
      people: [expect.objectContaining({ id: "person-9" })],
      nextCursor: null,
      total: 9,
    });
    const firstScenes = await loadPersonAssets("person-1");
    const lastScenes = await loadPersonAssets("person-1", 12);
    expect(
      new Set(
        [...firstScenes.assets, ...lastScenes.assets].map((asset) => asset.id),
      ),
    ).toHaveLength(13);
    expect(api.listCharacterSceneLooksPage).toHaveBeenCalledWith("person-1", {
      limit: 12,
      offset: 12,
    });
    const lastGeneration = await loadMoreGenerationTasks("batch-next");
    expect(lastGeneration.items).toHaveLength(1);
    expect(api.listGenerationBatches).toHaveBeenLastCalledWith({
      limit: 20,
      cursor: "batch-next",
    });
    await expect(loadMoreOralTasks(20)).resolves.toMatchObject({
      items: [expect.objectContaining({ id: "oral-oral-21" })],
      loaded: 21,
      total: 21,
    });
  });

  it("一个爆款平台失败时保留另一平台并上报错误", async () => {
    api.listViralVideos.mockImplementation((platform: string) => {
      if (platform === "wechat_channels") {
        return Promise.reject(new Error("channels timeout"));
      }
      return Promise.resolve({
        platform,
        sort: "hot",
        categories: ["建房预算"],
        fetchedAt: "2026-09-06T10:00:00Z",
        items: [
          {
            platform: "douyin",
            videoId: "douyin-1",
            category: "建房预算",
            title: "预算拆解",
            author: "张工",
            authorAvatar: null,
            verified: false,
            coverUrl: null,
            durationMs: 30_000,
            likes: 100,
            comments: 10,
            shares: 5,
            collects: 8,
            publishedAt: null,
            publishedDisplay: null,
            likeDisplay: null,
            tags: [],
            hasPlayableAudio: true,
            playUrl: null,
          },
        ],
      });
    });

    const data = await loadStudioData(user);

    expect(data.videos).toHaveLength(1);
    expect(data.videos[0]?.nativeId).toBe("douyin-1");
    expect(data.errors).toContain("读取视频号爆款失败：channels timeout");
  });

  it("核心工作台数据可不等待慢爆款请求", async () => {
    api.listViralVideos.mockReturnValue(new Promise(() => {}));

    const data = await loadStudioData(user, { includeViral: false });

    expect(data.projects).toEqual([project]);
    expect(data.videos).toEqual([]);
    expect(api.listViralVideos).not.toHaveBeenCalled();
  });

  it("把真实声音和分身按人物绑定并加载可预览素材", async () => {
    api.listOralAvatars.mockResolvedValue([
      {
        id: "avatar-1",
        identity_id: "person-1",
        title: "张工照片分身",
        status: "READY",
        source_kind: "IMAGE",
        source_asset_id: "scene-asset-1",
        error_message: null,
        created_at: "2026-09-06T10:00:00Z",
        updated_at: "2026-09-06T10:03:00Z",
      },
    ] as OralAvatarRecord[]);
    api.listOralVoices.mockResolvedValue([
      {
        id: "voice-1",
        identity_id: "person-1",
        title: "张工本人音色",
        status: "READY",
        source_asset_id: "voice-source-1",
        demo_asset_id: null,
        confirmed: 1,
        error_message: null,
        created_at: "2026-09-06T10:00:00Z",
        updated_at: "2026-09-06T10:02:00Z",
      },
    ] as OralVoiceRecord[]);

    const data = await loadStudioData(user);

    expect(api.listOralAvatars).toHaveBeenCalledWith("person-1");
    expect(api.listOralVoices).toHaveBeenCalledWith("person-1");
    expect(data.people[0]?.avatars).toEqual([
      expect.objectContaining({
        id: "avatar-1",
        imageId: "scene-asset-1",
        origin: "照片制作",
        ready: true,
        status: "READY",
      }),
    ]);
    expect(data.people[0]?.voices).toEqual([
      expect.objectContaining({
        id: "voice-1",
        confirmed: false,
        status: "READY",
        url: undefined,
      }),
    ]);
    expect(data.assets).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: "scene-asset-1",
          kind: "image",
          personId: "person-1",
        }),
        expect.objectContaining({
          id: "voice-source-1",
          kind: "audio",
          personId: "person-1",
        }),
      ]),
    );
  });

  it("部分成功且仍需处理的批次不会冒充已完成", async () => {
    api.listGenerationBatches.mockResolvedValue({
      ...batchPage,
      items: [
        {
          ...batchPage.items[0],
          status: "COMPLETED_WITH_FAILURES",
          needs_attention_count: 1,
          has_results: true,
          progress: {
            total_count: 2,
            terminal_count: 2,
            progress_percent: 100,
            counts: { SUCCEEDED: 1, FAILED: 1 },
          },
          tasks: [
            {
              id: "task-1",
              status: "SUCCEEDED",
              archive_status: "ARCHIVED",
              quality_status: "PASSED",
              quality_issue_codes: [],
              result_asset_id: "result-1",
              direct_result_available: true,
              stage: "COMPLETED",
              provider: "minimax",
              model: "h3",
              provider_task_id_tail: "task-1",
              attempt: 1,
              archive_retry_count: 0,
              estimated_cost: null,
              actual_cost: null,
              error_code: null,
              error_message_redacted: null,
              submitted_at: "2026-09-05T09:30:00+08:00",
              started_at: "2026-09-05T09:30:10+08:00",
              completed_at: "2026-09-05T09:31:00+08:00",
              duration_seconds: 8,
              retry_of_task_id: null,
              superseded_by_task_id: null,
              superseded_at: null,
              retry_reason: null,
              retry_requested_at: null,
              available_actions: [],
            },
          ],
        },
      ],
    } satisfies GenerationBatchListPage);

    const data = await loadStudioData(user);

    expect(data.tasks).toEqual([
      expect.objectContaining({
        id: "batch-1",
        status: "failed",
        progress: 100,
        resultId: "result-1",
      }),
    ]);
  });

  it("局部API失败时保留其他真实数据并记录明确错误", async () => {
    api.listProjects.mockRejectedValue(new Error("project timeout"));
    api.listGenerationBatches.mockRejectedValue(new Error("batch timeout"));

    const data = await loadStudioData(user);

    expect(data.projects).toEqual([]);
    expect(data.people).toHaveLength(1);
    expect(data.tasks).toEqual([]);
    expect(data.errors).toEqual([
      "读取项目失败：project timeout",
      "读取任务失败：batch timeout",
    ]);
  });

  it("所有主列表失败时返回可识别空态，不回退审核夹具", async () => {
    api.listProjects.mockRejectedValue(new Error("projects unavailable"));
    api.listSimpleCharacterLibrary.mockRejectedValue(
      new Error("people unavailable"),
    );
    api.listGenerationBatches.mockRejectedValue(new Error("tasks unavailable"));

    const data = await loadStudioData(user);

    expect(data).toMatchObject({
      projects: [],
      people: [],
      assets: [],
      tasks: [],
      videos: [],
      loading: false,
    });
    expect(data.errors).toHaveLength(3);
  });

  it("限制首页数量及签名预览请求，避免无界N+1", async () => {
    api.listProjects.mockResolvedValue(
      Array.from({ length: 30 }, (_, index) => ({
        ...project,
        id: `project-${index}`,
        name: `项目${index}`,
        reference_asset_id: `source-${index}`,
      })),
    );
    api.listSimpleCharacterLibrary.mockResolvedValue(
      Array.from({ length: 20 }, (_, index) => ({
        ...person,
        identity_id: `person-${index}`,
        display_name: `人物${index}`,
        contact_sheet_asset_id: `sheet-${index}`,
        views: [{ view_type: "FRONT_FACE", asset_id: `face-${index}` }],
      })),
    );

    const data = await loadStudioData(user);

    expect(data.projects).toHaveLength(24);
    expect(data.people).toHaveLength(8);
    expect(api.getAssetDownloadUrl).toHaveBeenCalledTimes(8);
    expect(api.getCachedCharacterAssetUrl).toHaveBeenCalledTimes(16);
  });

  it("启动只读取60条素材元数据且不逐条签名", async () => {
    api.listMaterials.mockResolvedValue({
      items: Array.from(
        { length: 60 },
        (_, index) =>
          ({
            id: `asset:material-${index}`,
            owner_user_id: "user-1",
            asset_id: `material-${index}`,
            generation_task_id: null,
            project_id: null,
            person_id: null,
            title: `素材${index}.png`,
            group: "我的上传",
            media_type: "image",
            source: "upload",
            status: "ready",
            delivery: "stored",
            content_type: "image/png",
            size_bytes: 1024,
            duration_seconds: null,
            created_at: "2026-09-08T10:00:00+08:00",
            hidden: false,
            saved: true,
            allowed_uses: ["reference"],
            allowed_actions: ["preview"],
          }) satisfies MaterialItem,
      ),
      page: 1,
      page_size: 60,
      total: 60,
    });

    const data = await loadStudioData(user);

    expect(data.materials).toHaveLength(60);
    expect(api.listMaterials).toHaveBeenCalledWith({
      mediaType: "image",
      pageSize: 60,
    });
    expect(api.getAssetDownloadUrl).toHaveBeenCalledOnce();
    expect(api.getAssetDownloadUrl).toHaveBeenCalledWith("source-video-1");
  });

  it("场景形象照每个场景只取一张正面预览，不展开成五张", async () => {
    const scene: SimpleSceneLook = {
      identity_id: "person-1",
      persona_id: "persona-scene-1",
      character_version_id: "version-scene-1",
      scene_name: "设计室讲解",
      scene_description: "乡墅方案桌前讲解",
      costume_description: "深色西装",
      contact_sheet_asset_id: "scene-sheet-1",
      generation_source: "image_provider",
      views: [
        { view_type: "LEFT_SIDE", asset_id: "scene-left" },
        { view_type: "FRONT_FACE", asset_id: "scene-front" },
        { view_type: "FRONT_FULL", asset_id: "scene-full" },
      ],
    };
    api.listCharacterSceneLooks.mockResolvedValue([scene]);
    api.getCachedCharacterAssetUrl.mockResolvedValue({
      url: "https://signed/scene-front",
    });

    const result = await loadPersonAssets("person-1");

    expect(api.getCachedCharacterAssetUrl).toHaveBeenCalledTimes(1);
    expect(api.getCachedCharacterAssetUrl).toHaveBeenCalledWith("scene-front");
    expect(result.assets).toEqual([
      expect.objectContaining({
        id: "scene-front",
        name: "设计室讲解",
        composite: false,
        personId: "person-1",
        url: "https://signed/scene-front",
      }),
    ]);
    expect(result.errors).toEqual([]);
  });

  it("场景预览签名失败仍保留资产身份并报告错误", async () => {
    api.listCharacterSceneLooks.mockResolvedValue([
      {
        identity_id: "person-1",
        persona_id: "persona-1",
        character_version_id: "version-1",
        scene_name: "庭院讲解",
        scene_description: "庭院",
        costume_description: "西装",
        contact_sheet_asset_id: "scene-sheet",
        generation_source: "image_provider",
        views: [{ view_type: "FRONT_FACE", asset_id: "scene-front" }],
      } satisfies SimpleSceneLook,
    ]);
    api.getCachedCharacterAssetUrl.mockRejectedValue(new Error("sign failed"));

    const result = await loadPersonAssets("person-1");

    expect(result.assets).toEqual([
      expect.objectContaining({ id: "scene-front", url: undefined }),
    ]);
    expect(result.errors).toEqual(["读取场景图片“庭院讲解”失败：sign failed"]);
  });
});

describe("批次类型映射与取消", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.listProjects.mockResolvedValue([]);
    api.listSimpleCharacterLibrary.mockResolvedValue([]);
    api.listOralTasks.mockResolvedValue([]);
    api.listSimpleCharacterLibraryPage.mockImplementation(async (filters) => {
      const items = await api.listSimpleCharacterLibrary();
      const limit = filters?.limit ?? items.length;
      return {
        items: items.slice(0, limit),
        next_cursor: items.length > limit ? "people-next" : null,
        total: items.length,
      };
    });
    api.listOralTasksPage.mockImplementation(async () => {
      const items = await api.listOralTasks();
      return { items, total: items.length, limit: 20, offset: 0 };
    });
    api.listGenerationBatches.mockResolvedValue(batchPage);
    api.getStudioStats.mockResolvedValue(null);
  });

  it("类型按 creation_kind 映射，未知通道回退视频生成", async () => {
    api.listGenerationBatches.mockResolvedValue({
      next_cursor: null,
      items: [
        { ...batchPage.items[0], creation_kind: "replica" },
        {
          ...batchPage.items[0],
          id: "batch-future",
          display_name: "未知通道批次",
          creation_kind: "future_kind",
        },
      ],
    });

    const data = await loadStudioData(user);

    expect(data.tasks.map((task) => task.type)).toEqual([
      "视频复刻",
      "视频生成",
    ]);
  });

  it("取消排队批次调用服务端取消接口", async () => {
    api.cancelGenerationBatch.mockResolvedValue(undefined);
    await cancelStudioTask({
      id: "batch-1",
      batchId: "batch-1",
      title: "排队批次",
      type: "视频复刻",
      status: "queued",
      submitted: "2026-09-06T09:32:00",
    });
    expect(api.cancelGenerationBatch).toHaveBeenCalledWith("batch-1");
  });

  it("任务标识明确区分生成批次与口播任务", async () => {
    api.listOralTasks.mockResolvedValue([
      {
        id: "oral-1",
        status: "QUEUED",
        title: "张工口播",
        mode: "TTS",
        identity_id: "person-1",
        avatar_id: "avatar-1",
        voice_id: "voice-1",
        script_text: "正文",
        audio_asset_id: null,
        result_asset_id: null,
        duration_sec: null,
        estimated_cost_fen: 100,
        created_at: "2026-09-06T09:00:00Z",
        updated_at: "2026-09-06T09:00:00Z",
      },
    ] satisfies OralTaskRecord[]);

    const tasks = await reloadTasks(user);

    expect(tasks).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          backendKind: "generation_batch",
          backendId: "batch-1",
        }),
        expect.objectContaining({
          backendKind: "oral_task",
          backendId: "oral-1",
        }),
      ]),
    );
  });

  it("轮询时某类任务失败仍保留另一类", async () => {
    api.listGenerationBatches.mockRejectedValue(new Error("generation down"));
    api.listOralTasks.mockResolvedValue([
      {
        id: "oral-keep",
        status: "RUNNING",
        title: "保留口播",
        mode: "AUDIO",
        identity_id: "person-1",
        avatar_id: "avatar-1",
        voice_id: null,
        script_text: null,
        audio_asset_id: "audio-1",
        result_asset_id: null,
        duration_sec: null,
        estimated_cost_fen: 100,
        created_at: "2026-09-06T09:00:00Z",
        updated_at: "2026-09-06T09:01:00Z",
      },
    ] satisfies OralTaskRecord[]);

    await expect(reloadTasks(user)).resolves.toEqual([
      expect.objectContaining({
        backendKind: "oral_task",
        backendId: "oral-keep",
      }),
    ]);
  });

  it.each([
    ["SUBMITTING", "queued", undefined],
    ["RUNNING", "running", undefined],
    ["ARCHIVING", "running", undefined],
    ["SUBMISSION_UNCERTAIN", "uncertain", "retry"],
    ["ARCHIVE_FAILED", "uncertain", "archive-retry"],
  ] as const)(
    "映射口播新状态 %s",
    async (backendStatus, status, retryAction) => {
      api.listGenerationBatches.mockResolvedValue({ ...batchPage, items: [] });
      api.listOralTasks.mockResolvedValue([
        {
          id: `oral-${backendStatus}`,
          status: backendStatus,
          title: backendStatus,
          mode: "TTS",
          identity_id: "person-1",
          avatar_id: "avatar-1",
          voice_id: "voice-1",
          script_text: "正文",
          audio_asset_id: null,
          result_asset_id: null,
          duration_sec: null,
          estimated_cost_fen: 100,
          created_at: "2026-09-06T09:00:00Z",
          updated_at: "2026-09-06T09:01:00Z",
        },
      ] as OralTaskRecord[]);

      const [task] = await reloadTasks(user);

      expect(task).toEqual(
        expect.objectContaining({ status, backendStatus, retryAction }),
      );
    },
  );

  it("口播取消和重试按 backendKind/backendId 分派", async () => {
    const queuedOral: StudioTask = {
      id: "display-id-without-prefix",
      backendKind: "oral_task",
      backendId: "oral-real-id",
      backendStatus: "QUEUED",
      title: "排队口播",
      type: "数字人口播",
      status: "queued",
      submitted: "2026-09-06T09:00:00Z",
    };
    api.cancelOralTask.mockResolvedValue({
      id: "oral-real-id",
      status: "CANCELLED",
      billing_status: "RELEASED",
    });

    await expect(cancelStudioTask(queuedOral)).resolves.toEqual({
      billingStatus: "RELEASED",
    });
    expect(api.cancelOralTask).toHaveBeenCalledWith("oral-real-id");
    expect(api.cancelGenerationBatch).not.toHaveBeenCalled();

    await retryStudioTask({ ...queuedOral, retryAction: "retry" });
    await retryStudioTask({ ...queuedOral, retryAction: "archive-retry" });
    expect(api.retryOralTask).toHaveBeenCalledWith("oral-real-id");
    expect(api.retryOralTaskArchive).toHaveBeenCalledWith("oral-real-id");
  });
});

describe("runReplicaGeneration（复刻一键管线）", () => {
  beforeEach(() => vi.clearAllMocks());

  const baseInput = {
    promptText: "编辑后的提示词",
    originalScriptText: "原片口播稿",
    shotCardVersionId: "scv-1",
    firstFrameAssetId: "ff-1",
    outputDurationSeconds: 8,
    resolution: "768P" as const,
    ratio: "16:9" as const,
    quantity: 1,
    idempotencyKey: "replica-idempotency-1",
  };

  function mockHappyPath() {
    api.createScriptVersion.mockResolvedValue({
      id: "script-1",
      payload: { shot_card_version_id: "scv-1" },
    });
    api.compileGenerationPrompt.mockResolvedValue({
      id: "prompt-compiled",
      payload: { prompt_text: "编译产物提示词" },
    });
    api.reviseGenerationPrompt.mockResolvedValue({
      id: "prompt-revised",
      payload: { prompt_text: "编辑后的提示词" },
    });
    api.lockGenerationPrompt.mockResolvedValue({
      id: "prompt-revised",
      payload: { prompt_text: "编辑后的提示词" },
    });
    api.createGenerationBatch.mockResolvedValue({ id: "batch-r1" });
  }

  it("编辑过 Prompt 时走 revise 再锁定，建批引用锁定版本", async () => {
    mockHappyPath();
    const batch = await live.runReplicaGeneration("project-1", baseInput);

    expect(api.createScriptVersion).toHaveBeenCalledWith("project-1", {
      source: "original",
      text: "原片口播稿",
      shot_card_version_id: "scv-1",
    });
    expect(api.compileGenerationPrompt).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        script_version_id: "script-1",
        shot_card_version_id: "scv-1",
        first_frame_asset_id: "ff-1",
      }),
    );
    expect(api.reviseGenerationPrompt).toHaveBeenCalledWith("project-1", {
      base_prompt_version_id: "prompt-compiled",
      prompt_text: "编辑后的提示词",
    });
    expect(api.lockGenerationPrompt).toHaveBeenCalledWith(
      "project-1",
      "prompt-revised",
    );
    expect(api.createGenerationBatch).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ idempotency_key: "replica-idempotency-1" }),
    );
    expect(batch.id).toBe("batch-r1");
  });

  it("建批响应不确定时复用已冻结的完整请求", async () => {
    api.createScriptVersion.mockResolvedValue({
      id: "script-first",
      payload: { shot_card_version_id: "scv-1" },
    });
    api.compileGenerationPrompt.mockResolvedValue({
      id: "prompt-compiled-first",
      payload: { prompt_text: "编译产物提示词" },
    });
    api.reviseGenerationPrompt.mockResolvedValue({
      id: "prompt-revised-first",
      payload: { prompt_text: "编辑后的提示词" },
    });
    api.lockGenerationPrompt.mockResolvedValue({ id: "prompt-locked-first" });
    api.createGenerationBatch
      .mockRejectedValueOnce(new Error("提交结果未知，请安全重试。"))
      .mockResolvedValueOnce({ id: "batch-replayed" });

    await expect(
      live.runReplicaGeneration("project-1", baseInput),
    ).rejects.toThrow("提交结果未知");
    await expect(
      live.runReplicaGeneration("project-1", {
        ...baseInput,
        idempotencyKey: "replica-key-after-reenter",
      }),
    ).resolves.toEqual({ id: "batch-replayed" });

    expect(api.createScriptVersion).toHaveBeenCalledOnce();
    expect(api.compileGenerationPrompt).toHaveBeenCalledOnce();
    expect(api.reviseGenerationPrompt).toHaveBeenCalledOnce();
    expect(api.lockGenerationPrompt).toHaveBeenCalledOnce();
    expect(api.createGenerationBatch).toHaveBeenCalledTimes(2);
    expect(api.createGenerationBatch.mock.calls[1]).toEqual(
      api.createGenerationBatch.mock.calls[0],
    );
  });

  it("Prompt 未编辑时跳过 revise 直接锁定编译产物", async () => {
    mockHappyPath();
    await live.runReplicaGeneration("project-1", {
      ...baseInput,
      promptText: "编译产物提示词",
    });

    expect(api.reviseGenerationPrompt).not.toHaveBeenCalled();
    expect(api.lockGenerationPrompt).toHaveBeenCalledWith(
      "project-1",
      "prompt-compiled",
    );
  });

  it("原稿为空时回退纯画面叙事文案并以 custom 来源存稿", async () => {
    mockHappyPath();
    await live.runReplicaGeneration("project-1", {
      ...baseInput,
      originalScriptText: "",
    });

    expect(api.createScriptVersion).toHaveBeenCalledWith("project-1", {
      source: "custom",
      text: "",
      shot_card_version_id: "scv-1",
    });
  });
});

describe("buildReplicaPromptText（拆解 Prompt 文本）", () => {
  it("逐镜头拼接时间/景别/动作/台词并附原口播稿", async () => {
    const { buildReplicaPromptText } = await import("./state");
    const text = buildReplicaPromptText(
      [
        {
          shot_id: "s1",
          start_time: 0,
          end_time: 8,
          shot_type: "中景",
          composition: "",
          camera_motion: "推进",
          subject: "院落",
          action: "缓推",
          scene: "庭院",
          spoken_text: "采光设计",
          transition: "切镜",
        },
      ],
      "原片口播",
    );
    expect(text).toContain("【镜头 1】0.0s–8.0s");
    expect(text).toContain("景别/构图：中景");
    expect(text).toContain("动作：缓推");
    expect(text).toContain("台词：采光设计");
    expect(text).toContain("【原片口播稿】");
    expect(text).toContain("原片口播");
  });
});
