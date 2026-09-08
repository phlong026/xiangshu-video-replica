import type {
  CurrentUser,
  IndependentCapabilities,
  Project,
  StudioAnalytics,
} from "../api";

export type StudioPage =
  | "workbench"
  | "viral"
  | "viral-detail"
  | "copy"
  | "replica"
  | "replacement"
  | "video"
  | "reference"
  | "oral"
  | "oral-audio"
  | "tasks"
  | "task-detail"
  | "people"
  | "person-ip"
  | "person-photos"
  | "person-avatars"
  | "person-voices"
  | "materials"
  | "publishing"
  | "analytics"
  | "profile";
export type AssetKind = "image" | "video" | "audio";
export type StudioAsset = {
  id: string;
  materialId?: string;
  assetId?: string;
  generationTaskId?: string;
  name: string;
  kind: AssetKind;
  url?: string;
  poster?: string;
  duration?: string;
  group: string;
  personId?: string;
  composite?: boolean;
  source: string;
  saved: boolean;
  delivery?: "stored" | "direct";
  allowedUses?: string[];
  allowedActions?: string[];
};
export type StudioAvatar = {
  id: string;
  name: string;
  imageId: string;
  ready: boolean;
  status?: "PENDING" | "RUNNING" | "READY" | "FAILED";
  error?: string;
  origin: "视频制作" | "照片制作";
  duration: string;
};
export type StudioVoice = {
  id: string;
  name: string;
  confirmed: boolean;
  isDefault: boolean;
  status?: "PENDING" | "RUNNING" | "READY" | "FAILED";
  error?: string;
  url?: string;
};
export type StudioPerson = {
  id: string;
  name: string;
  role: string;
  portrait?: string;
  version: number;
  scope: string;
  audience: string;
  expression: string;
  sheetId?: string;
  photoIds: string[];
  photoCount?: number;
  avatars: StudioAvatar[];
  voices: StudioVoice[];
};
export type StudioVideo = {
  id: string;
  title: string;
  author: string;
  platform: "抖音" | "视频号";
  category: string;
  poster: string;
  videoUrl?: string;
  duration: string;
  likes: number;
  collections: number | null;
  shares: number | null;
  description: string;
  /** C4 重启：爆款数据源规范化字段（列表/详情按平台展示，审核样例可缺省）。 */
  platformKey?: "douyin" | "wechat_channels";
  nativeId?: string;
  authorAvatar?: string | null;
  verified?: boolean;
  comments?: number | null;
  publishedAt?: number | null;
  publishedDisplay?: string | null;
  likeDisplay?: string | null;
  tags?: string[];
  hasPlayableAudio?: boolean;
  playUrl?: string | null;
};
export type StudioTask = {
  draftSnapshot?: StudioDraft;
  id: string;
  backendKind?: "generation_batch" | "oral_task";
  backendId?: string;
  backendStatus?: string;
  billingStatus?: string;
  retryAction?: "retry" | "archive-retry";
  title: string;
  type: "视频复刻" | "人物置换" | "视频生成" | "数字人口播";
  status:
    | "running"
    | "queued"
    | "failed"
    | "completed"
    | "uncertain"
    | "cancelled";
  progress?: number;
  submitted: string;
  poster?: string;
  resultId?: string;
  batchId?: string;
  /** 独立创作批次无项目归属（null）。 */
  projectId?: string | null;
  driverMode?: "text" | "audio";
  ipId?: string;
  avatarId?: string;
  voiceId?: string;
  audioId?: string;
  scriptVersion?: number;
  /** 服务端记录的实际口播正文，用于基于历史任务创建新稿。 */
  scriptText?: string;
};
export type StudioStats = {
  today_completed: number;
  running: number;
  queued: number;
  needs_attention: number;
  total_completed: number;
};
export type StudioData = {
  people: StudioPerson[];
  assets: StudioAsset[];
  /** 素材库图片（视频生成页首帧/尾帧/参考素材的素材库选择来源）。 */
  materials: StudioAsset[];
  videos: StudioVideo[];
  tasks: StudioTask[];
  projects: Project[];
  errors: string[];
  loading: boolean;
  /** 平台侧真实统计（/api/studio/stats）；加载失败或审核模式为 null。 */
  stats: StudioStats | null;
  /** 平台侧真实成片聚合（/api/studio/analytics，C6 数据看板），7/30 天双窗口；
   * 加载失败为 null，看板页回退"尚未就绪"空态。 */
  analytics7: StudioAnalytics | null;
  analytics30: StudioAnalytics | null;
  pagination?: {
    people?: { nextCursor: string | null; total: number };
    scenes?: Record<string, { loaded: number; total: number }>;
    generationTasks?: { nextCursor: string | null; total: number };
    oralTasks?: { loaded: number; total: number };
  };
};
export type StudioScript = {
  id: string;
  title: string;
  original: string;
  text: string;
  version: number;
  confirmed: boolean;
  ipId?: string;
  sourceProjectId?: string;
  sourceKind?: "viral" | "project" | "link" | "upload";
};
export type StudioDraft = {
  id: string;
  ipId?: string;
  sourceId?: string;
  /** 上传来源视频的资产 id：提取文案（script-from-audio）管线输入。 */
  sourceAssetId?: string;
  projectId?: string;
  selectedShotId: string;
  originalImageId?: string;
  imageId?: string;
  firstFrameId?: string;
  /** 人物置换流程交接的已确认首帧版本。 */
  firstFrameSelectionVersionId?: string;
  tailFrameId?: string;
  avatarId?: string;
  voiceId?: string;
  audioId?: string;
  script: StudioScript;
  /** 当前脚本是否包含尚未发布为项目版本的本地编辑，包括主动清空。 */
  scriptEdited?: boolean;
  prompt: string;
  /** 当前 Prompt 是否包含尚未保存为项目版本的本地编辑，包括主动清空。 */
  promptEdited?: boolean;
  referenceIds: string[];
  resolution: string;
  ratio: string;
  duration: number;
  count: number;
  frameConfirmed: boolean;
  style: "standard" | "template";
  subtitles: boolean;
  quoteRevision: number;
  /** 最近一次独立创作提交的批次 id：预览区就地展示生成进度。 */
  videoBatchId?: string;
};
export type PickerKind =
  | "person"
  | "image"
  | "original-frame"
  | "first-frame"
  | "tail-frame"
  | "reference"
  | "avatar"
  | "voice"
  | "audio"
  | "voice-audio"
  | "avatar-photo";
