import type { components } from "./generated/api";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;
// Cloud/storage operations (diagnostics, presigned URLs, archive prechecks)
// may legitimately take much longer than a normal API round-trip.
const CLOUD_OP_TIMEOUT_MS = 60_000;
// The server-side video provider permits a 90s response window.
const ANALYSIS_TIMEOUT_MS = 120_000;
export const SESSION_EXPIRED_EVENT = "video-replica:session-expired";

type HealthResponse = components["schemas"]["HealthResponse"];
export type UserRole = "employee" | "admin" | "auditor";

export type CurrentUser = {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
};

export type GenerationTask = {
  id: string;
  status: string;
  archive_status: string;
  quality_status: string;
  quality_issue_codes: string[];
  result_asset_id: string | null;
  prompt_snapshot: Record<string, unknown> | null;
};

export type BatchProgress = {
  total_count: number;
  terminal_count: number;
  progress_percent: number;
  counts: Record<string, number>;
};

export type GenerationBatch = {
  id: string;
  status: string;
  quantity: number;
  progress: BatchProgress;
  tasks: GenerationTask[];
};

export type ProviderName = "metaso" | "apilio" | "cos" | "oss";

export type ProviderSettings = {
  provider: ProviderName;
  configured: boolean;
  config: Record<string, string>;
};

export type RuntimeSettings = {
  max_generation_count_per_batch: number;
  max_concurrent_h3_tasks: number;
  active_storage_provider: "cos" | "oss";
};

export type SettingsSnapshot = {
  providers: Record<ProviderName, ProviderSettings>;
  runtime: RuntimeSettings;
};

export type DiagnosticProviderResult = {
  provider: ProviderName;
  status: "ok" | "not_configured" | "configured_only" | "error";
  configured_fields: string[];
  adapter_capability: "configuration_only" | "connection_test";
  test_kind: string;
  http_status?: number | null;
  error_code?: string | null;
  failure_phase?: string | null;
  cleanup_failed?: boolean | null;
  latency_ms?: number | null;
  message: string;
};

export type SettingsDiagnosticReport = {
  id: string;
  status: "ok" | "attention";
  providers: DiagnosticProviderResult[];
  download_url: string;
};

export type Project = {
  id: string;
  owner_user_id: string;
  name: string;
  status: string;
  reference_asset_id: string | null;
  reference_upload_status: "NOT_STARTED" | "UPLOAD_PENDING" | "READY";
  analysis_status: "NOT_READY" | "PENDING" | "READY";
};

export type UploadIntent = {
  asset_id: string;
  project_id: string;
  storage_key: string;
  method: "PUT";
  url: string;
  headers: Record<string, string>;
  expires_at: string;
};

export type CompletedUpload = {
  asset_id: string;
  project_id: string;
  status: string;
  storage_uri: string;
  sha256: string;
  size_bytes: number;
  content_type: string;
  metadata: { duration_seconds: number };
};

export type AnalysisVersion = {
  id: string;
  project_id: string;
  asset_id: string | null;
  kind: string;
  version_number: number;
  payload: Record<string, unknown>;
  created_by_user_id: string | null;
  created_at: string;
};

export type AnalysisProvider = "apilio_gemini" | "fake_gemini";

export type ShotCard = {
  shot_id: string;
  start_time: number;
  end_time: number;
  shot_type: string;
  composition: string;
  camera_motion: string;
  subject: string;
  action: string;
  scene: string;
  spoken_text: string;
  transition: string;
};

export type AnalysisPayload = {
  summary: string;
  duration_seconds: number;
  shots: ShotCard[];
};

export type ShotCardPayload = {
  source_analysis_version_id: string;
  duration_seconds: number;
  shots: ShotCard[];
};

