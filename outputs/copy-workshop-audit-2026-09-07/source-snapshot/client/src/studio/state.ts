import type { ShotCard } from "./../api";
import type {
  StudioAsset,
  StudioDraft,
  StudioPage,
  StudioState,
  StudioTask,
} from "./types";

export const pageTitles: Record<StudioPage, string> = {
  workbench: "工作台",
  viral: "爆款视频",
  "viral-detail": "爆款视频详情",
  copy: "文案工坊",
  replica: "视频复刻",
  replacement: "人物置换",
  video: "视频生成 · 文/图生视频",
  reference: "视频生成 · 参考生视频",
  oral: "数字人口播",
  "oral-audio": "数字人口播 · 用已有音频生成",
  tasks: "任务中心",
  "task-detail": "任务详情与结果",
  people: "人物库",
  "person-ip": "人物详情",
  "person-photos": "人物详情",
  "person-avatars": "人物详情",
  "person-voices": "人物详情",
  materials: "素材库",
  publishing: "发布管理",
  analytics: "数据看板",
  profile: "用户档案",
};

export function routeFromHash(hash: string): StudioPage {
  const raw = hash.replace(/^#(?:studio\/)?/, "");
  const aliases: Record<string, StudioPage> = {
    projects: "replica",
    characters: "people",
    wallet: "profile",
  };
  if (Object.hasOwn(aliases, raw)) return aliases[raw];
  return Object.hasOwn(pageTitles, raw) ? (raw as StudioPage) : "workbench";
}

export function createDraft(): StudioDraft {
  return {
    id: `draft-${crypto.randomUUID()}`,
    selectedShotId: "shot-2",
    script: {
      id: `script-${crypto.randomUUID()}`,
      title: "",
      original: "",
      text: "",
      version: 1,
      confirmed: false,
    },
    scriptEdited: false,
    prompt: "",
    promptEdited: false,
    referenceIds: [],
    resolution: "768P",
    ratio: "16:9",
    duration: 8,
    count: 1,
    frameConfirmed: false,
    style: "standard",
    subtitles: false,
    quoteRevision: 0,
  };
}

export function createState(page: StudioPage = "workbench"): StudioState {
  return { page, draft: createDraft(), savedScripts: [], favorites: [] };
}

export function withImportedProject(
  state: StudioState,
  imported: StudioDraft,
): StudioState {
  if (imported.projectId && imported.projectId === state.draft.projectId) {
    const current = state.draft.script;
    const newer =
      imported.script.text &&
      (imported.script.id !== current.id ||
        imported.script.version > current.version);
    return {
      ...state,
      draft: {
        ...state.draft,
        sourceId: imported.sourceId,
        script: newer ? imported.script : current,
      },
    };
  }
  return {
    ...state,
    draft: imported,
    selectedVideoId: undefined,
    selectedTaskId: undefined,
    selectedAssetId: undefined,
    selectedPersonId: undefined,
    returnTo: undefined,
  };
}

export function draftFromTask(task: StudioTask): StudioDraft {
  const fresh = createDraft();
  const snapshot = task.draftSnapshot;
  return {
    ...fresh,
    ...snapshot,
    id: fresh.id,
    projectId: task.projectId ?? snapshot?.projectId,
    ipId: task.ipId ?? snapshot?.ipId,
    avatarId: task.avatarId ?? snapshot?.avatarId,
    voiceId:
      task.driverMode === "audio"
        ? undefined
        : (task.voiceId ?? snapshot?.voiceId),
    audioId: task.audioId ?? snapshot?.audioId,
    script: { ...(snapshot?.script ?? fresh.script), confirmed: false },
    quoteRevision: 0,
  };
}

export function patchStudioDraft(
  draft: StudioDraft,
  patch: Partial<StudioDraft>,
): StudioDraft {
  const projectChanged =
    Object.hasOwn(patch, "projectId") && patch.projectId !== draft.projectId;
  const identityChanged =
    projectChanged ||
    (Object.hasOwn(patch, "ipId") && patch.ipId !== draft.ipId) ||
    (Object.hasOwn(patch, "imageId") && patch.imageId !== draft.imageId);
  const firstFrameChanged =
    Object.hasOwn(patch, "firstFrameId") &&
    patch.firstFrameId !== draft.firstFrameId;
  const next = {
    ...draft,
    ...patch,
    id: draft.id,
    quoteRevision: draft.quoteRevision + 1,
  };
  if (projectChanged) {
    const blank = createDraft();
    if (!Object.hasOwn(patch, "prompt")) next.prompt = "";
    if (!Object.hasOwn(patch, "promptEdited")) next.promptEdited = false;
    if (!patch.script) next.script = blank.script;
    if (!Object.hasOwn(patch, "scriptEdited")) next.scriptEdited = false;
  }
  // Asset ownership must be reselected when identity changes, never relabelled.
  if (Object.hasOwn(patch, "ipId") && patch.ipId !== draft.ipId) {
    next.voiceId = undefined;
    next.avatarId = undefined;
    next.script = { ...next.script, confirmed: false };
  }
  if (identityChanged || firstFrameChanged) {
    if (!Object.hasOwn(patch, "firstFrameId")) next.firstFrameId = undefined;
    if (!Object.hasOwn(patch, "firstFrameSelectionVersionId"))
      next.firstFrameSelectionVersionId = undefined;
    if (!Object.hasOwn(patch, "frameConfirmed")) next.frameConfirmed = false;
  }
  if (patch.script && patch.script.text !== draft.script.text)
    next.script = { ...patch.script, confirmed: false };
  if (
    patch.script &&
    !projectChanged &&
    patch.scriptEdited === undefined &&
    (patch.script.title !== draft.script.title ||
      patch.script.original !== draft.script.original ||
      patch.script.text !== draft.script.text)
  )
    next.scriptEdited = true;
  if (
    Object.hasOwn(patch, "prompt") &&
    !projectChanged &&
    patch.promptEdited === undefined &&
    patch.prompt !== draft.prompt
  )
    next.promptEdited = true;
  return next;
}

export function buildOralInput(draft: StudioDraft, mode: "text" | "audio") {
  if (mode === "text" && (!draft.script.confirmed || !draft.script.text.trim()))
    throw new Error("请先在文案工坊确认终稿");
  if (!draft.ipId || !draft.avatarId)
    throw new Error("请选择人物与可用口播分身");
  if (mode === "audio") {
    if (!draft.audioId) throw new Error("请选择完整口播音频");
    return {
      draftId: draft.id,
      mode,
      ipId: draft.ipId,
      avatarId: draft.avatarId,
      audioAssetId: draft.audioId,
    };
  }
  if (!draft.voiceId) throw new Error("请选择已确认声音");
  return {
    draftId: draft.id,
    mode,
    ipId: draft.ipId,
    avatarId: draft.avatarId,
    voiceId: draft.voiceId,
    scriptId: draft.script.id,
    scriptVersion: draft.script.version,
    style: draft.style,
    subtitles: draft.style === "standard" && draft.subtitles,
  };
}

// ---- C2 独立创作（视频生成页）----

export const SUPPORTED_VIDEO_RATIOS = [
  "adaptive",
  "21:9",
  "16:9",
  "4:3",
  "1:1",
  "3:4",
  "9:16",
] as const;

export const DEFAULT_MAX_REFERENCE_IMAGES = 4;

export function validateReferenceImages(
  referenceIds: string[],
  availableAssets: StudioAsset[],
  maxReferenceImages = DEFAULT_MAX_REFERENCE_IMAGES,
) {
  const assetById = new Map(availableAssets.map((asset) => [asset.id, asset]));
  const seen = new Set<string>();
  const images: StudioAsset[] = [];
  let invalidCount = 0;
  let duplicateCount = 0;

  for (const id of referenceIds) {
    if (seen.has(id)) {
      duplicateCount += 1;
      continue;
    }
    seen.add(id);
    const asset = assetById.get(id);
    if (asset?.kind !== "image") {
      invalidCount += 1;
      continue;
    }
    images.push(asset);
  }

  const limit = Math.max(0, Math.floor(maxReferenceImages));
  const overLimitCount = Math.max(0, images.length - limit);
  const issues: string[] = [];
  if (invalidCount > 0)
    issues.push(`参考图仅支持图片，旧草稿中有 ${invalidCount} 项无效素材。`);
  if (duplicateCount > 0)
    issues.push(
      `参考图不能重复选择，旧草稿中有 ${duplicateCount} 项重复素材。`,
    );
  if (overLimitCount > 0)
    issues.push(
      `当前最多选择 ${limit} 张参考图，旧草稿已超出 ${overLimitCount} 张。`,
    );

  return {
    images,
    imageIds: images.map((asset) => asset.id),
    repairIds: images.slice(0, limit).map((asset) => asset.id),
    limit,
    invalidCount,
    duplicateCount,
    overLimitCount,
    issues,
  };
}

/** 文图生页签有首帧即 I2V（可选尾帧），无首帧为 T2V；参考生页签为 R2V。 */
export function resolveVideoMode(
  page: StudioPage,
  hasFirstFrame = false,
): "t2v" | "i2v" | "r2v" {
  if (page === "reference") return "r2v";
  return hasFirstFrame ? "i2v" : "t2v";
}

/** 把分镜卡拼成可读的反推提示词文本（可编辑、可另存为自定义提示词）。 */
export function buildReplicaPromptText(
  shots: ShotCard[],
  originalScript: string,
): string {
  const lines: string[] = [];
  shots.forEach((shot, index) => {
    lines.push(
      `【镜头 ${index + 1}】${shot.start_time.toFixed(1)}s–${shot.end_time.toFixed(1)}s`,
    );
    if (shot.shot_type) {
      lines.push(
        `景别/构图：${shot.shot_type}${shot.composition ? ` · ${shot.composition}` : ""}`,
      );
    }
    if (shot.camera_motion) lines.push(`运镜：${shot.camera_motion}`);
    if (shot.subject) lines.push(`主体：${shot.subject}`);
    if (shot.action) lines.push(`动作：${shot.action}`);
    if (shot.scene) lines.push(`场景：${shot.scene}`);
    if (shot.spoken_text) lines.push(`台词：${shot.spoken_text}`);
    if (shot.transition) lines.push(`转场：${shot.transition}`);
    lines.push("");
  });
  if (originalScript.trim()) {
    lines.push("【原片口播稿】", originalScript.trim());
  }
  return lines.join("\n").trim();
}