export type StudioPublishDraft = {
  id: string;
  assetId: string;
  coverId?: string;
  platform: "抖音" | "视频号";
  account: string;
  title: string;
  description: string;
  tags: string[];
};
export type StudioState = {
  page: StudioPage;
  draft: StudioDraft;
  selectedVideoId?: string;
  selectedTaskId?: string;
  selectedTaskKind?: "generation_batch" | "oral_task";
  selectedTaskBackendId?: string;
  selectedAssetId?: string;
  returnTo?: StudioPage;
  selectedPersonId?: string;
  savedScripts: StudioScript[];
  favorites: string[];
  publishDrafts?: StudioPublishDraft[];
};
export type LivePanel =
  | "projects"
  | "characters"
  | "tasks"
  | "profile"
  | "wallet"
  | "analysis";
export type StudioContextValue = {
  state: StudioState;
  data: StudioData;
  review: boolean;
  user: CurrentUser;
  videoCapabilities?: IndependentCapabilities;
  videoCapabilitiesStatus?: "loading" | "ready" | "error";
  retryVideoCapabilities?: () => void;
  referenceAssetsPending?: boolean;
  referenceAssetsError?: boolean;
  retryReferenceAssets?: () => void;
  draftSaveStatus?: "idle" | "dirty" | "saving" | "saved" | "error";
  navigate: (page: StudioPage, patch?: Partial<StudioState>) => void;
  patchDraft: (patch: Partial<StudioDraft>) => void;
  patchState: (patch: Partial<StudioState>) => void;
  updateData: (update: (data: StudioData) => StudioData) => void;
  notify: (message: string) => void;
  openPicker: (kind: PickerKind) => void;
  openLive: (panel: LivePanel) => void;
  requestGeneration: (kind: StudioTask["type"]) => void;
  saveDraft: () => void;
  /** 确认终稿：云端保存成功后置 confirmed；项目已有分镜时再同步项目脚本。 */
  confirmFinalDraft: () => void;
  /** 上传来源视频 → 提取文案（script-from-audio）→ 回填草稿并跳文案工坊。 */
  extractScriptFromUpload: (projectId?: string, assetId?: string) => void;
  refresh: () => void;
};