export type Character = {
  id: string;
  name: string;
  reference_asset_ids: string[];
  authorization_project_ids: string[];
  authorization_expires_at: string | null;
  is_active: boolean;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectMainCharacter = Omit<
  components["schemas"]["ProjectMainCharacterResponse"],
  "character_snapshot"
> & {
  character_snapshot: ProjectCharacterSnapshot;
};

export type ProjectCharacterAssetOption =
  components["schemas"]["ProjectCharacterAssetOption"];

export type ProjectCharacterVersionOption = Omit<
  components["schemas"]["ProjectCharacterVersionOption"],
  "persona_snapshot_json"
> & {
  persona_snapshot_json: Record<string, unknown>;
};

export type ProjectCharacterSnapshot = {
  name?: string;
  schema_version?: string;
  character_version_id?: string;
  character_version_number?: number;
  identity?: {
    id?: string;
    display_name?: string;
    authorization_expires_at?: string | null;
  };
  persona_snapshot_json?: Record<string, unknown>;
  provider?: string | null;
  model?: string | null;
  template_version?: string | null;
  template_hash?: string | null;
  published_at?: string;
  publication_hash?: string;
  published_assets?: ProjectCharacterAssetOption[];
};

export type SourceFrameCandidate = {
  asset_id: string;
  timestamp_seconds: number;
  score: number | null;
};

export type SourceFrameCandidates = {
  requested_timestamps_seconds: number[];
  candidates: SourceFrameCandidate[];
};

export type SourceFrameSelectionState = {
  version: AnalysisVersion | null;
  stale: boolean;
};

export type SourceFrameCharacterFeatures =
  components["schemas"]["SourceFrameCharacterFeatures"];

export type CharacterReferenceRecommendation = Omit<
  components["schemas"]["CharacterReferenceRecommendation"],
  "character_version_snapshot_json" | "recommendation_reason_json"
> & {
  character_version_snapshot_json: Record<string, unknown>;
  recommendation_reason_json: Record<string, unknown>;
};

export type CharacterReferenceSelection = Omit<
  components["schemas"]["CharacterReferenceSelection"],
  "character_version_snapshot_json" | "recommendation_reason_json"
> & {
  character_version_snapshot_json: Record<string, unknown>;
  recommendation_reason_json: Record<string, unknown>;
};

export type SelectCharacterReferencesInput =
  components["schemas"]["SelectCharacterReferencesRequest"];

export type FirstFrameModel = "gpt-image-2" | "nano-banana-pro-2k";

export type FirstFrameCandidate = {
  asset_id: string;
  storage_key: string;
  storage_uri: string;
  sha256: string;
  size_bytes: number;
  content_type: string;
};

export type FirstFrameCandidates = {
  provider: string;
  model: FirstFrameModel;
  prompt: string;
  candidates: FirstFrameCandidate[];
};

export type FirstFrameSelectionState = {
  version: AnalysisVersion | null;
  stale: boolean;
};

export type FirstFrameSelectionPayload = {
  first_frame_candidates_version_id: string;
  first_frame_asset_id: string;
};

export type DownloadUrl = { url: string };

export type PersonIdentity = components["schemas"]["PersonIdentity"];
export type CharacterPersona = Omit<
  components["schemas"]["CharacterPersona"],
  "appearance_constraints_json"
> & { appearance_constraints_json: Record<string, unknown> };
export type CharacterVersion = Omit<
  components["schemas"]["CharacterVersion"],
  | "generation_params_json"
  | "persona_snapshot_json"
  | "publication_snapshot_json"
> & {
  generation_params_json: Record<string, unknown>;
  persona_snapshot_json: Record<string, unknown>;
  publication_snapshot_json: Record<string, unknown> | null;
};
export type CharacterAsset = Omit<
  components["schemas"]["CharacterAsset"],
  "auto_quality_json"
> & { auto_quality_json: Record<string, unknown> };
export type CharacterAssetReview =
  components["schemas"]["CharacterAssetReview"];
export type CharacterGenerationTask = Omit<
  components["schemas"]["CharacterGenerationTask"],
  "request_snapshot_json"
> & { request_snapshot_json: Record<string, unknown> };
export type IdentityUploadPurpose = "authorization" | "source";
export type IdentityUploadIntent =
  components["schemas"]["CreatedIdentityUploadIntent"];
export type CompletedIdentitySource =
  components["schemas"]["CompletedSourceImage"];
export type RequiredCharacterViewType =
  components["schemas"]["CharacterGenerationTask"]["view_type"];
export type CharacterReviewDecision = "APPROVED" | "REJECTED";

export type CharacterPersonaInput = {
  name: string;
  occupation?: string | null;
  scene_description?: string | null;
  appearance_constraints_json?: Record<string, unknown>;
  costume_description?: string | null;
  default_background?: string | null;
  positive_prompt?: string | null;
  negative_prompt?: string | null;
  usage_scope_json?: string[];
};

export type CharacterVersionInput = {
  provider: string;
  model: string;
  generation_params_json: Record<string, unknown>;
};

export async function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health", "本地服务暂不可用");
}

