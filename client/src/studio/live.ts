import {
  type CurrentUser,
  cancelGenerationBatch,
  cancelOralTask,
  compileGenerationPrompt,
  completeMaterialUpload,
  completeVideoUpload,
  createGenerationBatch,
  createGenerationResultPreviewUrl,
  createGenerationTaskPreviewUrl,
  createMaterialUploadIntent,
  createProject,
  createScriptFromAudioTask,
  createScriptVersion,
  createVideoUploadIntent,
  defaultBatchProvider,
  downloadMaterialAsset,
  type GenerationBatch,
  type GenerationBatchInput,
  type GenerationBatchListItem,
  type GenerationRatio,
  getAssetDownloadUrl,
  getCachedCharacterAssetUrl,
  getGenerationBatch,
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  getLatestScriptFromAudioTask,
  getLatestScriptVersion,
  getOralTask,
  getScriptFromAudioTask,
  getStudioAnalytics,
  getStudioDraft,
  getStudioStats,
  listCharacterSceneLooksPage,
  listGenerationBatches,
  listMaterials,
  listOralAvatars,
  listOralTasksPage,
  listOralVoices,
  listProjects,
  listSimpleCharacterLibraryPage,
  listStudioSavedScripts,
  listViralVideos,
  lockGenerationPrompt,
  type MaterialItem,
  type OralAvatarRecord,
  type OralTaskRecord,
  type Project,
  readAnalysisPayload,
  resolveMaterials,
  retryOralTask,
  retryOralTaskArchive,
  reviseGenerationPrompt,
  type SimpleLibraryEntry,
  type StudioDraftKind,
  type StudioSavedScriptInput,
  saveStudioDraft,
  saveStudioSavedScript,
  uploadMaterial,
  uploadReferenceVideo,
  type ViralVideoItem,
} from "../api";
import { createDraft } from "./state";
import type {
  StudioAsset,
  StudioAvatar,
  StudioData,
  StudioDraft,
  StudioPerson,
  StudioScript,
  StudioStats,
  StudioTask,
  StudioVideo,
  StudioVoice,
} from "./types";

const projectLimit = 24;
const personLimit = 8;
const projectPreviewLimit = 8;
const sceneLimit = 12;
const MAX_ORAL_SOURCE_BYTES = 50 * 1024 * 1024;

type FrozenReplicaRequest = {
  fingerprint: string;
  idempotencyKey: string;
  projectId: string;
  request: GenerationBatchInput;
};

const frozenReplicaRequests = new Map<string, FrozenReplicaRequest>();
const frozenReplicaRequestsByContext = new Map<string, FrozenReplicaRequest>();

function replicaRequestContextKey(projectId: string, fingerprint: string) {
  return `${projectId}:${fingerprint}`;
}

function clearFrozenReplicaRequest(frozen: FrozenReplicaRequest) {
  if (frozenReplicaRequests.get(frozen.idempotencyKey) === frozen) {
    frozenReplicaRequests.delete(frozen.idempotencyKey);
  }
  const contextKey = replicaRequestContextKey(
    frozen.projectId,
    frozen.fingerprint,
  );
  if (frozenReplicaRequestsByContext.get(contextKey) === frozen) {
    frozenReplicaRequestsByContext.delete(contextKey);
  }
}

function errorText(error: unknown) {
  return error instanceof Error && error.message.trim()
    ? error.message.trim()
    : "未知错误";
}

const materialSourceLabels: Record<MaterialItem["source"], string> = {
  upload: "我的上传",
  project: "项目素材",
  character: "人物库",
  oral: "口播成片",
  generation: "生成结果",
};

