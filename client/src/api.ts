import type { components } from "./generated/api";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;

type HealthResponse = components["schemas"]["HealthResponse"];

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

export async function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health", "本地服务暂不可用");
}

export async function getGenerationBatch(
  batchId: string,
): Promise<GenerationBatch> {
  return requestApiJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    "任务批次暂不可用",
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
        content_type: file.type || contentTypeForFile(file),
        size_bytes: file.size,
      }),
    },
  );
}

export function uploadReferenceVideo(
  intent: UploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(intent.method, intent.url);
    request.timeout = REQUEST_TIMEOUT_MS * 12;
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
      reject(new Error(`上传参考视频失败（${request.status}）`));
    };
    request.onerror = () => reject(new Error("上传参考视频失败（网络错误）"));
    request.ontimeout = () => reject(new Error("上传参考视频失败（请求超时）"));
    request.send(file);
  });
}

export async function completeVideoUpload(
  assetId: string,
): Promise<CompletedUpload> {
  return requestApiJson<CompletedUpload>(
    `/api/assets/${encodeURIComponent(assetId)}/complete`,
    "参考视频预检失败",
    { method: "POST" },
  );
}

export async function startVideoAnalysis(
  projectId: string,
  assetId: string,
  durationSeconds: number,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/analysis`,
    "启动视频拆解失败",
    {
      method: "POST",
      body: JSON.stringify({
        asset_id: assetId,
        duration_seconds: durationSeconds,
      }),
    },
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
  URL.revokeObjectURL(blobUrl);
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
      throw new Error(`${errorPrefix}（${response.status}）`);
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
): Promise<T> {
  const response = await requestAdmin(path, init);
  if (!response.ok) {
    throw new Error(`${errorPrefix}（${response.status}）`);
  }
  return (await response.json()) as T;
}

async function requestApiJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await requestApi(path, init);
  if (!response.ok) {
    throw new Error(`${errorPrefix}（${response.status}）`);
  }
  return (await response.json()) as T;
}

async function requestAdmin(
  path: string,
  init: RequestInit,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
  const headers = new Headers(init.headers);
  const devUserId =
    import.meta.env.VITE_DEV_USER_ID ??
    (import.meta.env.DEV ? "admin_1" : undefined);

  if (init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (devUserId) {
    headers.set("X-Dev-User-Id", devUserId);
  }

  try {
    return await fetch(`${apiBaseUrl}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}

async function requestApi(path: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
  const headers = new Headers(init.headers);
  const devUserId =
    import.meta.env.VITE_DEV_USER_ID ??
    (import.meta.env.DEV ? "employee_1" : undefined);

  if (init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (devUserId) {
    headers.set("X-Dev-User-Id", devUserId);
  }

  try {
    return await fetch(`${apiBaseUrl}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}

function contentTypeForFile(file: File): "video/mp4" | "video/quicktime" {
  return file.name.toLowerCase().endsWith(".mov")
    ? "video/quicktime"
    : "video/mp4";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
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