export async function getCurrentUser(): Promise<CurrentUser> {
  const user = await requestApiJson<unknown>("/api/auth/me", "身份验证失败");
  if (!isCurrentUser(user)) {
    throw new Error("身份验证失败：本地服务返回的用户信息无效");
  }
  return user;
}

export async function getGenerationBatch(
  batchId: string,
): Promise<GenerationBatch> {
  return requestApiJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    "任务批次暂不可用",
  );
}

export async function reconcileUncertainTask(
  taskId: string,
): Promise<GenerationTask> {
  return requestApiJson<GenerationTask>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/reconcile`,
    "任务对账失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function listProjects(): Promise<Project[]> {
  return requestApiJson<Project[]>("/api/projects", "项目列表暂不可用");
}

export async function createProject(name: string): Promise<Project> {
  return requestApiJson<Project>("/api/projects", "创建项目失败", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export async function deleteProject(projectId: string): Promise<void> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "删除项目失败"));
  }
}

export async function createVideoUploadIntent(
  projectId: string,
  file: File,
): Promise<UploadIntent> {
  return requestApiJson<UploadIntent>(
    "/api/assets/upload-intent",
    "创建上传任务失败",
    {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        filename: file.name,
        // Derive from the extension so a generic/empty file.type (e.g.
        // application/octet-stream from some file managers) is normalized.
        content_type: contentTypeForFile(file),
        size_bytes: file.size,
      }),
    },
  );
}

export function uploadReferenceVideo(
  intent: UploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return uploadStorageObject(intent, file, onProgress, "上传参考视频", signal);
}

export function uploadIdentityAsset(
  intent: IdentityUploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return uploadStorageObject(intent, file, onProgress, "上传人物资料", signal);
}

function uploadStorageObject(
  intent: {
    headers: Record<string, string>;
    method: string;
    url: string;
  },
  file: File,
  onProgress: (progressPercent: number) => void,
  errorPrefix: string,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(intent.method, intent.url);
    // Scale the timeout with the payload (~200KB/s) so large 50MB uploads are
    // not cut off on slow links, while small files keep a tight bound.
    request.timeout = Math.max(60_000, Math.ceil(file.size / 200));
    const devUserId = getDevelopmentUserId();
    if (devUserId && isLocalApiUploadUrl(intent.url)) {
      request.setRequestHeader("X-Dev-User-Id", devUserId);
    }
    for (const [name, value] of Object.entries(intent.headers)) {
      request.setRequestHeader(name, value);
    }
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress(100);
        resolve();
        return;
      }
      if (request.status === 401 && isLocalApiUploadUrl(intent.url)) {
        emitSessionExpired();
        reject(new Error("登录已失效，请重新进入工作台。"));
        return;
      }
      reject(new Error(`${errorPrefix}失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error(`${errorPrefix}失败（网络错误）`));
    request.ontimeout = () =>
      reject(new Error(`${errorPrefix}失败（请求超时）`));
    request.onabort = () => reject(new Error("上传已取消"));
    if (signal) {
      const onAbort = () => request.abort();
      if (signal.aborted) {
        request.abort();
      } else {
        signal.addEventListener("abort", onAbort, { once: true });
      }
    }
    request.send(file);
  });
}

export async function listPersonIdentities(): Promise<PersonIdentity[]> {
  return requestApiJson<PersonIdentity[]>(
    "/api/person-identities",
    "读取人物身份失败",
  );
}

export async function createPersonIdentity(input: {
  display_name: string;
  authorization_scope: string[];
  authorization_expires_at: string | null;
}): Promise<PersonIdentity> {
  return requestApiJson<PersonIdentity>(
    "/api/person-identities",
    "创建人物身份失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function createIdentityUploadIntent(
  identityId: string,
  purpose: IdentityUploadPurpose,
  file: File,
): Promise<IdentityUploadIntent> {
  return requestApiJson<IdentityUploadIntent>(
    `/api/person-identities/${encodeURIComponent(identityId)}/${purpose}-upload-intent`,
    purpose === "authorization" ? "创建授权上传失败" : "创建源图上传失败",
    {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        content_type: contentTypeForIdentityFile(file),
        size_bytes: file.size,
      }),
    },
  );
}