function materialDuration(seconds: number | null) {
  if (seconds === null) return undefined;
  const rounded = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(rounded / 60)).padStart(2, "0")}:${String(
    rounded % 60,
  ).padStart(2, "0")}`;
}

export function studioAssetFromMaterial(item: MaterialItem): StudioAsset {
  return {
    id: item.asset_id ?? item.id,
    materialId: item.id,
    assetId: item.asset_id ?? undefined,
    generationTaskId: item.generation_task_id ?? undefined,
    name: item.title,
    kind: item.media_type,
    duration: materialDuration(item.duration_seconds),
    group: item.group,
    personId: item.person_id ?? undefined,
    source: materialSourceLabels[item.source],
    saved: item.saved,
    delivery: item.delivery,
    allowedUses: item.allowed_uses,
    allowedActions: item.allowed_actions,
  };
}

export type OralAudioPurpose = "oral_audio" | "voice_clone";

export function validateOralAudioFile(file: File) {
  if (!file.name.toLowerCase().endsWith(".mp3")) return "仅支持 MP3 音频。";
  if (file.size <= 0) return "上传文件不能为空。";
  if (file.size > MAX_ORAL_SOURCE_BYTES) return "上传文件不能超过 50 MB。";
  return undefined;
}

export function readAudioDuration(file: File): Promise<number> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const audio = document.createElement("audio");
    const cleanup = () => {
      audio.removeAttribute("src");
      URL.revokeObjectURL(url);
    };
    audio.preload = "metadata";
    audio.onloadedmetadata = () => {
      const duration = audio.duration;
      cleanup();
      if (Number.isFinite(duration) && duration > 0) resolve(duration);
      else reject(new Error("无法读取音频时长，请重新选择 MP3 文件。"));
    };
    audio.onerror = () => {
      cleanup();
      reject(new Error("无法读取音频时长，请重新选择 MP3 文件。"));
    };
    audio.src = url;
  });
}

function draftAssetIds(draft: StudioDraft): string[] {
  return [
    draft.sourceAssetId,
    draft.originalImageId,
    draft.imageId,
    draft.firstFrameId,
    draft.tailFrameId,
    draft.audioId,
    ...draft.referenceIds,
  ].filter((id): id is string => Boolean(id));
}

/** 草稿只保存物理资产 ID；恢复时由服务端重新校验归属并补齐元数据。 */
export async function loadDraftMaterials(draft: StudioDraft): Promise<{
  assets: StudioAsset[];
  unavailableIds: string[];
}> {
  const assetIds = [...new Set(draftAssetIds(draft))];
  if (!assetIds.length) return { assets: [], unavailableIds: [] };
  const materialIds = assetIds.map((id) =>
    id.startsWith("asset:") ? id : `asset:${id}`,
  );
  const resolved = await resolveMaterials(materialIds);
  const assets = resolved.items.map(studioAssetFromMaterial);
  const previewResults = await Promise.allSettled(
    assets.map((asset) =>
      asset.assetId
        ? getAssetDownloadUrl(asset.assetId).then((result) => result.url)
        : asset.generationTaskId
          ? createGenerationTaskPreviewUrl(asset.generationTaskId)
          : Promise.resolve(undefined),
    ),
  );
  return {
    assets: assets.map((asset, index) => ({
      ...asset,
      url:
        previewResults[index]?.status === "fulfilled"
          ? previewResults[index].value
          : undefined,
    })),
    unavailableIds: resolved.unavailable_ids.map((id) =>
      id.startsWith("asset:") ? id.slice("asset:".length) : id,
    ),
  };
}

export async function loadTaskPreview(
  task: StudioTask,
): Promise<StudioAsset | undefined> {
  // 数字人口播任务没有生成批次：成片按平台资产直取签名地址。
  if (task.backendKind === "oral_task" && task.resultId) {
    const url = (await getAssetDownloadUrl(task.resultId)).url;
    return {
      id: task.resultId,
      name: `${task.title} · 成片`,
      kind: "video",
      url,
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
  }
  if (task.backendKind === "oral_task") return undefined;
  if (!task.batchId) return undefined;

  const batch = await getGenerationBatch(task.batchId);
  const successful = batch.tasks.filter(
    (result) =>
      result.status === "SUCCEEDED" &&
      (result.direct_result_available || Boolean(result.result_asset_id)),
  );
  const result =
    successful.find((candidate) => candidate.direct_result_available) ??
    successful[0];
  if (!result) return undefined;

  const direct = result.direct_result_available;
  const assetId = result.result_asset_id;
  const url = direct
    ? await createGenerationTaskPreviewUrl(result.id)
    : await createGenerationResultPreviewUrl(assetId as string);
  return {
    id: direct ? `direct-task-${result.id}` : (assetId as string),
    name: `${task.title} · 首个可用结果`,
    kind: "video",
    url,
    group: "任务结果",
    source: "任务中心",
    saved: !direct,
  };
}

export async function loadProjectDraft(
  project: Project,
): Promise<{ draft: StudioDraft; errors: string[] }> {
  const draft = createDraft();
  draft.projectId = project.id;
  draft.sourceId = project.reference_asset_id ?? undefined;
  draft.script.title = project.name;

  const [analysisResult, scriptResult] = await Promise.allSettled([
    project.analysis_status === "READY"
      ? getLatestProjectAnalysis(project.id)
      : Promise.resolve(undefined),
    getLatestScriptVersion(project.id),
  ]);
  const errors: string[] = [];
  let original = "";

  if (analysisResult.status === "rejected") {
    errors.push(`读取项目拆解结果失败：${errorText(analysisResult.reason)}`);
  } else if (analysisResult.value) {
    original = readAnalysisPayload(analysisResult.value)?.original_script ?? "";
  }

  draft.script.original = original;
  draft.script.text = original;
  if (scriptResult.status === "rejected") {
    errors.push(`读取项目已保存文案失败：${errorText(scriptResult.reason)}`);
  } else {
    const state = scriptResult.value;
    const fullText = state.version?.payload.full_text;
    if (!state.stale && state.version && typeof fullText === "string") {
      draft.script.id = state.version.id;
      draft.script.text = fullText;
      draft.script.version = state.version.version_number;
    }
  }
  draft.script.confirmed = false;

  return { draft, errors };
}

async function signedUrl(
  assetId: string,
  loader: (id: string) => Promise<{ url: string }>,
): Promise<string | undefined> {
  return (await loader(assetId)).url || undefined;
}

function projectAsset(project: Project, url?: string): StudioAsset | undefined {
  if (!project.reference_asset_id) return undefined;
  return {
    id: project.reference_asset_id,
    name: `${project.name} · 来源视频`,
    kind: "video",
    url,
    group: project.name,
    source: "项目上传",
    saved: project.reference_upload_status === "READY",
  };
}

async function loadProjects(): Promise<{
  projects: Project[];
  assets: StudioAsset[];
  errors: string[];
}> {
  const projects = (await listProjects()).slice(0, projectLimit);
  const previewProjects = projects
    .filter((project) => project.reference_asset_id)
    .slice(0, projectPreviewLimit);
  const previews = await Promise.allSettled(
    previewProjects.map(async (project) => ({
      project,
      url: await signedUrl(
        project.reference_asset_id as string,
        getAssetDownloadUrl,
      ),
    })),
  );
  const previewUrls = new Map<string, string | undefined>();
  const errors: string[] = [];
  previews.forEach((result, index) => {
    const project = previewProjects[index];
    if (!project?.reference_asset_id) return;
    if (result.status === "fulfilled") {
      previewUrls.set(project.reference_asset_id, result.value.url);
    } else {
      errors.push(
        `读取项目预览“${project.name}”失败：${errorText(result.reason)}`,
      );
    }
  });
  const assets = projects
    .map((project) =>
      projectAsset(
        project,
        project.reference_asset_id
          ? previewUrls.get(project.reference_asset_id)
          : undefined,
      ),
    )
    .filter((asset): asset is StudioAsset => Boolean(asset));
  return { projects, assets, errors };
}

function basePerson(
  entry: SimpleLibraryEntry,
  portrait?: string,
  avatars: StudioAvatar[] = [],
  voices: StudioVoice[] = [],
): StudioPerson {
  return {
    id: entry.identity_id,
    name: entry.display_name,
    role: entry.role,
    portrait,
    version: entry.version_number ?? 0,
    scope: entry.service_scope,
    audience: entry.target_audience,
    expression: entry.expression_style,
    sheetId: entry.contact_sheet_asset_id ?? undefined,
    photoIds: [],
    avatars,
    voices,
  };
}

type OralIdentityAssets = {
  avatars: StudioAvatar[];
  voices: StudioVoice[];
  assets: StudioAsset[];
  errors: string[];
};

function oralStatusLabel(status: OralAvatarRecord["status"]): string {
  if (status === "READY") return "已就绪";
  if (status === "FAILED") return "制作失败";
  return "制作中";
}

async function loadOralIdentityAssets(
  identityId: string,
): Promise<OralIdentityAssets> {
  const [avatarResult, voiceResult] = await Promise.allSettled([
    listOralAvatars(identityId),
    listOralVoices(identityId),
  ]);
  const errors: string[] = [];
  const avatarRows =
    avatarResult.status === "fulfilled" ? avatarResult.value : [];
  const voiceRows = voiceResult.status === "fulfilled" ? voiceResult.value : [];
  if (avatarResult.status === "rejected") {
    errors.push(`读取口播分身失败：${errorText(avatarResult.reason)}`);
  }
  if (voiceResult.status === "rejected") {
    errors.push(`读取声音档案失败：${errorText(voiceResult.reason)}`);
  }

  const sourceAssets = new Map<
    string,
    { name: string; kind: "image" | "video" | "audio"; source: string }
  >();
  for (const avatar of avatarRows) {
    sourceAssets.set(avatar.source_asset_id, {
      name: `${avatar.title} · 制作素材`,
      kind: avatar.source_kind === "IMAGE" ? "image" : "video",
      source: "口播分身",
    });
  }
  for (const voice of voiceRows) {
    sourceAssets.set(voice.source_asset_id, {
      name: `${voice.title} · 声音样本`,
      kind: "audio",
      source: "声音档案",
    });
    if (voice.demo_asset_id) {
      sourceAssets.set(voice.demo_asset_id, {
        name: `${voice.title} · 试听`,
        kind: "audio",
        source: "声音档案",
      });
    }
  }
  const sourceEntries = [...sourceAssets.entries()];
  const previews = await Promise.allSettled(
    sourceEntries.map(([assetId]) => signedUrl(assetId, getAssetDownloadUrl)),
  );
  const previewUrls = new Map<string, string>();
  sourceEntries.forEach(([assetId, descriptor], index) => {
    const preview = previews[index];
    if (preview?.status === "fulfilled" && preview.value) {
      previewUrls.set(assetId, preview.value);
    } else if (preview?.status === "rejected") {
      errors.push(`读取“${descriptor.name}”失败：${errorText(preview.reason)}`);
    }
  });

  return {
    avatars: avatarRows.map((avatar) => ({
      id: avatar.id,
      name: avatar.title,
      imageId: avatar.source_asset_id,
      ready: avatar.status === "READY",
      status: avatar.status,
      error: avatar.error_message ?? undefined,
      origin: avatar.source_kind === "IMAGE" ? "照片制作" : "视频制作",
      duration: oralStatusLabel(avatar.status),
    })),
    voices: voiceRows.map((voice) => {
      const demoUrl = voice.demo_asset_id
        ? previewUrls.get(voice.demo_asset_id)
        : undefined;
      return {
        id: voice.id,
        name: voice.title,
        confirmed:
          voice.status === "READY" &&
          Boolean(voice.confirmed) &&
          Boolean(demoUrl),
        isDefault: false,
        status: voice.status,
        error: voice.error_message ?? undefined,
        url: demoUrl,
      };
    }),
    assets: sourceEntries.map(([id, descriptor]) => ({
      id,
      name: descriptor.name,
      kind: descriptor.kind,
      url: previewUrls.get(id),
      group: descriptor.source,
      personId: identityId,
      source: descriptor.source,
      saved: true,
    })),
    errors,
  };
}

async function loadPeople(): Promise<{
  people: StudioPerson[];
  assets: StudioAsset[];
  errors: string[];
  nextCursor: string | null;
  total: number;
}> {
  const page = await listSimpleCharacterLibraryPage({ limit: personLimit });
  return loadPeopleEntries(page.items, page.next_cursor, page.total);
}

async function loadPeopleEntries(
  entries: SimpleLibraryEntry[],
  nextCursor: string | null,
  total: number,
) {
  const errors: string[] = [];
  const people: StudioPerson[] = [];
  const assets: StudioAsset[] = [];

  for (const entry of entries) {
    const face =
      entry.views.find((view) => view.view_type === "FRONT_FACE") ??
      entry.views[0];
    const requests = await Promise.allSettled([
      face
        ? signedUrl(face.asset_id, getCachedCharacterAssetUrl)
        : Promise.resolve(undefined),
      entry.contact_sheet_asset_id
        ? signedUrl(entry.contact_sheet_asset_id, getCachedCharacterAssetUrl)
        : Promise.resolve(undefined),
      loadOralIdentityAssets(entry.identity_id),
    ]);
    const portrait =
      requests[0].status === "fulfilled" ? requests[0].value : undefined;
    const sheetUrl =
      requests[1].status === "fulfilled" ? requests[1].value : undefined;
    if (requests[0].status === "rejected") {
      errors.push(
        `读取人物头像“${entry.display_name}”失败：${errorText(requests[0].reason)}`,
      );
    }
    if (requests[1].status === "rejected") {
      errors.push(
        `读取人物五视图“${entry.display_name}”失败：${errorText(requests[1].reason)}`,
      );
    }
    const oral =
      requests[2].status === "fulfilled"
        ? requests[2].value
        : { avatars: [], voices: [], assets: [], errors: [] };
    if (requests[2].status === "rejected") {
      errors.push(
        `读取人物口播资产“${entry.display_name}”失败：${errorText(requests[2].reason)}`,
      );
    }
    errors.push(...oral.errors);
    people.push(basePerson(entry, portrait, oral.avatars, oral.voices));
    assets.push(...oral.assets);
    if (entry.contact_sheet_asset_id) {
      assets.push({
        id: entry.contact_sheet_asset_id,
        name: `${entry.display_name} · 基础五视图`,
        kind: "image",
        url: sheetUrl,
        group: "基础五视图",
        personId: entry.identity_id,
        composite: true,
        source: "人物库",
        saved: entry.status === "PUBLISHED",
      });
    }
  }
  return { people, assets, errors, nextCursor, total };
}

export async function loadMorePeople(cursor: string) {
  const page = await listSimpleCharacterLibraryPage({
    limit: personLimit,
    cursor,
  });
  return loadPeopleEntries(page.items, page.next_cursor, page.total);
}

function studioTaskStatus(
  batch: Pick<GenerationBatchListItem, "status" | "needs_attention_count">,
): StudioTask["status"] {
  const status = batch.status.toUpperCase();
  if (status === "SUBMISSION_UNCERTAIN") return "uncertain";
  if (status === "CANCELLED") return "cancelled";
  if (batch.needs_attention_count > 0) return "failed";
  if (status === "SUCCEEDED") return "completed";
  if (status === "COMPLETED_WITH_FAILURES") return "failed";
  if (status === "NEEDS_ATTENTION" || status === "FAILED") return "failed";
  if (status === "PENDING" || status === "QUEUED") return "queued";
  return "running";
}

/** 批次创作通道 → 任务中心类型页签文案（I13 类型保真）。
 * 服务端 058 起在 generation_batches.creation_kind 记录创建通道；
 * 未知通道回退到"视频生成"保持旧数据可见。 */
export const CREATION_KIND_LABELS: Record<string, StudioTask["type"]> = {
  replica: "视频复刻",
  independent: "视频生成",
  replacement: "人物置换",
};

function studioTask(batch: GenerationBatchListItem): StudioTask {
  return {
    id: batch.id,
    backendKind: "generation_batch",
    backendId: batch.id,
    backendStatus: batch.status,
    batchId: batch.id,
    projectId: batch.project_id,
    title: batch.display_name?.trim() || batch.project_name || batch.id,
    type: CREATION_KIND_LABELS[batch.creation_kind] ?? "视频生成",
    status: studioTaskStatus(batch),
    progress: batch.progress.progress_percent,
    submitted: batch.created_at,
    resultId:
      batch.tasks.find((task) => task.result_asset_id)?.result_asset_id ??
      undefined,
  };
}

function studioTaskFromBatch(batch: GenerationBatch): StudioTask {
  const needsAttention = batch.tasks.filter((task) =>
    ["FAILED", "NEEDS_ATTENTION", "SUBMISSION_UNCERTAIN"].includes(
      task.status.toUpperCase(),
    ),
  ).length;
  return {
    id: batch.id,
    backendKind: "generation_batch",
    backendId: batch.id,
    backendStatus: batch.status,
    batchId: batch.id,
    projectId: batch.project_id,
    title: batch.display_name?.trim() || batch.id,
    type: CREATION_KIND_LABELS[batch.creation_kind] ?? "视频生成",
    status: studioTaskStatus({
      status: batch.status,
      needs_attention_count: needsAttention,
    }),
    progress: batch.progress.progress_percent,
    submitted:
      batch.tasks.find((task) => task.submitted_at)?.submitted_at ?? "—",
    resultId:
      batch.tasks.find((task) => task.result_asset_id)?.result_asset_id ??
      undefined,
  };
}

/** 任务中心"取消任务"：仅服务端判定为仍可取消（全部任务未认领）的
 * 排队批次会成功，其余状态返回明确错误由调用方提示。 */
export async function cancelStudioTask(
  task: StudioTask,
): Promise<{ billingStatus?: string }> {
  if (task.backendKind === "oral_task") {
    if (task.backendStatus !== "QUEUED") {
      throw new Error("只有仍在排队的口播任务可以取消。");
    }
    const result = await cancelOralTask(task.backendId || task.id);
    return { billingStatus: result.billing_status ?? undefined };
  }
  await cancelGenerationBatch(task.backendId || task.batchId || task.id);
  return {};
}

async function loadTasks(_currentUser: CurrentUser) {
  // The authenticated server scope includes delegated project tasks; filtering
  // by creator here would silently hide work the current customer can access.
  const page = await listGenerationBatches({ limit: 20 });
  return {
    items: page.items.map(studioTask),
    nextCursor: page.next_cursor,
    total: page.total ?? page.items.length,
  };
}

/** 每轮同时刷新生成批次和口播任务；单边失败不丢弃另一边的有效结果。 */
export async function reloadTasks(
  currentUser: CurrentUser,
): Promise<StudioTask[]> {
  const [generationResult, oralResult] = await Promise.allSettled([
    loadTasks(currentUser),
    loadOralTasks(),
  ]);
  if (
    generationResult.status === "rejected" &&
    oralResult.status === "rejected"
  ) {
    throw generationResult.reason;
  }
  return [
    ...(generationResult.status === "fulfilled"
      ? generationResult.value.items
      : []),
    ...(oralResult.status === "fulfilled" ? oralResult.value.items : []),
  ];
}

/** Workbench quick upload: create a project, PUT the raw video to cloud
 * storage through the presigned intent, and hand back the source identity so
 * 后续复刻/文案提取都拿这个来源继续，而不是把大视频当解析输入。 */
export async function uploadWorkbenchSourceVideo(
  file: File,
  onProgress: (percent: number) => void,
  signal?: AbortSignal,
): Promise<{
  projectId: string;
  assetId: string;
  project?: Project;
  asset?: StudioAsset;
}> {
  const base = file.name.replace(/\.(mp4|mov)$/i, "").trim();
  const project = await createProject((base || file.name).slice(0, 120));
  const intent = await createVideoUploadIntent(project.id, file);
  let assetId = intent.asset_id;
  if (intent.upload_required !== false) {
    await uploadReferenceVideo(intent, file, onProgress, signal);
    const completed = await completeVideoUpload(intent.asset_id);
    assetId = completed.asset_id;
  }
  const uploadedProject: Project = {
    ...project,
    reference_asset_id: assetId,
    reference_upload_status: "READY",
  };
  return {
    projectId: project.id,
    assetId,
    project: uploadedProject,
    asset: projectAsset(uploadedProject),
  };
}

function oralTask(row: OralTaskRecord): StudioTask {
  const statusMap: Record<OralTaskRecord["status"], StudioTask["status"]> = {
    QUEUED: "queued",
    SUBMITTING: "queued",
    RUNNING: "running",
    ARCHIVING: "running",
    SUBMISSION_UNCERTAIN: "uncertain",
    ARCHIVE_FAILED: "uncertain",
    SUCCEEDED: "completed",
    FAILED: "failed",
    CANCELLED: "cancelled",
  };
  const availableActions = row.available_actions ?? [];
  const retryAction =
    row.status === "ARCHIVE_FAILED" ||
    availableActions.includes("archive_retry")
      ? "archive-retry"
      : row.status === "SUBMISSION_UNCERTAIN" ||
          availableActions.includes("retry")
        ? "retry"
        : undefined;
  return {
    id: `oral-${row.id}`,
    backendKind: "oral_task",
    backendId: row.id,
    backendStatus: row.status,
    billingStatus: row.billing_status ?? undefined,
    retryAction,
    batchId: undefined,
    title: row.title,
    type: "数字人口播",
    status: statusMap[row.status] ?? "running",
    submitted: row.created_at,
    resultId: row.result_asset_id ?? undefined,
    driverMode: row.mode === "AUDIO" ? "audio" : "text",
    ipId: row.identity_id,
    avatarId: row.avatar_id,
    voiceId: row.voice_id ?? undefined,
    audioId: row.audio_asset_id ?? undefined,
    scriptText: row.script_text ?? undefined,
  };
}

async function loadOralTasks() {
  const page = await listOralTasksPage({ limit: 20 });
  return {
    items: page.items.map(oralTask),
    loaded: page.items.length,
    total: page.total ?? page.items.length,
  };
}

export async function loadMoreGenerationTasks(cursor: string) {
  const page = await listGenerationBatches({ limit: 20, cursor });
  return {
    items: page.items.map(studioTask),
    nextCursor: page.next_cursor,
    total: page.total ?? page.items.length,
  };
}

export async function loadMoreOralTasks(offset: number) {
  const page = await listOralTasksPage({ limit: 20, offset });
  return {
    items: page.items.map(oralTask),
    loaded: offset + page.items.length,
    total: page.total,
  };
}

export async function loadStudioTaskDetail(
  kind: "generation_batch" | "oral_task",
  id: string,
): Promise<StudioTask> {
  return kind === "oral_task"
    ? oralTask(await getOralTask(id))
    : studioTaskFromBatch(await getGenerationBatch(id));
}

export async function retryStudioTask(task: StudioTask): Promise<void> {
  if (task.backendKind !== "oral_task" || !task.retryAction) {
    throw new Error("当前任务状态不支持重试。");
  }
  const taskId = task.backendId || task.id;
  if (task.retryAction === "archive-retry") {
    await retryOralTaskArchive(taskId);
    return;
  }
  await retryOralTask(taskId);
}

export async function downloadStudioTaskResult(
  task: StudioTask,
): Promise<void> {
  if (task.backendKind !== "oral_task" || !task.resultId) {
    throw new Error("当前任务没有可下载的口播成片。");
  }
  await downloadMaterialAsset(task.resultId, `${task.title}.mp4`);
}

function formatViralDuration(durationMs: number): string {
  const totalSeconds = Math.max(0, Math.round(durationMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function studioVideoFromViral(item: ViralVideoItem): StudioVideo {
  return {
    id: `${item.platform}-${item.videoId}`,
    title: item.title,
    author: item.author,
    platform: item.platform === "douyin" ? "抖音" : "视频号",
    category: item.category,
    poster: item.coverUrl ?? "",
    duration: formatViralDuration(item.durationMs),
    likes: item.likes,
    collections: item.collects,
    shares: item.shares,
    description: item.sourceDescription ?? "",
    platformKey: item.platform,
    nativeId: item.videoId,
    authorAvatar: item.authorAvatar,
    verified: item.verified,
    comments: item.comments,
    publishedAt: item.publishedAt,
    publishedDisplay: item.publishedDisplay,
    likeDisplay: item.likeDisplay,
    tags: item.tags,
    hasPlayableAudio: item.hasPlayableAudio,
    playUrl: item.playUrl,
  };
}

/** 爆款视频（C4 重启）：两个平台各自聚合；数据源未配置或失败时保持
 * 空态，不打断工作台其余数据的加载（与统计指标同一容错口径）。 */
export async function loadViralVideos(): Promise<{
  videos: StudioVideo[];
  errors: string[];
}> {
  const results = await Promise.allSettled([
    listViralVideos("douyin"),
    listViralVideos("wechat_channels"),
  ]);
  const labels = ["抖音", "视频号"];
  const videos: StudioVideo[] = [];
  const errors: string[] = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      videos.push(...result.value.items.map(studioVideoFromViral));
    } else {
      errors.push(`读取${labels[index]}爆款失败：${errorText(result.reason)}`);
    }
  });
  return { videos, errors };
}

export async function loadStudioData(
  currentUser: CurrentUser,
  options: { includeViral?: boolean } = {},
): Promise<StudioData> {
  const [
    projectsResult,
    peopleResult,
    tasksResult,
    statsResult,
    analytics7Result,
    analytics30Result,
    oralResult,
    viralResult,
    materialsResult,
  ] = await Promise.allSettled([
    loadProjects(),
    loadPeople(),
    loadTasks(currentUser),
    getStudioStats(),
    getStudioAnalytics(7),
    getStudioAnalytics(30),
    loadOralTasks(),
    options.includeViral === false
      ? Promise.resolve({ videos: [], errors: [] })
      : loadViralVideos(),
    loadVideoMaterialMetadata(),
  ]);
  const errors: string[] = [];
  const projectData =
    projectsResult.status === "fulfilled"
      ? projectsResult.value
      : { projects: [], assets: [], errors: [] };
  const peopleData =
    peopleResult.status === "fulfilled"
      ? peopleResult.value
      : { people: [], assets: [], errors: [], nextCursor: null, total: 0 };
  const tasks = [
    ...(tasksResult.status === "fulfilled" ? tasksResult.value.items : []),
    ...(oralResult.status === "fulfilled" ? oralResult.value.items : []),
  ];
  // 统计加载失败不打断工作区：指标卡回退为 "—"，重试路径会再次拉取。
  const stats = statsResult.status === "fulfilled" ? statsResult.value : null;
  // 数据看板聚合同理：失败回退 null，看板页展示"尚未就绪"空态。
  const analytics7 =
    analytics7Result.status === "fulfilled" ? analytics7Result.value : null;
  const analytics30 =
    analytics30Result.status === "fulfilled" ? analytics30Result.value : null;

  if (projectsResult.status === "rejected") {
    errors.push(`读取项目失败：${errorText(projectsResult.reason)}`);
  }
  errors.push(...projectData.errors);
  if (peopleResult.status === "rejected") {
    errors.push(`读取人物失败：${errorText(peopleResult.reason)}`);
  }
  errors.push(...peopleData.errors);
  if (tasksResult.status === "rejected") {
    errors.push(`读取任务失败：${errorText(tasksResult.reason)}`);
  }
  if (oralResult.status === "rejected") {
    errors.push(`读取口播任务失败：${errorText(oralResult.reason)}`);
  }
  if (viralResult.status === "rejected") {
    errors.push(`读取爆款视频失败：${errorText(viralResult.reason)}`);
  } else {
    errors.push(...viralResult.value.errors);
  }
  // 素材库加载失败不打断工作区：视频生成页的选择器退化为仅已加载资产。
  const materials =
    materialsResult.status === "fulfilled" ? materialsResult.value : [];

  return {
    people: peopleData.people,
    assets: [...projectData.assets, ...peopleData.assets],
    materials,
    videos: viralResult.status === "fulfilled" ? viralResult.value.videos : [],
    tasks,
    projects: projectData.projects,
    errors,
    loading: false,
    stats,
    analytics7,
    analytics30,
    pagination: {
      people: {
        nextCursor: peopleData.nextCursor,
        total: peopleData.total,
      },
      scenes: {},
      generationTasks:
        tasksResult.status === "fulfilled"
          ? {
              nextCursor: tasksResult.value.nextCursor,
              total: tasksResult.value.total,
            }
          : { nextCursor: null, total: 0 },
      oralTasks:
        oralResult.status === "fulfilled"
          ? {
              loaded: oralResult.value.loaded,
              total: oralResult.value.total,
            }
          : { loaded: 0, total: 0 },
    },
  };
}

/** 启动阶段只读取图片素材元数据；预览地址由实际可见的选择器按页签发。 */
async function loadVideoMaterialMetadata(): Promise<StudioAsset[]> {
  const page = await listMaterials({ mediaType: "image", pageSize: 60 });
  return page.items.map(studioAssetFromMaterial);
}

/** 视频生成页本机上传图片：素材三步通道，返回可直接引用的签名资产。 */
export async function uploadVideoMaterial(
  file: File,
  group: string,
  onProgress: (progress: number) => void,
  signal?: AbortSignal,
): Promise<StudioAsset> {
  const intent = await createMaterialUploadIntent(file, {
    title: file.name,
    group,
  });
  await uploadMaterial(intent, file, onProgress, signal);
  const material = await completeMaterialUpload(intent.asset_id);
  const asset = studioAssetFromMaterial(material);
  const url = material.asset_id
    ? await getAssetDownloadUrl(material.asset_id)
        .then((result) => result.url)
        .catch(() => undefined)
    : undefined;
  return { ...asset, url };
}

export async function uploadOralAudioMaterial(
  file: File,
  purpose: OralAudioPurpose,
  durationSeconds: number,
  onProgress: (progress: number) => void,
  signal?: AbortSignal,
): Promise<StudioAsset> {
  const intent = await createMaterialUploadIntent(file, {
    title: file.name,
    group: purpose === "oral_audio" ? "完整口播音频" : "声音克隆样本",
    audioPurpose: purpose,
    durationSeconds,
  });
  await uploadMaterial(intent, file, onProgress, signal);
  const material = await completeMaterialUpload(intent.asset_id);
  const asset = studioAssetFromMaterial(material);
  const url = material.asset_id
    ? await getAssetDownloadUrl(material.asset_id)
        .then((result) => result.url)
        .catch(() => undefined)
    : undefined;
  return { ...asset, url };
}

/** 静默轮询用的统计刷新：失败返回 null，由调用方保留旧值。 */
export async function reloadStats(): Promise<StudioStats | null> {
  try {
    return await getStudioStats();
  } catch {
    return null;
  }
}

/** 云端草稿恢复结果：草稿 + 我的文案列表。 */
export type CloudDraftRestore = {
  draft: StudioDraft;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** 把云端 JSON payload 还原为 StudioDraft；结构不完整时返回 null 而不是抛错，
 * 云端草稿必须永远不阻塞工作区进入。 */
function draftFromPayload(payload: unknown): StudioDraft | null {
  if (!isRecord(payload)) return null;
  const script = payload.script;
  const base = createDraft();
  if (!isRecord(script) || typeof script.text !== "string") return null;
  const merged: StudioDraft = {
    ...base,
    ...(payload as Partial<StudioDraft>),
    script: {
      ...base.script,
      ...(script as Partial<StudioScript>),
    },
    promptEdited:
      typeof payload.promptEdited === "boolean"
        ? payload.promptEdited
        : typeof payload.prompt === "string" && payload.prompt.length > 0,
    scriptEdited:
      typeof payload.scriptEdited === "boolean"
        ? payload.scriptEdited
        : [script.title, script.original, script.text].some(
            (value) => typeof value === "string" && value.length > 0,
          ),
  };
  return merged;
}

/** 读取云端工作草稿（copy 工作区）。无草稿（404）返回 undefined，
 * 其余错误抛给调用方决定提示方式。 */
export async function loadCloudDraft(): Promise<CloudDraftRestore | undefined> {
  let record: Awaited<ReturnType<typeof getStudioDraft>>;
  try {
    record = await getStudioDraft("copy");
  } catch (cause: unknown) {
    if (
      cause &&
      typeof cause === "object" &&
      "status" in cause &&
      (cause as { status?: number }).status === 404
    ) {
      return undefined;
    }
    throw cause;
  }
  const draft = draftFromPayload(record.payload);
  if (!draft) return undefined;
  draft.script.confirmed = record.script_confirmed;
  return { draft };
}

/** 云端草稿自动保存（last-write-wins）。整个 StudioDraft 序列化上送，
 * 服务端按 (user, kind) 单行 upsert。 */
export async function persistCloudDraft(draft: StudioDraft): Promise<void> {
  await saveStudioDraft(
    "copy" satisfies StudioDraftKind,
    draft as unknown as Record<string, unknown>,
    draft.script.confirmed,
  );
}

/** 我的文案列表：云端记录 → StudioScript（confirmed 不持久化，回填后需重新确认终稿）。 */
function savedScriptFromRecord(record: {
  script_id: string;
  title: string;
  text: string;
  original: string | null;
  version: number;
  ip_id?: string | null;
  source_project_id?: string | null;
  source_kind?: string | null;
}): StudioScript {
  return {
    id: record.script_id,
    title: record.title,
    original: record.original ?? "",
    text: record.text,
    version: record.version,
    confirmed: false,
    ipId: record.ip_id ?? undefined,
    sourceProjectId: record.source_project_id ?? undefined,
    // 与服务端 studio_drafts.py 的 source_kind 契约对齐：
    // Literal["viral", "project", "link", "upload"]，其余值一律丢弃。
    sourceKind:
      record.source_kind === "viral" ||
      record.source_kind === "project" ||
      record.source_kind === "link" ||
      record.source_kind === "upload"
        ? record.source_kind
        : undefined,
  };
}

export async function loadSavedScriptList(): Promise<StudioScript[]> {
  const records = await listStudioSavedScripts();
  return records.map(savedScriptFromRecord);
}

export async function persistSavedScript(
  script: StudioScript,
  sourceProjectId: string | undefined = script.sourceProjectId,
  ipId?: string,
): Promise<void> {
  const input: StudioSavedScriptInput = {
    script_id: script.id,
    title: script.title || "未命名文案",
    text: script.text,
    original: script.original || null,
    version: script.version,
    ip_id: ipId ?? script.ipId ?? null,
    source_project_id: sourceProjectId ?? null,
    source_kind: script.sourceKind ?? (sourceProjectId ? "project" : null),
  };
  await saveStudioSavedScript(input);
}

export type ProjectScriptPublishResult =
  | "published"
  | "not-applicable"
  | "failed";

/** 终稿显式发布到项目脚本版本（C7 衔接点）。没有镜头卡表示当前稿只完成了
 * 工坊确认，尚未进入复刻分镜阶段；该状态不是发布失败。 */
export async function publishScriptVersion(
  projectId: string,
  text: string,
): Promise<ProjectScriptPublishResult> {
  try {
    const shotCards = await getLatestProjectShotCards(projectId);
    if (!shotCards) return "not-applicable";
    await createScriptVersion(projectId, {
      source: "custom",
      text,
      shot_card_version_id: shotCards.id,
    });
    return "published";
  } catch {
    return "failed";
  }
}

export type PersonAssetLoad = {
  assets: StudioAsset[];
  errors: string[];
  loaded: number;
  total: number;
};

export async function loadPersonAssets(
  identityId: string,
  offset = 0,
): Promise<PersonAssetLoad> {
  try {
    const page = await listCharacterSceneLooksPage(identityId, {
      limit: sceneLimit,
      offset,
    });
    const scenes = page.items;
    const selected = scenes
      .map((scene) => ({
        scene,
        view:
          scene.views.find((view) => view.view_type === "FRONT_FACE") ??
          scene.views[0],
      }))
      .filter((item) => Boolean(item.view));
    const previews = await Promise.allSettled(
      selected.map(({ view }) =>
        signedUrl(view?.asset_id as string, getCachedCharacterAssetUrl),
      ),
    );
    const errors: string[] = [];
    const assets = selected.map(({ scene, view }, index): StudioAsset => {
      const preview = previews[index];
      let url: string | undefined;
      if (preview?.status === "fulfilled") {
        url = preview.value;
      } else if (preview?.status === "rejected") {
        errors.push(
          `读取场景图片“${scene.scene_name}”失败：${errorText(preview.reason)}`,
        );
      }
      return {
        id: view?.asset_id as string,
        name: scene.scene_name,
        kind: "image",
        url,
        group: "场景形象照",
        personId: identityId,
        composite: false,
        source: "人物库场景造型",
        saved: Boolean(scene.published_at),
      };
    });
    return {
      assets,
      errors,
      loaded: offset + scenes.length,
      total: page.total,
    };
  } catch (error) {
    throw new Error(`读取人物场景形象照失败：${errorText(error)}`);
  }
}

/** 提取文案管线（script-from-audio）：提交任务 → 每 2 秒轮询 → 终态返回。
 * 成功返回转写全文；失败抛出带服务端文案的 Error（含 SUBMISSION_UNCERTAIN）。 */
export async function extractScriptFromUpload(
  projectId: string,
  assetId: string,
): Promise<{ text: string; taskId: string }> {
  const submitted = await createScriptFromAudioTask(
    projectId,
    assetId,
    crypto.randomUUID(),
  );
  const maxAttempts = 150; // 2s × 150 = 5 分钟上限（长音频异步转写兜底）
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    const task = await getScriptFromAudioTask(submitted.id);
    if (task.status === "SUCCEEDED" && task.result) {
      return { text: task.result.text, taskId: task.id };
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new Error(task.error_message || "文案提取失败，请稍后重试。");
    }
  }
  throw new Error("文案仍在后台提取，请返回文案工坊恢复本次任务。");
}

export async function loadLatestScriptFromUpload(projectId: string) {
  const task = await getLatestScriptFromAudioTask(projectId);
  return task
    ? {
        id: task.id,
        status: task.status,
        result: task.result,
        errorMessage: task.error_message ?? undefined,
        sourceAssetId: task.source_asset_id ?? undefined,
      }
    : null;
}

/** 复刻一键生成：存稿 → 编译 →（编辑过则存修订）→ 锁定 → 建批。 */
export async function runReplicaGeneration(
  projectId: string,
  input: {
    promptText: string;
    originalScriptText: string;
    confirmedScriptText?: string;
    shotCardVersionId: string;
    firstFrameAssetId: string;
    outputDurationSeconds: number;
    resolution: "768P" | "2K";
    ratio: GenerationRatio;
    quantity: number;
    idempotencyKey: string;
    isCurrent?: () => boolean;
  },
): Promise<GenerationBatch> {
  const { idempotencyKey, isCurrent, ...stableInput } = input;
  const fingerprint = JSON.stringify(stableInput);
  const contextKey = replicaRequestContextKey(projectId, fingerprint);
  const frozen =
    frozenReplicaRequests.get(idempotencyKey) ??
    frozenReplicaRequestsByContext.get(contextKey);
  if (frozen) {
    if (frozen.projectId !== projectId || frozen.fingerprint !== fingerprint) {
      throw new Error("复刻提交参数已变化，请重新确认费用后再试。");
    }
    if (isCurrent && !isCurrent()) {
      throw new Error("复刻页面已变化，本次旧提交已停止。");
    }
    const batch = await createGenerationBatch(projectId, frozen.request);
    clearFrozenReplicaRequest(frozen);
    return batch;
  }
  const script = await createScriptVersion(projectId, {
    source: input.confirmedScriptText?.trim()
      ? "custom"
      : input.originalScriptText.trim()
        ? "original"
        : "custom",
    text: input.confirmedScriptText?.trim() || input.originalScriptText,
    shot_card_version_id: input.shotCardVersionId,
  });
  const compiled = await compileGenerationPrompt(projectId, {
    script_version_id: script.id,
    shot_card_version_id: input.shotCardVersionId,
    first_frame_asset_id: input.firstFrameAssetId,
    output_duration_seconds: input.outputDurationSeconds,
    resolution: input.resolution,
    ratio: input.ratio,
  });
  const compiledText = String(
    (compiled.payload as Record<string, unknown>).prompt_text ?? "",
  );
  const finalPrompt =
    !input.confirmedScriptText?.trim() &&
    input.promptText.trim() &&
    input.promptText !== compiledText
      ? await reviseGenerationPrompt(projectId, {
          base_prompt_version_id: compiled.id,
          prompt_text: input.promptText.trim(),
        })
      : compiled;
  const locked = await lockGenerationPrompt(projectId, finalPrompt.id);
  if (isCurrent && !isCurrent()) {
    throw new Error("复刻页面已变化，本次旧提交已停止。");
  }
  const request: GenerationBatchInput = {
    quantity: input.quantity,
    prompt_version_id: locked.id,
    first_frame_asset_id: input.firstFrameAssetId,
    output_duration_seconds: input.outputDurationSeconds,
    resolution: input.resolution,
    ratio: input.ratio,
    idempotency_key: idempotencyKey,
    provider: defaultBatchProvider(),
    fake_audio_quality: "ok",
  };
  const prepared = {
    fingerprint,
    idempotencyKey,
    projectId,
    request,
  };
  frozenReplicaRequests.set(idempotencyKey, prepared);
  frozenReplicaRequestsByContext.set(contextKey, prepared);
  const batch = await createGenerationBatch(projectId, request);
  clearFrozenReplicaRequest(prepared);
  return batch;
}