export async function completeIdentityAuthorizationUpload(
  identityId: string,
  assetId: string,
): Promise<PersonIdentity> {
  return requestApiJson<PersonIdentity>(
    `/api/person-identities/${encodeURIComponent(identityId)}/authorization-upload-complete`,
    "确认授权文件失败",
    { method: "POST", body: JSON.stringify({ asset_id: assetId }) },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function completeIdentitySourceUpload(
  identityId: string,
  assetId: string,
): Promise<CompletedIdentitySource> {
  return requestApiJson<CompletedIdentitySource>(
    `/api/person-identities/${encodeURIComponent(identityId)}/source-upload-complete`,
    "检查真人源图失败",
    { method: "POST", body: JSON.stringify({ asset_id: assetId }) },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function listCharacterPersonas(
  identityId: string,
): Promise<CharacterPersona[]> {
  return requestApiJson<CharacterPersona[]>(
    `/api/person-identities/${encodeURIComponent(identityId)}/personas`,
    "读取人物人设失败",
  );
}

export async function createCharacterPersona(
  identityId: string,
  input: CharacterPersonaInput,
): Promise<CharacterPersona> {
  return requestApiJson<CharacterPersona>(
    `/api/person-identities/${encodeURIComponent(identityId)}/personas`,
    "创建人物人设失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function updateCharacterPersona(
  personaId: string,
  input: CharacterPersonaInput,
): Promise<CharacterPersona> {
  return requestApiJson<CharacterPersona>(
    `/api/character-personas/${encodeURIComponent(personaId)}`,
    "更新人物人设失败",
    { method: "PATCH", body: JSON.stringify(input) },
  );
}

export async function listCharacterVersions(
  personaId: string,
): Promise<CharacterVersion[]> {
  return requestApiJson<CharacterVersion[]>(
    `/api/character-personas/${encodeURIComponent(personaId)}/versions`,
    "读取角色版本失败",
  );
}

export async function createCharacterVersion(
  personaId: string,
  input: CharacterVersionInput,
): Promise<CharacterVersion> {
  return requestApiJson<CharacterVersion>(
    `/api/character-personas/${encodeURIComponent(personaId)}/versions`,
    "创建角色版本失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function listCharacterGenerationTasks(
  versionId: string,
): Promise<CharacterGenerationTask[]> {
  return requestApiJson<CharacterGenerationTask[]>(
    `/api/character-versions/${encodeURIComponent(versionId)}/generation-tasks`,
    "读取人物生成任务失败",
  );
}

export async function listCharacterAssets(
  versionId: string,
): Promise<CharacterAsset[]> {
  return requestApiJson<CharacterAsset[]>(
    `/api/character-versions/${encodeURIComponent(versionId)}/assets`,
    "读取人物视角资产失败",
  );
}

export async function generateCharacterAssets(
  versionId: string,
  input: {
    idempotency_key: string;
    candidates_per_view: number;
    view_types?: RequiredCharacterViewType[];
  },
): Promise<CharacterGenerationTask[]> {
  return requestApiJson<CharacterGenerationTask[]>(
    `/api/character-versions/${encodeURIComponent(versionId)}/generate-assets`,
    "启动人物视角生成失败",
    { method: "POST", body: JSON.stringify(input) },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function regenerateCharacterAsset(
  characterAssetId: string,
  idempotencyKey: string,
): Promise<CharacterGenerationTask[]> {
  return requestApiJson<CharacterGenerationTask[]>(
    `/api/character-assets/${encodeURIComponent(characterAssetId)}/regenerate`,
    "重新生成人物视角失败",
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function reviewCharacterAsset(
  characterAssetId: string,
  decision: CharacterReviewDecision,
  comment: string,
): Promise<CharacterAssetReview> {
  return requestApiJson<CharacterAssetReview>(
    `/api/character-assets/${encodeURIComponent(characterAssetId)}/review`,
    decision === "APPROVED" ? "批准人物资产失败" : "驳回人物资产失败",
    {
      method: "POST",
      body: JSON.stringify({
        decision,
        issue_codes: decision === "REJECTED" ? ["MANUAL_REJECT"] : [],
        comment: comment.trim() || null,
      }),
    },
  );
}

export async function publishCharacterVersion(
  versionId: string,
  selectedAssetIds: Record<RequiredCharacterViewType, string>,
): Promise<CharacterVersion> {
  return requestApiJson<CharacterVersion>(
    `/api/character-versions/${encodeURIComponent(versionId)}/publish`,
    "发布角色版本失败",
    {
      method: "POST",
      body: JSON.stringify({ selected_asset_ids: selectedAssetIds }),
    },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function completeVideoUpload(
  assetId: string,
): Promise<CompletedUpload> {
  return requestApiJson<CompletedUpload>(
    `/api/assets/${encodeURIComponent(assetId)}/complete`,
    "参考视频预检失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function startVideoAnalysis(
  projectId: string,
  assetId: string,
  durationSeconds?: number,
): Promise<AnalysisVersion> {
  const payload =
    durationSeconds === undefined
      ? { asset_id: assetId }
      : {
          asset_id: assetId,
          duration_seconds: durationSeconds,
          reuse_existing: true,
        };
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/analysis`,
    "启动视频拆解失败",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
    ANALYSIS_TIMEOUT_MS,
  );
}

export async function getLatestProjectAnalysis(
  projectId: string,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/analysis/latest`,
    "读取视频拆解失败",
  );
}

export async function getLatestProjectShotCards(
  projectId: string,
): Promise<AnalysisVersion | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/shot-cards/latest`,
    {},
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取已保存镜头卡片失败（${response.status}）`);
  }
  return (await response.json()) as AnalysisVersion;
}

export async function saveShotCards(
  analysisId: string,
  shots: ShotCard[],
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/analysis/${encodeURIComponent(analysisId)}/shots`,
    "保存镜头卡片失败",
    { method: "PUT", body: JSON.stringify({ shots }) },
  );
}

export async function listProjectCharacters(
  projectId: string,
): Promise<Character[]> {
  return requestApiJson<Character[]>(
    `/api/characters?project_id=${encodeURIComponent(projectId)}`,
    "读取可用人物失败",
  );
}

export async function listProjectCharacterVersions(
  projectId: string,
): Promise<ProjectCharacterVersionOption[]> {
  return requestApiJson<ProjectCharacterVersionOption[]>(
    `/api/projects/${encodeURIComponent(projectId)}/character-versions/available`,
    "读取可用角色版本失败",
  );
}

export async function getProjectMainCharacter(
  projectId: string,
): Promise<ProjectMainCharacter | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/main-character`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取已选人物失败（${response.status}）`);
  }
  return (await response.json()) as ProjectMainCharacter;
}

export async function chooseProjectMainCharacter(
  projectId: string,
  characterId: string,
): Promise<ProjectMainCharacter> {
  return requestApiJson<ProjectMainCharacter>(
    `/api/projects/${encodeURIComponent(projectId)}/main-character`,
    "选择人物失败",
    { method: "PUT", body: JSON.stringify({ character_id: characterId }) },
  );
}

export async function chooseProjectMainCharacterVersion(
  projectId: string,
  characterVersionId: string,
): Promise<ProjectMainCharacter> {
  return requestApiJson<ProjectMainCharacter>(
    `/api/projects/${encodeURIComponent(projectId)}/main-character`,
    "选择角色版本失败",
    {
      method: "PUT",
      body: JSON.stringify({ character_version_id: characterVersionId }),
    },
  );
}

export async function getLatestProjectSourceFrames(
  projectId: string,
): Promise<AnalysisVersion | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取候选源画面失败（${response.status}）`);
  }
  return (await response.json()) as AnalysisVersion;
}

export async function getLatestProjectSourceFrameSelection(
  projectId: string,
): Promise<SourceFrameSelectionState> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/selection/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return { version: null, stale: false };
  }
  if (response.status === 409) {
    return { version: null, stale: true };
  }
  if (!response.ok) {
    throw new Error(`读取已确认源画面失败（${response.status}）`);
  }
  return { version: (await response.json()) as AnalysisVersion, stale: false };
}

export async function extractSourceFrames(
  projectId: string,
  assetId: string,
  timestampsSeconds: number[],
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/extract`,
    "提取候选源画面失败",
    {
      method: "POST",
      body: JSON.stringify({
        asset_id: assetId,
        timestamps_seconds: timestampsSeconds,
      }),
    },
  );
}

export async function confirmSourceFrame(
  projectId: string,
  sourceFrameAssetId: string,
  characterFeatures: SourceFrameCharacterFeatures,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/confirm`,
    "确认源画面失败",
    {
      method: "POST",
      body: JSON.stringify({
        source_frame_asset_id: sourceFrameAssetId,
        character_features: characterFeatures,
      }),
    },
  );
}

export async function getCharacterReferenceRecommendation(
  projectId: string,
): Promise<CharacterReferenceRecommendation> {
  return requestApiJson<CharacterReferenceRecommendation>(
    `/api/projects/${encodeURIComponent(projectId)}/character-reference-recommendation`,
    "读取人物参考图推荐失败",
  );
}

export async function getLatestCharacterReferenceSelection(
  projectId: string,
): Promise<CharacterReferenceSelection | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/character-reference-selection/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取已确认人物参考图失败（${response.status}）`);
  }
  return (await response.json()) as CharacterReferenceSelection;
}

export async function selectCharacterReferences(
  projectId: string,
  input: SelectCharacterReferencesInput,
): Promise<CharacterReferenceSelection> {
  return requestApiJson<CharacterReferenceSelection>(
    `/api/projects/${encodeURIComponent(projectId)}/character-reference-selection`,
    "确认人物参考图失败",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export async function getLatestProjectFirstFrames(
  projectId: string,
): Promise<FirstFrameSelectionState> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return { version: null, stale: false };
  }
  if (response.status === 409) {
    return { version: null, stale: true };
  }
  if (!response.ok) {
    throw new Error(`读取候选首帧失败（${response.status}）`);
  }
  const version = (await response.json()) as unknown;
  return {
    version: isAnalysisVersion(version) ? version : null,
    stale: false,
  };
}

export async function getProjectFirstFrameHistory(
  projectId: string,
): Promise<AnalysisVersion[]> {
  const versions = await requestApiJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/history`,
    "读取首帧历史失败",
  );
  return Array.isArray(versions) && versions.every(isAnalysisVersion)
    ? versions
    : [];
}

export async function getLatestProjectFirstFrameSelection(
  projectId: string,
): Promise<FirstFrameSelectionState> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/selection/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return { version: null, stale: false };
  }
  if (response.status === 409) {
    return { version: null, stale: true };
  }
  if (!response.ok) {
    throw new Error(`读取已确认首帧失败（${response.status}）`);
  }
  return { version: (await response.json()) as AnalysisVersion, stale: false };
}

export async function generateFirstFrames(
  projectId: string,
  input: {
    model: FirstFrameModel;
    prompt: string;
    quantity: number;
    character_version_id: string;
    character_reference_selection_id: string;
  },
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/generate`,
    "生成人物置换首帧失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function confirmFirstFrame(
  projectId: string,
  firstFrameAssetId: string,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/confirm`,
    "确认首帧失败",
    {
      method: "POST",
      body: JSON.stringify({ first_frame_asset_id: firstFrameAssetId }),
    },
  );
}

export async function getAssetDownloadUrl(
  assetId: string,
): Promise<DownloadUrl> {
  return requestApiJson<DownloadUrl>(
    `/api/assets/${encodeURIComponent(assetId)}/download-url`,
    "读取源画面失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export function readSourceFrameCandidates(
  version: AnalysisVersion,
): SourceFrameCandidates | null {
  const payload = version.payload;
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.requested_timestamps_seconds) ||
    !payload.requested_timestamps_seconds.every(
      (timestamp) => typeof timestamp === "number",
    ) ||
    !Array.isArray(payload.candidates) ||
    !payload.candidates.every(isSourceFrameCandidate)
  ) {
    return null;
  }
  return {
    requested_timestamps_seconds: payload.requested_timestamps_seconds,
    candidates: payload.candidates,
  };
}

export function readFirstFrameCandidates(
  version: AnalysisVersion,
): FirstFrameCandidates | null {
  const payload = version.payload;
  if (
    !isRecord(payload) ||
    typeof payload.provider !== "string" ||
    (payload.model !== "gpt-image-2" &&
      payload.model !== "nano-banana-pro-2k") ||
    typeof payload.prompt !== "string" ||
    !Array.isArray(payload.candidates) ||
    !payload.candidates.every(isFirstFrameCandidate)
  ) {
    return null;
  }
  return {
    provider: payload.provider,
    model: payload.model,
    prompt: payload.prompt,
    candidates: payload.candidates,
  };
}

export function readFirstFrameSelectionPayload(
  version: AnalysisVersion,
): FirstFrameSelectionPayload | null {
  const payload = version.payload;
  if (
    !isRecord(payload) ||
    typeof payload.first_frame_candidates_version_id !== "string" ||
    typeof payload.first_frame_asset_id !== "string"
  ) {
    return null;
  }
  return {
    first_frame_candidates_version_id:
      payload.first_frame_candidates_version_id,
    first_frame_asset_id: payload.first_frame_asset_id,
  };
}

export function readAnalysisPayload(
  version: AnalysisVersion,
): AnalysisPayload | null {
  const analysis = version.payload.analysis;
  if (!isRecord(analysis) || typeof analysis.summary !== "string") {
    return null;
  }
  if (
    typeof analysis.duration_seconds !== "number" ||
    !Array.isArray(analysis.shots) ||
    !analysis.shots.every(isShotCard)
  ) {
    return null;
  }
  return {
    summary: analysis.summary,
    duration_seconds: analysis.duration_seconds,
    shots: analysis.shots,
  };
}

export function readAnalysisProvider(
  version: AnalysisVersion,
): AnalysisProvider | null {
  const responseRef = version.payload.provider_response_ref;
  if (!isRecord(responseRef) || !isRecord(responseRef.raw)) {
    return null;
  }
  const provider = responseRef.raw.provider;
  return provider === "apilio_gemini" || provider === "fake_gemini"
    ? provider
    : null;
}

function isSourceFrameCandidate(value: unknown): value is SourceFrameCandidate {
  return (
    isRecord(value) &&
    typeof value.asset_id === "string" &&
    typeof value.timestamp_seconds === "number" &&
    (typeof value.score === "number" || value.score === null)
  );
}

function isFirstFrameCandidate(value: unknown): value is FirstFrameCandidate {
  return (
    isRecord(value) &&
    typeof value.asset_id === "string" &&
    typeof value.storage_key === "string" &&
    typeof value.storage_uri === "string" &&
    typeof value.sha256 === "string" &&
    typeof value.size_bytes === "number" &&
    typeof value.content_type === "string"
  );
}

export function readShotCardPayload(
  version: AnalysisVersion,
): ShotCardPayload | null {
  const payload = version.payload;
  if (
    typeof payload.source_analysis_version_id !== "string" ||
    typeof payload.duration_seconds !== "number" ||
    !Array.isArray(payload.shots) ||
    !payload.shots.every(isShotCard)
  ) {
    return null;
  }
  return {
    source_analysis_version_id: payload.source_analysis_version_id,
    duration_seconds: payload.duration_seconds,
    shots: payload.shots,
  };
}

export async function getSettings(): Promise<SettingsSnapshot> {
  return requestAdminJson<SettingsSnapshot>(
    "/api/admin/settings",
    "设置暂不可用",
  );
}

export async function updateProviderSettings(
  provider: ProviderName,
  config: Record<string, string>,
): Promise<ProviderSettings> {
  return requestAdminJson<ProviderSettings>(
    `/api/admin/settings/providers/${provider}`,
    "保存设置失败",
    { method: "PUT", body: JSON.stringify({ config }) },
  );
}

export async function updateRuntimeSettings(
  runtime: RuntimeSettings,
): Promise<RuntimeSettings> {
  return requestAdminJson<RuntimeSettings>(
    "/api/admin/settings/runtime",
    "保存运行设置失败",
    { method: "PATCH", body: JSON.stringify(runtime) },
  );
}

export async function runSettingsDiagnostic(): Promise<SettingsDiagnosticReport> {
  return requestAdminJson<SettingsDiagnosticReport>(
    "/api/admin/settings/diagnostic-test",
    "测试设置失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function downloadDiagnosticReport(
  downloadUrl: string,
  reportId: string,
): Promise<void> {
  const response = await requestAdmin(downloadUrl, { method: "GET" });
  if (!response.ok) {
    throw new Error(`下载诊断日志失败（${response.status}）`);
  }

  const blobUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = `settings-diagnostic-${reportId}.json`;
  anchor.click();
  // Defer revocation so WebView2/WKWebView have time to start the download.
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1_000);
}

async function requestJson<T>(path: string, errorPrefix: string): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );

  try {
    const apiBaseUrl =
      import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
    const response = await fetch(`${apiBaseUrl}${path}`, {
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(await responseErrorMessage(response, errorPrefix));
    }

    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function requestAdminJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const response = await requestAdmin(path, init, timeoutMs);
  if (!response.ok) {
    const message = await responseErrorMessage(response, errorPrefix);
    const error = new Error(message) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return (await response.json()) as T;
}

async function requestApiJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const response = await requestApi(path, init, timeoutMs);
  if (!response.ok) {
    const message = await responseErrorMessage(response, errorPrefix);
    const error = new Error(message) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return (await response.json()) as T;
}

async function responseErrorMessage(
  response: Response,
  errorPrefix: string,
): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && isRecord(payload.detail)) {
      const message = payload.detail.message;
      if (typeof message === "string" && message.trim()) {
        return `${errorPrefix}：${message}（${response.status}）`;
      }
    }
  } catch {
    // A missing or non-JSON error body must not hide the HTTP status.
  }
  return `${errorPrefix}（${response.status}）`;
}

async function requestAdmin(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  return requestApi(path, init, timeoutMs);
}

async function requestApi(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
  const headers = new Headers(init.headers);
  const devUserId = getDevelopmentUserId();

  if (init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (devUserId) {
    headers.set("X-Dev-User-Id", devUserId);
  }

  try {
    const response = await fetch(`${apiBaseUrl}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
    if (response.status === 401 && path !== "/api/auth/me") {
      emitSessionExpired();
    }
    return response;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function getDevelopmentUserId(): string | undefined {
  if (!import.meta.env.DEV) {
    return undefined;
  }
  const explicitUserId = import.meta.env.VITE_DEV_USER_ID;
  if (explicitUserId?.trim()) {
    return explicitUserId.trim();
  }
  return "employee_1";
}

function emitSessionExpired() {
  window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

function contentTypeForFile(file: File): "video/mp4" | "video/quicktime" {
  return file.name.toLowerCase().endsWith(".mov")
    ? "video/quicktime"
    : "video/mp4";
}

function contentTypeForIdentityFile(file: File): string {
  const name = file.name.toLowerCase();
  if (name.endsWith(".pdf")) {
    return "application/pdf";
  }
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) {
    return "image/jpeg";
  }
  if (name.endsWith(".png")) {
    return "image/png";
  }
  return file.type || "application/octet-stream";
}

function isLocalApiUploadUrl(url: string): boolean {
  try {
    const target = new URL(url);
    return (
      (target.hostname === "127.0.0.1" || target.hostname === "localhost") &&
      target.port === "8000"
    );
  } catch {
    return false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCurrentUser(value: unknown): value is CurrentUser {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.username === "string" &&
    typeof value.display_name === "string" &&
    (value.role === "employee" ||
      value.role === "admin" ||
      value.role === "auditor")
  );
}

function isAnalysisVersion(value: unknown): value is AnalysisVersion {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.project_id === "string" &&
    typeof value.kind === "string" &&
    typeof value.version_number === "number" &&
    isRecord(value.payload)
  );
}

function isShotCard(value: unknown): value is ShotCard {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.shot_id === "string" &&
    typeof value.start_time === "number" &&
    typeof value.end_time === "number" &&
    typeof value.shot_type === "string" &&
    typeof value.composition === "string" &&
    typeof value.camera_motion === "string" &&
    typeof value.subject === "string" &&
    typeof value.action === "string" &&
    typeof value.scene === "string" &&
    typeof value.spoken_text === "string" &&
    typeof value.transition === "string"
  );
}
