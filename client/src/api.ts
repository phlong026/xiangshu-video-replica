import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import type { components } from "./generated/api";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;
// Cloud/storage operations (diagnostics, presigned URLs, archive prechecks)
// may legitimately take much longer than a normal API round-trip.
const CLOUD_OP_TIMEOUT_MS = 60_000;
export const SESSION_EXPIRED_EVENT = "video-replica:session-expired";
let internalAccessToken: string | null = null;
let customerSessionToken: string | null = null;
let customerSessionOwner: symbol | null = null;
// The customer-production admin session exchanges its CSRF value once and
// keeps it in memory only.  Control-plane writes share this value so the
// existing account/billing screens stay behind the same per-operator session
// rather than a legacy proxy identity.  It is deliberately never persisted.
let adminCsrfToken: string | null = null;

export function setAdminCsrfToken(token: string): void {
  adminCsrfToken = token;
}

export function getAdminCsrfToken(): string | null {
  return adminCsrfToken;
}

export function clearAdminCsrfToken(): void {
  adminCsrfToken = null;
}

type ApiRuntimeLocation = Pick<Location, "origin" | "protocol">;

export function resolveApiBaseUrl(
  configuredUrl: string | undefined,
  isProduction: boolean,
  runtimeLocation: ApiRuntimeLocation,
): string {
  const normalizedUrl = configuredUrl?.trim().replace(/\/+$/, "");
  if (normalizedUrl) {
    return normalizedUrl;
  }
  if (isProduction && runtimeLocation.protocol === "https:") {
    return runtimeLocation.origin;
  }
  return DEFAULT_API_BASE_URL;
}

function apiBaseUrl(): string {
  return resolveApiBaseUrl(
    import.meta.env.VITE_API_BASE_URL,
    import.meta.env.PROD,
    window.location,
  );
}

type HealthResponse = components["schemas"]["HealthResponse"];
export type UserRole = "employee" | "admin" | "auditor" | "customer";

export type CurrentUser = {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
};

// Task #7 / PR #65 (Codex P1): the wallet and recharge adapter types derive
// from the regenerated OpenAPI contract instead of handwritten shapes, so the
// schema-drift gate (tsc) catches a later server response change. The internal
// and customer lanes share these aliases; the Control* views extend them.
type OpenApiWalletSnapshot = components["schemas"]["WalletResponse"];
export type WalletSnapshot = Omit<
  OpenApiWalletSnapshot,
  "internal_unit_price_fen" | "min_recharge_fen" | "recharge_step_fen"
> & {
  internal_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
};
export type WalletTransaction =
  components["schemas"]["WalletTransactionResponse"];
export type WalletTransactionPage =
  components["schemas"]["WalletTransactionPage"];
export type RechargeOrderStatus =
  components["schemas"]["RechargeOrderStatusResponse"]["status"];
export type RechargeOrder =
  components["schemas"]["RechargeOrderStatusResponse"];
export type RechargeOrderPage = components["schemas"]["RechargeOrderPage"];
export type CreatedRechargeOrder =
  components["schemas"]["RechargeOrderResponse"];

export type ControlAccount = {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  available_credits: number;
  reserved_credits: number;
  active_token_count: number;
};

export type ControlAccountPage = {
  items: ControlAccount[];
  total: number;
  limit: number;
  offset: number;
};

export type ControlRechargeOrder = RechargeOrder & {
  id: string;
  user_id: string;
  username: string;
  display_name: string;
  provider_trade_no: string | null;
};

export type ControlRechargeOrderPage = {
  items: ControlRechargeOrder[];
  total: number;
  limit: number;
  offset: number;
};

export type ControlWalletTransaction = WalletTransaction & {
  username: string;
};

export type ControlWalletTransactionPage = {
  items: ControlWalletTransaction[];
  total: number;
  limit: number;
  offset: number;
};

export type BillingSettings = {
  internal_base_unit_price_fen: number;
  charged_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
};

export type ControlSettings = {
  providers: Record<ProviderName, ProviderSettings>;
  runtime: RuntimeSettings;
  billing: BillingSettings;
  zpay: {
    provider: "zpay";
    configured: boolean;
    config: Record<string, string>;
  };
  deployment: {
    gateway_url: string;
    notify_url: string;
    return_url: string;
  };
};

export type ControlReconciliation = {
  wallet_count: number;
  wallet_mismatch_count: number;
  paid_order_without_charge_count: number;
  charge_without_paid_order_count: number;
  pending_order_count: number;
};

export type GenerationVersion = Omit<
  components["schemas"]["VersionResult"],
  "payload"
> & { payload: Record<string, unknown> };
export type GenerationVersionState = Omit<
  components["schemas"]["VersionState"],
  "version"
> & { version: GenerationVersion | null };
export type ScriptVersionInput = components["schemas"]["ScriptRequest"];
export type PromptCompileInput = components["schemas"]["PromptCompileRequest"];
export type PromptRevisionInput =
  components["schemas"]["PromptRevisionRequest"];
export type GenerationBatchInput =
  components["schemas"]["GenerationBatchRequest"];
export type GenerationRuntimeLimits =
  components["schemas"]["GenerationRuntimeLimits"];
export type GenerationTask = Omit<
  components["schemas"]["TaskResult"],
  "prompt_snapshot"
> & { prompt_snapshot: Record<string, unknown> | null };
export type BatchProgress = components["schemas"]["BatchProgress"];
export type GenerationBatch = Omit<
  components["schemas"]["BatchResult"],
  "tasks"
> & { tasks: GenerationTask[] };
export type GenerationTaskSummary = components["schemas"]["TaskSummary"];
export type GenerationTaskRetryInput =
  components["schemas"]["GenerationTaskRetryRequest"];
export type ConfirmNotChargedInput =
  components["schemas"]["ConfirmNotChargedRequest"];
export type ReconcileGenerationTaskInput =
  components["schemas"]["ReconcileGenerationTaskRequest"];
export type GenerationReconcileOperation = {
  id: string;
  task_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};
export type PaidRegenerationInput =
  components["schemas"]["PaidRegenerationRequest"];
export type GenerationBatchListItem = Omit<
  components["schemas"]["GenerationBatchListItem"],
  "tasks"
> & { tasks: GenerationTaskSummary[] };
export type GenerationBatchListPage = Omit<
  components["schemas"]["GenerationBatchListPage"],
  "items"
> & { items: GenerationBatchListItem[] };
export type GenerationBatchListFilters = {
  projectId?: string;
  createdByUserId?: string;
  status?:
    | "PENDING"
    | "QUEUED"
    | "NEEDS_ATTENTION"
    | "SUCCEEDED"
    | "COMPLETED_WITH_FAILURES";
  needsAttention?: boolean;
  limit?: number;
  cursor?: string;
};

export type ProviderName = "metaso" | "apilio" | "cos" | "deepseek";

export type ProviderSettings = {
  provider: ProviderName;
  configured: boolean;
  config: Record<string, string>;
};

export type RuntimeSettings = {
  max_generation_count_per_batch: number;
  max_concurrent_h3_tasks: number;
  active_storage_provider: "cos" | "local";
  /** M4/M5 review M2: PostgreSQL-only rollout switch (SQLite lane keeps the
   * legacy global FIFO and rejects a provided value with 422). Absent on the
   * desktop lane and when the caller leaves it unchanged. */
  fair_queue_enabled?: boolean;
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
  analysis_status: "NOT_READY" | "PENDING" | "READY" | "FAILED";
  analysis_task_id?: string | null;
  analysis_error_message?: string | null;
  analysis_retryable?: boolean;
};

export type UploadIntent = {
  asset_id: string;
  project_id: string;
  storage_key?: string | null;
  method: "PUT" | null;
  url: string | null;
  headers: Record<string, string>;
  expires_at: string | null;
  upload_required?: boolean;
};

export type CompletedUpload = {
  asset_id: string;
  project_id: string;
  status: string;
  storage_uri?: string | null;
  sha256: string;
  size_bytes: number;
  content_type: string;
  metadata: { duration_seconds: number };
  analysis_task_id: string | null;
  analysis_task_status: AnalysisTask["status"] | null;
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

export type AnalysisTask = {
  id: string;
  project_id: string;
  asset_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  attempt: number;
  result_version_id: string | null;
  error_code: string | null;
  error_message: string | null;
  failure_phase: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

const analysisTaskWaiters = new Map<string, Promise<AnalysisTask>>();
export type AnalysisProvider = "apilio_gemini" | "fake_gemini";

export type ShotMotion = {
  subject_motion_state:
    | "STATIC"
    | "WALKING"
    | "RUNNING"
    | "TURNING"
    | "GESTURING_ONLY"
    | "OBJECT_MOTION"
    | "NO_PERSON";
  subject_direction:
    | "toward_camera"
    | "away_from_camera"
    | "left"
    | "right"
    | "lateral"
    | "in_place"
    | "none";
  subject_displacement: string;
  hand_action: string;
  camera_motion:
    | "STATIC"
    | "PUSH_IN"
    | "PULL_BACK"
    | "HANDHELD_TRACKING"
    | "PAN"
    | "TILT"
    | "FOLLOW";
  relative_motion: string;
};

export type ShotCard = {
  shot_id: string;
  start_time: number;
  end_time: number;
  shot_type: string;
  composition: string;
  camera_motion: string;
  subject: string;
  person_count?: number | null;
  action: string;
  scene: string;
  spoken_text: string;
  transition: string;
  motion?: ShotMotion | null;
  segment_kind?: "SHOT_CUT" | "ACTION_BEAT" | null;
  boundary_reason?: string | null;
};

export type AnalysisPayload = {
  summary: string;
  duration_seconds: number;
  original_script: string;
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
  persona_id?: string;
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
  technical_score?: number | null;
  semantic_score?: number | null;
  selection_reason?: string;
};

export type SourceFrameCandidates = {
  requested_timestamps_seconds: number[];
  semantic_quality_status: "VERIFIED" | "UNAVAILABLE" | "NOT_REQUESTED";
  candidates: SourceFrameCandidate[];
};

export type SourceFrameSelectionState = {
  version: AnalysisVersion | null;
  stale: boolean;
};

export type SourceFrameTask = {
  id: string;
  project_id: string;
  asset_id: string;
  timestamps_seconds: number[];
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  attempt: number;
  result_version_id: string | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export class SourceFrameTaskFailedError extends Error {
  readonly task: SourceFrameTask;

  constructor(task: SourceFrameTask) {
    super(task.error_message || "候选源画面提取失败，请重新提交。");
    this.name = "SourceFrameTaskFailedError";
    this.task = task;
  }
}

const sourceFrameTaskWaiters = new Map<string, Promise<SourceFrameTask>>();

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

export type GenerateFirstFramesInput =
  components["schemas"]["GenerateFirstFramesRequest"];

export type FirstFrameTaskStage =
  components["schemas"]["FirstFrameTaskResponse"]["stage"];

export interface FirstFrameTask {
  id: string;
  project_id: string;
  status: DurableImageTaskStatus;
  stage: FirstFrameTaskStage;
  attempt: number;
  result_version_id: string | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export type FirstFrameCandidate = {
  asset_id: string;
  storage_key: string;
  storage_uri: string;
  sha256: string;
  size_bytes: number;
  content_type: string;
  quality?: {
    passed: boolean;
    attempt: number;
    issue_codes: string[];
    inspection: Record<string, unknown>;
  } | null;
};

export type FirstFrameCandidates = {
  provider: string;
  model: FirstFrameModel;
  prompt: string;
  candidates: FirstFrameCandidate[];
  reconstruction_mode: string | null;
  character_contract: Record<string, unknown> | null;
  project_character_appearance_version_id: string | null;
  project_appearance: ProjectAppearanceSpec | null;
};

export type ProjectAppearanceSpec = {
  category: string;
  scene: string;
  subject: string;
  outfit_description: string;
  selection_reason: string;
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

export function setInternalAccessToken(token: string | null): void {
  const normalized = token?.trim() ?? "";
  internalAccessToken = normalized || null;
}

/** Keep the active customer workspace credential in memory only.  The Tauri
 * vault remains the persistent source; this bridge exists solely so the
 * shared project/analysis/generation API adapter can authenticate requests. */
export function setCustomerSessionToken(token: string | null): void {
  const normalized = token?.trim() ?? "";
  customerSessionToken = normalized || null;
  customerSessionOwner = null;
}

/** Attach a workspace-owned credential and return an ownership-aware cleanup.
 * A stale React tree may unmount after a newer tree has already attached its
 * token; its cleanup must not clear the newer session. */
export function attachCustomerSessionToken(token: string): () => void {
  const normalized = token.trim();
  if (!normalized) {
    throw new Error("Customer session token is required");
  }
  const owner = Symbol("customer-workspace-session");
  customerSessionToken = normalized;
  customerSessionOwner = owner;
  return () => {
    if (customerSessionOwner === owner) {
      customerSessionToken = null;
      customerSessionOwner = null;
    }
  };
}

function workspaceAccessToken(): string | null {
  return internalAccessToken ?? customerSessionToken;
}

export async function getWallet(): Promise<WalletSnapshot> {
  const wallet = await requestApiJson<OpenApiWalletSnapshot>(
    "/api/wallet",
    "读取钱包失败",
  );
  return requireWalletPricing(wallet);
}

export async function listWalletTransactions({
  limit = 20,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<WalletTransactionPage> {
  return requestApiJson<WalletTransactionPage>(
    `/api/wallet/transactions?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取条数流水失败",
  );
}

export async function createRechargeOrder(
  amountFen: number,
): Promise<CreatedRechargeOrder> {
  return requestApiJson<CreatedRechargeOrder>(
    "/api/recharge-orders",
    "创建充值订单失败",
    { method: "POST", body: JSON.stringify({ amount_fen: amountFen }) },
  );
}

export async function listRechargeOrders({
  limit = 20,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<RechargeOrderPage> {
  return requestApiJson<RechargeOrderPage>(
    `/api/recharge-orders?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取充值订单失败",
  );
}

export async function getRechargeOrder(
  orderNo: string,
): Promise<RechargeOrder> {
  return requestApiJson<RechargeOrder>(
    `/api/recharge-orders/${encodeURIComponent(orderNo)}`,
    "读取充值状态失败",
  );
}

export async function getControlAccounts({
  limit = 50,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<ControlAccountPage> {
  return requestControlJson<ControlAccountPage>(
    `/api/control/accounts?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取内部账号失败",
  );
}

export async function getControlRechargeOrders({
  status,
  limit = 50,
  offset = 0,
}: {
  status?: RechargeOrderStatus;
  limit?: number;
  offset?: number;
} = {}): Promise<ControlRechargeOrderPage> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (status) {
    query.set("status", status);
  }
  return requestControlJson<ControlRechargeOrderPage>(
    `/api/control/recharge-orders?${query}`,
    "读取充值订单失败",
  );
}

export async function getControlWalletTransactions({
  limit = 50,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<ControlWalletTransactionPage> {
  return requestControlJson<ControlWalletTransactionPage>(
    `/api/control/wallet-transactions?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取账务流水失败",
  );
}

export async function getControlReconciliation(): Promise<ControlReconciliation> {
  return requestControlJson<ControlReconciliation>(
    "/api/control/billing-reconciliation",
    "读取对账结果失败",
  );
}

export async function getControlSettings(): Promise<ControlSettings> {
  return requestControlJson<ControlSettings>(
    "/api/control/settings",
    "读取管理设置失败",
  );
}

function newControlWriteIdempotencyKey(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
    return cryptoRef.randomUUID();
  }
  return `control-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function controlWriteInit(
  method: "PATCH" | "PUT" | "POST",
  body: Record<string, unknown>,
  reason: string,
): RequestInit {
  return {
    method,
    headers: {
      "Idempotency-Key": newControlWriteIdempotencyKey(),
    },
    body: JSON.stringify({
      ...body,
      confirm: true,
      reason,
    }),
  };
}

export async function updateControlZPaySettings(input: {
  pid: string;
  key: string;
  enabled_channels: Array<"alipay" | "wxpay">;
}): Promise<ControlSettings["zpay"]> {
  return requestControlJson<ControlSettings["zpay"]>(
    "/api/control/settings/zpay",
    "保存 ZPay 设置失败",
    controlWriteInit("PATCH", input, "更新 ZPay 支付配置"),
  );
}

export async function updateControlBillingSettings(input: {
  internal_base_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
}): Promise<BillingSettings> {
  return requestControlJson<BillingSettings>(
    "/api/control/settings/billing",
    "保存内部价格失败",
    controlWriteInit("PATCH", input, "更新后台计费配置"),
  );
}

export async function updateControlProviderSettings(
  provider: ProviderName,
  config: Record<string, string>,
): Promise<ProviderSettings> {
  return requestControlJson<ProviderSettings>(
    `/api/control/settings/providers/${provider}`,
    "保存服务设置失败",
    controlWriteInit("PUT", { config }, `更新 ${provider} 服务配置`),
  );
}

export async function testControlProviderConnection(
  provider: ProviderName,
): Promise<ProviderTestResult> {
  return requestControlJson<ProviderTestResult>(
    `/api/control/settings/providers/${provider}/connection-test`,
    "连接测试失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function updateControlRuntimeSettings(
  runtime: RuntimeSettings,
): Promise<RuntimeSettings> {
  return requestControlJson<RuntimeSettings>(
    "/api/control/settings/runtime",
    "保存运行设置失败",
    controlWriteInit("PATCH", runtime, "更新后台运行参数"),
  );
}

export async function syncControlRechargeOrder(
  orderNo: string,
  reason: string,
): Promise<RechargeOrder> {
  // A4（2026-09-02 评估）：手动查单现在执行管理端写契约
  // （confirm + reason + 幂等键），服务端会落一条 payment.sync 审计行。
  return requestControlJson<RechargeOrder>(
    `/api/control/recharge-orders/${encodeURIComponent(orderNo)}/sync`,
    "同步充值订单失败",
    controlWriteInit("POST", {}, reason),
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function downloadCustomersCsv(
  options: { status?: string; username?: string } = {},
): Promise<void> {
  const query = new URLSearchParams();
  if (options.status) query.set("status", options.status);
  if (options.username) query.set("username", options.username);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  await downloadControlCsv(
    `/api/control/customers.csv${suffix}`,
    "customers.csv",
  );
}

export async function downloadControlRechargeOrdersCsv(): Promise<void> {
  await downloadControlCsv(
    "/api/control/recharge-orders.csv",
    "recharge-orders.csv",
  );
}

export async function downloadControlWalletTransactionsCsv(): Promise<void> {
  await downloadControlCsv(
    "/api/control/wallet-transactions.csv",
    "wallet-transactions.csv",
  );
}

export async function createScriptVersion(
  projectId: string,
  input: ScriptVersionInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/scripts`,
    "保存口播稿失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function getLatestScriptVersion(
  projectId: string,
): Promise<GenerationVersionState> {
  return requestGenerationJson<GenerationVersionState>(
    `/api/projects/${encodeURIComponent(projectId)}/scripts/latest`,
    "读取口播稿失败",
  );
}

export async function compileGenerationPrompt(
  projectId: string,
  input: PromptCompileInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/compile`,
    "编译视频生成提示词失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function reviseGenerationPrompt(
  projectId: string,
  input: PromptRevisionInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/revise`,
    "保存视频生成提示词失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export type PromptPreviewInput = {
  output_duration_seconds?: number;
  resolution?: "768P" | "2K";
};

export type PromptPreviewResult = {
  prompt_text: string;
  output_duration_seconds: number;
  resolution: "768P" | "2K";
  script_source: "script_version" | "analysis_original";
  shot_card_version_id: string | null;
};

export async function previewGenerationPrompt(
  projectId: string,
  input: PromptPreviewInput = {},
): Promise<PromptPreviewResult> {
  try {
    return await requestGenerationJson<PromptPreviewResult>(
      `/api/projects/${encodeURIComponent(projectId)}/prompts/preview`,
      "生成提示词预览失败",
      { method: "POST", body: JSON.stringify(input) },
    );
  } catch (error) {
    throw previewRequestError(error);
  }
}

// 生成系的通用 409 文案（"上游内容已变化"）描述的是版本级联过期；预览端点的
// 两种 409 都指向拆解数据缺失，换成可行动的原因，避免误导用户。
function previewRequestError(error: unknown): Error {
  const { status, code } = error as RequestError;
  if (
    status === 409 &&
    (code === "ANALYSIS_NOT_READY" || code === "SHOT_CARD_TIMELINE_INVALID")
  ) {
    const mapped = new Error(
      "拆解结果缺少镜头数据，请重新上传视频拆解",
    ) as RequestError;
    mapped.status = status;
    mapped.code = code;
    return mapped;
  }
  return error instanceof Error ? error : new Error("生成提示词预览失败");
}

export async function getLatestGenerationPrompt(
  projectId: string,
): Promise<GenerationVersionState> {
  return requestGenerationJson<GenerationVersionState>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/latest`,
    "读取视频生成提示词失败",
  );
}

export async function lockGenerationPrompt(
  projectId: string,
  promptVersionId: string,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/${encodeURIComponent(promptVersionId)}/lock`,
    "锁定视频生成提示词失败",
    { method: "POST" },
  );
}

export async function getGenerationRuntimeLimits(): Promise<GenerationRuntimeLimits> {
  return requestGenerationJson<GenerationRuntimeLimits>(
    "/api/generation/runtime-limits",
    "读取生成数量上限失败",
  );
}

// 批次 Provider 由 VITE_GENERATION_PROVIDER 显式驱动：未设置时默认走真实
// Metaso H3，避免开发环境静默创建模拟任务、让用户误以为真实生成已触发。
// 需要本地模拟管线（不产生付费调用）时，显式设置
// VITE_GENERATION_PROVIDER=fake_h3。
export function defaultBatchProvider(): GenerationBatchInput["provider"] {
  return import.meta.env.VITE_GENERATION_PROVIDER === "fake_h3"
    ? "fake_h3"
    : "metaso";
}

export async function createGenerationBatch(
  projectId: string,
  input: GenerationBatchInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    `/api/projects/${encodeURIComponent(projectId)}/generation-batches`,
    "创建视频生成批次失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function getGenerationBatch(
  batchId: string,
): Promise<GenerationBatch> {
  return requestApiJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    "任务批次暂不可用",
  );
}

export async function regenerateGenerationBatch(
  batchId: string,
  input: PaidRegenerationInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}/regenerate`,
    "整批重新生成失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function renameGenerationBatch(
  batchId: string,
  displayName: string,
): Promise<GenerationBatch> {
  return requestApiJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}/name`,
    "修改批次名称失败",
    { method: "PATCH", body: JSON.stringify({ display_name: displayName }) },
  );
}

export async function deleteGenerationBatch(batchId: string): Promise<void> {
  const response = await requestApi(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "删除批次失败"));
  }
}

export async function listGenerationBatches(
  filters: GenerationBatchListFilters = {},
): Promise<GenerationBatchListPage> {
  const query = new URLSearchParams();
  if (filters.projectId) {
    query.set("project_id", filters.projectId);
  }
  if (filters.createdByUserId) {
    query.set("created_by_user_id", filters.createdByUserId);
  }
  if (filters.status) {
    query.set("status", filters.status);
  }
  if (filters.needsAttention !== undefined) {
    query.set("needs_attention", String(filters.needsAttention));
  }
  if (filters.limit !== undefined) {
    query.set("limit", String(filters.limit));
  }
  if (filters.cursor) {
    query.set("cursor", filters.cursor);
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return requestApiJson<GenerationBatchListPage>(
    `/api/generation-batches${suffix}`,
    "任务记录列表暂不可用",
  );
}

export async function retryGenerationTask(
  taskId: string,
  input: GenerationTaskRetryInput,
): Promise<GenerationTask> {
  return requestGenerationJson<GenerationTask>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/retry`,
    "重试生成任务失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function regenerateGenerationTask(
  taskId: string,
  input: PaidRegenerationInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/regenerate`,
    "重新生成视频任务失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function confirmGenerationTaskNotCharged(
  taskId: string,
  input: ConfirmNotChargedInput,
): Promise<GenerationTask> {
  return requestGenerationJson<GenerationTask>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/confirm-not-charged`,
    "确认任务未计费失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function getGenerationResultDownloadUrl(
  assetId: string,
): Promise<DownloadUrl> {
  return requestGenerationJson<DownloadUrl>(
    `/api/assets/${encodeURIComponent(assetId)}/download-url`,
    "获取生成结果下载地址失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

// 在线播放：直接复用后端签发的预签名 URL 作为 video src（COS 与本地
// 存储均支持 Range 渐进播放，无需整包下载 blob，首帧秒出）。url 缺失
// 视为契约异常直接抛错，由调用方落入失败分支，避免上层自动签发循环。
export async function createGenerationResultPreviewUrl(
  assetId: string,
): Promise<string> {
  const { url } = await getGenerationResultDownloadUrl(assetId);
  if (!url) {
    throw new Error("预览链接获取失败，请重试。");
  }
  return url;
}

// 直链交付不复制成片到云存储。URL 仅通过任务所属权限接口按需签发，
// 不进入批次详情，避免被列表、日志或跨用户缓存意外暴露。
export async function createGenerationTaskPreviewUrl(
  taskId: string,
): Promise<string> {
  const { url } = await requestGenerationJson<
    components["schemas"]["GenerationTaskPreviewUrlResponse"]
  >(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/preview-url`,
    "获取生成结果播放地址失败",
  );
  if (!url) {
    throw new Error("预览链接获取失败，请重试。");
  }
  return url;
}

export async function downloadGenerationResult(
  assetId: string,
  filename: string,
): Promise<VideoDownloadResult> {
  return downloadVideoResult(
    async () => (await getGenerationResultDownloadUrl(assetId)).url,
    filename,
  );
}

export async function downloadGenerationTaskResult(
  taskId: string,
  filename: string,
): Promise<VideoDownloadResult> {
  return downloadVideoResult(
    () => createGenerationTaskPreviewUrl(taskId),
    filename,
  );
}

export type VideoDownloadResult =
  | { status: "saved"; downloadId: string; path: string }
  | { status: "cancelled" }
  | { status: "started" };

export class VideoDownloadUnconfirmedError extends Error {
  constructor() {
    super("尚未确认保存结果，请先检查所选文件夹，避免重复下载。");
    this.name = "VideoDownloadUnconfirmedError";
  }
}

type NativeVideoDownload = { download_id: string; path: string };
type NativeVideoDownloadFinished = {
  download_id: string;
  success: boolean;
  path: string | null;
  error: string | null;
};

export async function openVideoDownloadFolder(
  downloadId: string,
): Promise<void> {
  try {
    await invoke("open_video_download_folder", { downloadId });
  } catch (error) {
    throw new Error("无法打开文件夹，请按显示的保存路径查找视频。", {
      cause: error,
    });
  }
}

async function downloadVideoResult(
  getUrl: () => Promise<string>,
  filename: string,
): Promise<VideoDownloadResult> {
  if (!isTauri()) {
    downloadBlob(
      await fetchGenerationResultBlob(await getUrl(), "下载生成结果失败"),
      filename,
    );
    // 浏览器不会向页面确认用户是否保存了文件，不能声称已保存。
    return { status: "started" };
  }

  let destination: NativeVideoDownload | null;
  try {
    destination = await invoke<NativeVideoDownload | null>(
      "choose_video_download",
      { filename },
    );
  } catch (error) {
    throw new Error("无法选择保存位置，请检查桌面权限后重试。", {
      cause: error,
    });
  }
  if (!destination) return { status: "cancelled" };

  let blobUrl: string | undefined;
  let unlisten: (() => void) | undefined;
  let timeout: number | undefined;
  let statusTimer: number | undefined;
  let finished = false;
  let started = false;
  try {
    const blob = await fetchGenerationResultBlob(
      await getUrl(),
      "下载生成结果失败",
    );
    blobUrl = URL.createObjectURL(blob);
    let resolveCompletion: (value: NativeVideoDownloadFinished) => void =
      () => {};
    const completion = new Promise<NativeVideoDownloadFinished>((resolve) => {
      resolveCompletion = resolve;
    });
    const finish = (value: NativeVideoDownloadFinished) => {
      if (finished || value.download_id !== destination.download_id) return;
      finished = true;
      resolveCompletion(value);
    };
    const unconfirmed = () =>
      finish({
        download_id: destination.download_id,
        success: false,
        path: null,
        error: "COMPLETION_UNCONFIRMED",
      });
    unlisten = await listen<NativeVideoDownloadFinished>(
      "video-download-finished",
      ({ payload }) => {
        if (payload.download_id === destination.download_id) finish(payload);
      },
    );
    timeout = window.setTimeout(unconfirmed, 300_000);
    await invoke("start_video_download", {
      downloadId: destination.download_id,
      url: blobUrl,
    });
    const anchor = document.createElement("a");
    anchor.href = blobUrl;
    anchor.download = filename;
    document.body.append(anchor);
    try {
      anchor.click();
      started = true;
    } finally {
      anchor.remove();
    }
    // 完成事件可能丢失；只查询本机已记录的结果，不重试下载或猜测成功。
    const checkStatus = async () => {
      try {
        const result = await invoke<NativeVideoDownloadFinished | null>(
          "get_video_download_status",
          { downloadId: destination.download_id },
        );
        if (result) finish(result);
      } catch {
        unconfirmed();
      }
      if (!finished) statusTimer = window.setTimeout(checkStatus, 2_000);
    };
    statusTimer = window.setTimeout(checkStatus, 2_000);
    const result = await completion;
    if (result.error === "COMPLETION_UNCONFIRMED") {
      throw new VideoDownloadUnconfirmedError();
    }
    if (!result.success || !result.path) {
      throw new Error(
        "保存视频失败或下载被中断，请检查磁盘空间和文件夹权限后重试。",
      );
    }
    return {
      status: "saved",
      downloadId: destination.download_id,
      path: result.path,
    };
  } catch (error) {
    if (!started) {
      try {
        await invoke("cancel_video_download", {
          downloadId: destination.download_id,
        });
      } catch (cleanupError) {
        throw new Error(
          "下载未完成且未能清理下载请求，请重新打开客户端后重试。",
          {
            cause: new AggregateError([error, cleanupError]),
          },
        );
      }
    }
    throw error;
  } finally {
    finished = true;
    window.clearTimeout(timeout);
    window.clearTimeout(statusTimer);
    unlisten?.();
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  }
}

async function fetchGenerationResultBlob(
  url: string,
  errorPrefix: string,
): Promise<Blob> {
  const inlinePrefix = "data:video/mp4;base64,";
  if (url.startsWith(inlinePrefix)) {
    // 测试成片已随授权接口返回，直接解码避免触发桌面端 connect-src 限制。
    try {
      const content = atob(url.slice(inlinePrefix.length));
      const bytes = Uint8Array.from(content, (character) =>
        character.charCodeAt(0),
      );
      return new Blob([bytes], { type: "video/mp4" });
    } catch (error) {
      throw new Error(`${errorPrefix}：内联视频数据无效。`, { cause: error });
    }
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    CLOUD_OP_TIMEOUT_MS,
  );

  try {
    // 任务权限由应用接口校验；下载文件时不向供应商传递会话凭据。
    const response = await fetch(url, {
      signal: controller.signal,
      credentials: "omit",
    });
    if (!response.ok) {
      throw new Error(`${errorPrefix}（${response.status}）`);
    }
    return await response.blob();
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function reconcileUncertainTask(
  taskId: string,
  input: ReconcileGenerationTaskInput,
): Promise<GenerationReconcileOperation> {
  return requestApiJson<GenerationReconcileOperation>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/reconcile`,
    "任务对账失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

const generationReconcileWaiters = new Map<
  string,
  Promise<GenerationReconcileOperation>
>();

export async function getGenerationReconcileOperation(
  operationId: string,
): Promise<GenerationReconcileOperation> {
  return requestApiJson<GenerationReconcileOperation>(
    `/api/generation-reconcile-operations/${encodeURIComponent(operationId)}`,
    "读取任务对账进度失败",
  );
}

export async function getLatestGenerationReconcileOperation(
  taskId: string,
): Promise<GenerationReconcileOperation | null> {
  const response = await requestApi(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/reconcile/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取任务对账进度失败（${response.status}）`);
  }
  return (await response.json()) as GenerationReconcileOperation | null;
}

export async function waitForGenerationReconcileOperation(
  operationId: string,
): Promise<GenerationReconcileOperation> {
  const existing = generationReconcileWaiters.get(operationId);
  if (existing) {
    return existing;
  }
  const waiter = pollGenerationReconcileOperation(operationId);
  generationReconcileWaiters.set(operationId, waiter);
  const clear = () => {
    if (generationReconcileWaiters.get(operationId) === waiter) {
      generationReconcileWaiters.delete(operationId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollGenerationReconcileOperation(
  operationId: string,
): Promise<GenerationReconcileOperation> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const operation = await getGenerationReconcileOperation(operationId);
    if (operation.status === "SUCCEEDED") {
      return operation;
    }
    if (operation.status === "FAILED") {
      throw new Error(operation.error_message || "任务对账失败，请重新提交。");
    }
    await waitForPoll();
  }
  throw new Error("任务仍在后台对账，请稍后返回查看。");
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

export async function renameProject(
  projectId: string,
  name: string,
): Promise<Project> {
  return requestApiJson<Project>(
    `/api/projects/${encodeURIComponent(projectId)}/name`,
    "修改项目名称失败",
    { method: "PATCH", body: JSON.stringify({ name }) },
  );
}

export async function createVideoUploadIntent(
  projectId: string,
  file: File,
): Promise<UploadIntent> {
  const sha256 = await sha256ForUpload(file);
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
        ...(sha256 === null ? {} : { sha256 }),
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
  if (
    intent.upload_required === false ||
    intent.method !== "PUT" ||
    !intent.url
  ) {
    return Promise.reject(new Error("该视频已存在，无需重复上传。"));
  }
  return uploadStorageObject(
    { headers: intent.headers, method: intent.method, url: intent.url },
    file,
    onProgress,
    "上传参考视频",
    signal,
  );
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
    if (isLocalApiUploadUrl(intent.url)) {
      // Mirror requestApi's auth precedence: the internal Bearer token wins in
      // managed mode; otherwise fall back to the development identity header.
      const accessToken = workspaceAccessToken();
      if (accessToken) {
        request.setRequestHeader("Authorization", `Bearer ${accessToken}`);
      } else if (devUserId) {
        request.setRequestHeader("X-Dev-User-Id", devUserId);
      }
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
    request.onerror = () =>
      reject(
        new Error(
          isLocalApiUploadUrl(intent.url)
            ? `${errorPrefix}失败（无法连接本地服务，请确认服务已启动）`
            : `${errorPrefix}失败（无法连接素材库；请检查网络以及素材库跨域访问规则）`,
        ),
      );
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

// The reference duration is never sent from here: the server already stores the
// ffprobe measurement taken during upload, and echoing it back only risks a
// request-validation rejection when the two contracts drift apart.
export async function startVideoAnalysis(
  projectId: string,
  assetId: string,
): Promise<AnalysisTask> {
  const errorPrefix = "启动视频拆解失败";
  try {
    return await requestApiJson<AnalysisTask>(
      `/api/projects/${encodeURIComponent(projectId)}/analysis-tasks`,
      errorPrefix,
      {
        method: "POST",
        // Clicking “重新拆解” must publish a fresh immutable analysis version;
        // existing versions are loaded separately when the workspace opens.
        body: JSON.stringify({ asset_id: assetId, reuse_existing: false }),
      },
    );
  } catch (error) {
    throw analysisRequestError(error, errorPrefix);
  }
}

export async function getAnalysisTask(taskId: string): Promise<AnalysisTask> {
  return requestApiJson<AnalysisTask>(
    `/api/analysis-tasks/${encodeURIComponent(taskId)}`,
    "读取视频拆解任务失败",
  );
}

export async function waitForAnalysisTask(
  taskId: string,
): Promise<AnalysisTask> {
  const existing = analysisTaskWaiters.get(taskId);
  if (existing) {
    return existing;
  }
  const waiter = pollAnalysisTask(taskId);
  analysisTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (analysisTaskWaiters.get(taskId) === waiter) {
      analysisTaskWaiters.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollAnalysisTask(taskId: string): Promise<AnalysisTask> {
  const deadline = Date.now() + 20 * 60_000;
  while (Date.now() < deadline) {
    const task = await getAnalysisTask(taskId);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED") {
      throw new Error(task.error_message || "视频拆解失败，请重新提交。");
    }
    await waitForPoll();
  }
  throw new Error("视频拆解仍在后台进行，请稍后返回项目列表查看。");
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
  const version = (await response.json()) as unknown;
  return isAnalysisVersion(version) ? version : null;
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

export type ScriptRewriteResult = {
  rewritten_text: string;
  provider: string;
  model: string;
};

export type ScriptRewriteTask = {
  id: string;
  project_id: string;
  status:
    | "PENDING"
    | "RUNNING"
    | "SUCCEEDED"
    | "FAILED"
    | "SUBMISSION_UNCERTAIN";
  attempt: number;
  result: ScriptRewriteResult | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

const scriptRewriteTaskWaiters = new Map<string, Promise<ScriptRewriteTask>>();

export async function rewriteProjectScript(
  projectId: string,
  text: string,
): Promise<ScriptRewriteTask> {
  return requestApiJson<ScriptRewriteTask>(
    `/api/projects/${encodeURIComponent(projectId)}/script-rewrite`,
    "AI 改写失败",
    {
      method: "POST",
      body: JSON.stringify({
        text,
        idempotency_key: newControlWriteIdempotencyKey(),
      }),
    },
  );
}

export async function getScriptRewriteTask(
  taskId: string,
): Promise<ScriptRewriteTask> {
  return requestApiJson<ScriptRewriteTask>(
    `/api/script-rewrite-tasks/${encodeURIComponent(taskId)}`,
    "读取 AI 改写任务失败",
  );
}

export async function getLatestScriptRewriteTask(
  projectId: string,
): Promise<ScriptRewriteTask | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/script-rewrite-tasks/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取 AI 改写任务失败（${response.status}）`);
  }
  return (await response.json()) as ScriptRewriteTask | null;
}

export async function waitForScriptRewriteTask(
  taskId: string,
): Promise<ScriptRewriteTask> {
  const existing = scriptRewriteTaskWaiters.get(taskId);
  if (existing) {
    return existing;
  }
  const waiter = pollScriptRewriteTask(taskId);
  scriptRewriteTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (scriptRewriteTaskWaiters.get(taskId) === waiter) {
      scriptRewriteTaskWaiters.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollScriptRewriteTask(
  taskId: string,
): Promise<ScriptRewriteTask> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const task = await getScriptRewriteTask(taskId);
    if (task.status === "SUCCEEDED") {
      if (!task.result) {
        throw new Error("AI 改写已完成，但结果暂不可用，请刷新后重试。");
      }
      return task;
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new Error(task.error_message || "AI 改写失败，请重新提交。");
    }
    await waitForPoll();
  }
  throw new Error("AI 改写仍在后台执行，请稍后返回查看。");
}

export type CharacterViewType =
  | "FRONT_FACE"
  | "FRONT_HALF"
  | "FRONT_FULL"
  | "LEFT_45"
  | "RIGHT_45"
  | "LEFT_SIDE"
  | "RIGHT_SIDE";

export interface SimpleCharacterView {
  view_type: CharacterViewType;
  asset_id: string;
}

export interface SimpleCharacterResult {
  identity_id: string;
  persona_id: string;
  character_version_id: string;
  publication_hash: string;
  contact_sheet_asset_id: string;
  generation_source: "image_provider" | "local_placeholder";
  views: SimpleCharacterView[];
}

export interface SimpleLibraryEntry {
  identity_id: string;
  display_name: string;
  owner_user_id: string | null;
  status: string;
  contact_sheet_asset_id: string | null;
  generation_source: "image_provider" | "local_placeholder" | null;
  views: SimpleCharacterView[];
}

export type DurableImageTaskStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "SUBMISSION_UNCERTAIN";

export interface CharacterSheetTask {
  id: string;
  project_id: string | null;
  identity_id: string | null;
  operation: "CREATE" | "REGENERATE" | "SCENE";
  display_name: string;
  status: DurableImageTaskStatus;
  attempt: number;
  result_identity_id: string | null;
  result_version_id: string | null;
  result:
    | SimpleCharacterResult
    | SimpleCharacterRegenerationResult
    | SimpleSceneLook
    | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

function createRequestKey(prefix: string): string {
  const suffix =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function waitForPoll(delayMs = 1_500): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, delayMs));
}

const characterSheetTaskWaiters = new Map<
  string,
  Promise<CharacterSheetTask>
>();
type FirstFrameTaskObserver = (task: FirstFrameTask) => void;

type FirstFrameTaskWaiter = {
  observers: Set<FirstFrameTaskObserver>;
  promise: Promise<FirstFrameTask>;
};

const firstFrameTaskWaiters = new Map<string, FirstFrameTaskWaiter>();
export async function uploadSimpleCharacter(
  projectId: string | null,
  file: File,
  displayName: string,
  personaName = "",
): Promise<SimpleCharacterResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("display_name", displayName);
  if (personaName.trim()) {
    form.append("persona_name", personaName.trim());
  }
  form.append("idempotency_key", createRequestKey("character-sheet"));
  const endpoint = projectId
    ? `/api/simple-characters/tasks/${encodeURIComponent(projectId)}/generate`
    : "/api/simple-characters/tasks/generate";
  const task = await requestApiJson<CharacterSheetTask>(
    endpoint,
    "一键创建人物失败",
    { method: "POST", body: form },
  );
  const completed = await waitForCharacterSheetTask(task.id);
  if (!completed.result || !("persona_id" in completed.result)) {
    throw new Error("人物生成任务完成但结果不可用，请重新读取人物库。");
  }
  return completed.result as SimpleCharacterResult;
}

export async function renamePersonIdentity(
  identityId: string,
  displayName: string,
): Promise<PersonIdentity> {
  return requestApiJson<PersonIdentity>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/name`,
    "修改人物名称失败",
    { method: "PATCH", body: JSON.stringify({ display_name: displayName }) },
  );
}

export async function deleteSimpleCharacterIdentity(
  identityId: string,
): Promise<void> {
  const response = await requestApi(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(
      await responseErrorMessage(response, "删除人物失败，请稍后重试。"),
    );
  }
}

export interface SimpleCharacterRegenerationResult {
  identity_id: string;
  persona_id: string;
  character_version_id: string;
  previous_version_id: string;
  version_number: number;
  publication_hash: string;
  contact_sheet_asset_id: string;
  generation_source: "image_provider" | "local_placeholder";
  views: SimpleCharacterView[];
}

export interface SimpleSceneLook {
  identity_id: string;
  persona_id: string;
  character_version_id: string;
  scene_name: string;
  scene_description: string;
  costume_description: string;
  contact_sheet_asset_id: string;
  generation_source: "image_provider" | "local_placeholder";
  views: SimpleCharacterView[];
  published_at?: string | null;
}

// Re-run the identity-preserve contact sheet from the stored source photo.
// The provider call can take 1–3 minutes, so reuse the analysis-sized budget.
export async function regenerateContactSheet(
  identityId: string,
): Promise<SimpleCharacterRegenerationResult> {
  const form = new FormData();
  form.append("idempotency_key", createRequestKey("character-regenerate"));
  const task = await requestApiJson<CharacterSheetTask>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/regenerate-contact-sheet-task`,
    "重新生成多视图失败",
    { method: "POST", body: form },
  );
  const completed = await waitForCharacterSheetTask(task.id);
  if (!completed.result || !("previous_version_id" in completed.result)) {
    throw new Error("人物重新生成任务完成但结果不可用，请重新读取人物库。");
  }
  return completed.result as SimpleCharacterRegenerationResult;
}

export async function getCharacterSheetTask(
  taskId: string,
): Promise<CharacterSheetTask> {
  return requestApiJson<CharacterSheetTask>(
    `/api/simple-characters/task-status/${encodeURIComponent(taskId)}`,
    "读取人物生成任务失败",
  );
}

export async function getLatestCharacterSheetTask(): Promise<CharacterSheetTask | null> {
  return requestApiJson<CharacterSheetTask | null>(
    "/api/simple-characters/tasks/active-or-latest",
    "读取人物生成任务失败",
  );
}

export async function getLatestSceneLookTask(
  identityId: string,
): Promise<CharacterSheetTask | null> {
  return requestApiJson<CharacterSheetTask | null>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/scene-looks/tasks/active-or-latest`,
    "读取场景造型生成任务失败",
  );
}

export async function waitForCharacterSheetTask(
  taskId: string,
): Promise<CharacterSheetTask> {
  const existing = characterSheetTaskWaiters.get(taskId);
  if (existing) {
    return existing;
  }
  const waiter = pollCharacterSheetTask(taskId);
  characterSheetTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (characterSheetTaskWaiters.get(taskId) === waiter) {
      characterSheetTaskWaiters.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollCharacterSheetTask(
  taskId: string,
): Promise<CharacterSheetTask> {
  // 场景造型最坏要两轮生成加质检（约 16 分钟），轮询死线留足余量。
  const deadline = Date.now() + 30 * 60_000;
  while (Date.now() < deadline) {
    const task = await getCharacterSheetTask(taskId);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new Error(task.error_message || "人物生成失败，请重新提交。");
    }
    await waitForPoll();
  }
  throw new Error("人物生成仍在后台进行，请稍后返回人物库查看。");
}

export async function listSimpleCharacterLibrary(): Promise<
  SimpleLibraryEntry[]
> {
  return requestApiJson<SimpleLibraryEntry[]>(
    "/api/simple-characters/library",
    "读取人物库失败",
  );
}

export async function listCharacterSceneLooks(
  identityId: string,
): Promise<SimpleSceneLook[]> {
  return requestApiJson<SimpleSceneLook[]>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/scene-looks`,
    "读取人物场景造型失败",
  );
}

export async function createCharacterSceneLook(
  identityId: string,
  input: {
    scene_name: string;
    scene_description: string;
    costume_description: string;
  },
): Promise<SimpleSceneLook> {
  const task = await requestApiJson<CharacterSheetTask>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/scene-looks/tasks/generate`,
    "启动场景造型生成失败",
    {
      method: "POST",
      body: JSON.stringify({
        ...input,
        idempotency_key: createRequestKey("scene-look"),
      }),
    },
  );
  const completed = await waitForCharacterSheetTask(task.id);
  if (!completed.result || !("scene_name" in completed.result)) {
    throw new Error("场景造型任务完成但结果不可用，请重新读取人物库。");
  }
  return completed.result;
}

export async function downloadCharacterAsset(
  assetId: string,
  filename: string,
): Promise<void> {
  const download = await getCachedCharacterAssetUrl(assetId);
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    CLOUD_OP_TIMEOUT_MS,
  );
  try {
    const response = await fetch(download.url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`下载人物视角图失败（${response.status}）`);
    }
    downloadBlob(await response.blob(), filename);
  } finally {
    window.clearTimeout(timeout);
  }
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
  return (await response.json()) as ProjectMainCharacter | null;
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
  const version = (await response.json()) as unknown;
  return isAnalysisVersion(version) ? version : null;
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
  const version = (await response.json()) as unknown;
  return {
    version: isAnalysisVersion(version) ? version : null,
    stale: false,
  };
}

export async function extractSourceFrames(
  projectId: string,
  assetId: string,
  timestampsSeconds: number[],
): Promise<SourceFrameTask> {
  return requestApiJson<SourceFrameTask>(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/extract`,
    "提取候选源画面失败",
    {
      method: "POST",
      body: JSON.stringify({
        asset_id: assetId,
        timestamps_seconds: timestampsSeconds,
        idempotency_key: newControlWriteIdempotencyKey(),
      }),
    },
  );
}

export async function getSourceFrameTask(
  taskId: string,
): Promise<SourceFrameTask> {
  return requestApiJson<SourceFrameTask>(
    `/api/source-frame-tasks/${encodeURIComponent(taskId)}`,
    "读取候选源画面任务失败",
  );
}

export async function cancelSourceFrameTask(
  taskId: string,
): Promise<SourceFrameTask> {
  return requestApiJson<SourceFrameTask>(
    `/api/source-frame-tasks/${encodeURIComponent(taskId)}/cancel`,
    "停止候选源画面任务失败",
    { method: "POST" },
  );
}

export async function getLatestProjectSourceFrameTask(
  projectId: string,
  assetId: string,
): Promise<SourceFrameTask | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/source-frame-tasks/latest?asset_id=${encodeURIComponent(assetId)}`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取候选源画面任务失败（${response.status}）`);
  }
  return (await response.json()) as SourceFrameTask | null;
}

export async function waitForSourceFrameTask(
  taskId: string,
): Promise<SourceFrameTask> {
  const existing = sourceFrameTaskWaiters.get(taskId);
  if (existing) {
    return existing;
  }
  const waiter = pollSourceFrameTask(taskId);
  sourceFrameTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (sourceFrameTaskWaiters.get(taskId) === waiter) {
      sourceFrameTaskWaiters.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollSourceFrameTask(taskId: string): Promise<SourceFrameTask> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const task = await getSourceFrameTask(taskId);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED") {
      throw new SourceFrameTaskFailedError(task);
    }
    await waitForPoll();
  }
  throw new Error("候选源画面仍在后台提取，请稍后返回查看。");
}

export async function confirmSourceFrame(
  projectId: string,
  sourceFrameAssetId: string,
  characterFeatures?: SourceFrameCharacterFeatures | null,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/confirm`,
    "确认源画面失败",
    {
      method: "POST",
      body: JSON.stringify({
        source_frame_asset_id: sourceFrameAssetId,
        ...(characterFeatures ? { character_features: characterFeatures } : {}),
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
  return (await response.json()) as CharacterReferenceSelection | null;
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
  const version = (await response.json()) as unknown;
  return {
    version: isAnalysisVersion(version) ? version : null,
    stale: false,
  };
}

export async function generateFirstFrames(
  projectId: string,
  input: GenerateFirstFramesInput,
  onTaskUpdate?: FirstFrameTaskObserver,
): Promise<AnalysisVersion> {
  const task = await requestApiJson<FirstFrameTask>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frame-tasks`,
    "生成人物置换首帧失败",
    {
      method: "POST",
      body: JSON.stringify({
        ...input,
        idempotency_key: createRequestKey("first-frame"),
      }),
    },
  );
  onTaskUpdate?.(task);
  return resumeFirstFrameGeneration(projectId, task.id, onTaskUpdate);
}

export async function resumeFirstFrameGeneration(
  projectId: string,
  taskId: string,
  onTaskUpdate?: FirstFrameTaskObserver,
): Promise<AnalysisVersion> {
  const completed = await waitForFirstFrameTask(taskId, onTaskUpdate);
  const latest = await getLatestProjectFirstFrames(projectId);
  if (
    !completed.result_version_id ||
    !latest.version ||
    latest.version.id !== completed.result_version_id
  ) {
    throw new Error("首帧任务已完成，但最新版本尚未同步，请重新读取项目。");
  }
  return latest.version;
}

export async function getFirstFrameTask(
  taskId: string,
): Promise<FirstFrameTask> {
  return requestApiJson<FirstFrameTask>(
    `/api/first-frame-tasks/${encodeURIComponent(taskId)}`,
    "读取首帧生成任务失败",
  );
}

export async function getLatestFirstFrameTask(
  projectId: string,
): Promise<FirstFrameTask | null> {
  return requestApiJson<FirstFrameTask | null>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frame-tasks/active-or-latest`,
    "读取首帧生成任务失败",
  );
}

export async function waitForFirstFrameTask(
  taskId: string,
  onTaskUpdate?: FirstFrameTaskObserver,
): Promise<FirstFrameTask> {
  let waiter = firstFrameTaskWaiters.get(taskId);
  if (!waiter) {
    const observers = new Set<FirstFrameTaskObserver>();
    const promise = pollFirstFrameTask(taskId, (task) => {
      for (const observer of observers) {
        observer(task);
      }
    });
    waiter = { observers, promise };
    firstFrameTaskWaiters.set(taskId, waiter);
    const clear = () => {
      if (firstFrameTaskWaiters.get(taskId) === waiter) {
        firstFrameTaskWaiters.delete(taskId);
      }
    };
    void promise.then(clear, clear);
  }
  if (onTaskUpdate) {
    waiter.observers.add(onTaskUpdate);
  }
  return waiter.promise.finally(() => {
    if (onTaskUpdate) {
      waiter.observers.delete(onTaskUpdate);
    }
  });
}

async function pollFirstFrameTask(
  taskId: string,
  onTaskUpdate: FirstFrameTaskObserver,
): Promise<FirstFrameTask> {
  // 两轮生成加质检的最坏耗时约 30 分钟；超时后任务仍在云端继续，
  // 重新进入项目会通过 active-or-latest 接上。
  const deadline = Date.now() + 30 * 60_000;
  while (Date.now() < deadline) {
    const task = await getFirstFrameTask(taskId);
    onTaskUpdate(task);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new Error(task.error_message || "首帧生成失败，请重新提交。");
    }
    await waitForPoll();
  }
  throw new Error("首帧仍在后台生成，请稍后返回项目查看。");
}

export async function confirmFirstFrame(
  projectId: string,
  firstFrameAssetId: string,
  options?: { allowUnverified?: boolean },
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/confirm`,
    "确认首帧失败",
    {
      method: "POST",
      body: JSON.stringify({
        first_frame_asset_id: firstFrameAssetId,
        ...(options?.allowUnverified ? { allow_unverified: true } : {}),
      }),
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

export async function getCachedCharacterAssetUrl(
  assetId: string,
): Promise<DownloadUrl> {
  return requestApiJson<DownloadUrl>(
    `/api/assets/${encodeURIComponent(assetId)}/cached-url`,
    "读取人物图片缓存失败",
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
    semantic_quality_status:
      payload.semantic_quality_status === "VERIFIED" ||
      payload.semantic_quality_status === "UNAVAILABLE" ||
      payload.semantic_quality_status === "NOT_REQUESTED"
        ? payload.semantic_quality_status
        : "NOT_REQUESTED",
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
    reconstruction_mode:
      typeof payload.reconstruction_mode === "string"
        ? payload.reconstruction_mode
        : null,
    character_contract: isRecord(payload.character_contract)
      ? payload.character_contract
      : null,
    project_character_appearance_version_id:
      typeof payload.project_character_appearance_version_id === "string"
        ? payload.project_character_appearance_version_id
        : null,
    project_appearance: readProjectAppearance(payload.project_appearance),
  };
}

function readProjectAppearance(value: unknown): ProjectAppearanceSpec | null {
  if (
    !isRecord(value) ||
    typeof value.category !== "string" ||
    typeof value.scene !== "string" ||
    typeof value.subject !== "string" ||
    typeof value.outfit_description !== "string" ||
    typeof value.selection_reason !== "string"
  ) {
    return null;
  }
  return {
    category: value.category,
    scene: value.scene,
    subject: value.subject,
    outfit_description: value.outfit_description,
    selection_reason: value.selection_reason,
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
    original_script:
      typeof analysis.original_script === "string"
        ? analysis.original_script
        : analysis.shots
            .map((shot) => shot.spoken_text)
            .filter(Boolean)
            .join(""),
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
  const qualityValid =
    !isRecord(value) ||
    value.quality === undefined ||
    value.quality === null ||
    (isRecord(value.quality) &&
      typeof value.quality.passed === "boolean" &&
      typeof value.quality.attempt === "number" &&
      Array.isArray(value.quality.issue_codes) &&
      value.quality.issue_codes.every((code) => typeof code === "string") &&
      isRecord(value.quality.inspection));
  return (
    isRecord(value) &&
    typeof value.asset_id === "string" &&
    typeof value.storage_key === "string" &&
    typeof value.storage_uri === "string" &&
    typeof value.sha256 === "string" &&
    typeof value.size_bytes === "number" &&
    typeof value.content_type === "string" &&
    qualityValid
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

export async function testProviderConnection(
  provider: ProviderName,
): Promise<ProviderTestResult> {
  return requestAdminJson<ProviderTestResult>(
    `/api/admin/settings/providers/${provider}/connection-test`,
    "连接测试失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export type ProviderTestResult = {
  status: string;
  provider: string;
  test_kind: string;
};

export async function downloadDiagnosticReport(
  downloadUrl: string,
  reportId: string,
): Promise<void> {
  const response = await requestAdmin(downloadUrl, { method: "GET" });
  if (!response.ok) {
    throw new Error(`下载诊断日志失败（${response.status}）`);
  }

  downloadBlob(await response.blob(), `settings-diagnostic-${reportId}.json`);
}

async function downloadControlCsv(
  path: string,
  filename: string,
): Promise<void> {
  const response = await requestControl(
    path,
    { method: "GET" },
    CLOUD_OP_TIMEOUT_MS,
  );
  if (!response.ok) {
    throw new Error(`下载管理导出失败（${response.status}）`);
  }

  downloadBlob(await response.blob(), filename);
}

function downloadBlob(blob: Blob, filename: string): void {
  const blobUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
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
    const response = await fetch(`${apiBaseUrl()}${path}`, {
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
    const details = await responseErrorDetails(response, errorPrefix);
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
    throw error;
  }
  return (await response.json()) as T;
}

async function requestControlJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const response = await requestControl(path, init, timeoutMs);
  if (!response.ok) {
    const details = await responseErrorDetails(response, errorPrefix);
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
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
    const details = await responseErrorDetails(response, errorPrefix);
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
    error.retryable = details.retryable;
    throw error;
  }
  return (await response.json()) as T;
}

async function requestGenerationJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  try {
    return await requestApiJson<T>(path, errorPrefix, init, timeoutMs);
  } catch (error) {
    throw generationRequestError(error, errorPrefix);
  }
}

function generationRequestError(error: unknown, errorPrefix: string): Error {
  const { status, code } = error as RequestError;
  const statusMessage =
    status === 401
      ? "登录已失效，请重新进入工作台"
      : status === 403
        ? "当前账号无权执行此操作"
        : status === 409
          ? "上游内容已变化，请重新确认后再试"
          : status === 422
            ? "生成参数无效，请检查后重试"
            : status === 429
              ? "请求过于频繁，请稍后重试"
              : status !== undefined && status >= 500
                ? "生成服务暂不可用，请稍后重试"
                : null;
  if (statusMessage) {
    const mapped = new Error(statusMessage) as RequestError;
    mapped.status = status;
    mapped.code = code;
    return mapped;
  }
  if (error instanceof Error && error.message === "请求超时，请重试") {
    return new Error(`${errorPrefix}：请求超时，请重试`);
  }
  if (error instanceof TypeError) {
    return new Error(`${errorPrefix}：网络连接失败，请检查本地服务`);
  }
  return error instanceof Error ? error : new Error(errorPrefix);
}

// The server already returns a readable Chinese reason for every provider failure;
// this only fills the gaps where it cannot (request validation, auth, transport),
// so the desktop never shows a bare HTTP status for a failed analysis.
function analysisRequestError(error: unknown, errorPrefix: string): Error {
  const { status, code, retryable } = error as RequestError;
  const fallback =
    status === 422
      ? "参考视频不满足拆解要求，请确认时长在 4–15 秒之间后重试"
      : status === 401
        ? "登录已失效，请重新进入工作台"
        : status === 403
          ? "当前账号无权启动视频拆解"
          : null;
  // A missing code means the server answered with something other than our error
  // envelope (FastAPI request validation, a proxy, an auth redirect).
  if (fallback && code === undefined && error instanceof Error) {
    const mapped = new Error(
      `${errorPrefix}：${fallback}（${status}）`,
    ) as RequestError;
    mapped.status = status;
    mapped.code = code;
    mapped.retryable = retryable;
    return mapped;
  }
  if (error instanceof Error && error.message === "请求超时，请重试") {
    return new Error(`${errorPrefix}：请求超时，请重试`);
  }
  if (error instanceof TypeError) {
    return new Error(`${errorPrefix}：网络连接失败，请检查本地服务`);
  }
  return error instanceof Error ? error : new Error(errorPrefix);
}

async function responseErrorMessage(
  response: Response,
  errorPrefix: string,
): Promise<string> {
  return (await responseErrorDetails(response, errorPrefix)).message;
}

type RequestError = Error & {
  status?: number;
  code?: string;
  retryable?: boolean;
  requestId?: string;
};

const BRANDED_SERVICE_ERRORS: ReadonlyArray<{
  pattern: RegExp;
  message: string;
}> = [
  {
    pattern: /metaso|minimax|(?:^|[_\W])h3(?:$|[_\W])/i,
    message: "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern:
      /(?:apilio|gemini).*(?:首帧|图像|人物)|(?:首帧|图像|人物).*(?:apilio|gemini)/i,
    message: "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /apilio|gemini/i,
    message: "视频拆解服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /gpt[\s_-]*image|nano[\s_-]*banana/i,
    message: "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /(?:^|[_\W])cos(?:$|[_\W])|腾讯云|myqcloud/i,
    message: "素材库暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /deepseek/i,
    message: "文案优化服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /zpay/i,
    message:
      "在线支付暂时不可用，请稍后重试；如已扣款，请勿重复支付并联系客服。",
  },
];

const CUSTOMER_ACCOUNT_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  ACTIVATION_UNAVAILABLE: "该激活码当前无法使用，请确认激活码仍在有效期内。",
  PAIRING_UNAVAILABLE: "该激活码当前无法用于设备配对，请联系服务人员处理。",
  SESSION_CONFLICT: "另一台设备当前正在使用此账号，请稍后重新打开应用。",
  SESSION_EXPIRED: "登录已过期，请重新登录。",
  SESSION_REPLACED: "当前设备已被另一台设备切换下线，请重新登录。",
  DEVICE_REVOKED: "当前设备凭据已失效，请联系服务人员处理。",
};

/**
 * Keep provider identifiers and raw diagnostics intact inside the API and
 * server, while translating only messages that are about to reach a product
 * surface. Non-branded, actionable errors are preserved verbatim.
 */
export function customerVisibleErrorMessage(
  error: unknown,
  fallback = "操作失败，请稍后重试。",
): string {
  let message = "";
  let code = "";
  let requestId = "";

  if (typeof error === "string") {
    message = error.trim();
  } else if (error instanceof Error) {
    message = error.message.trim();
    const details = error as RequestError;
    code = details.code?.trim() ?? "";
    requestId = details.requestId?.trim() ?? "";
  } else if (isRecord(error)) {
    message = typeof error.message === "string" ? error.message.trim() : "";
    code = typeof error.code === "string" ? error.code.trim() : "";
    requestId =
      typeof error.requestId === "string" ? error.requestId.trim() : "";
  }

  const accountMessage = CUSTOMER_ACCOUNT_ERROR_MESSAGES[code];
  if (accountMessage) {
    return accountMessage;
  }
  if (error instanceof TypeError) {
    return fallback;
  }

  const source = `${code} ${message}`;
  const branded = BRANDED_SERVICE_ERRORS.find(({ pattern }) =>
    pattern.test(source),
  );
  if (!branded) {
    return message || fallback;
  }
  return requestId
    ? `${branded.message} 问题编号：${requestId}`
    : branded.message;
}

async function responseErrorDetails(
  response: Response,
  errorPrefix: string,
): Promise<{ message: string; code?: string; retryable?: boolean }> {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && isRecord(payload.detail)) {
      const message = payload.detail.message;
      const code =
        typeof payload.detail.code === "string"
          ? payload.detail.code
          : undefined;
      const retryable =
        typeof payload.detail.retryable === "boolean"
          ? payload.detail.retryable
          : undefined;
      // The code must survive even when the server omits a message, otherwise
      // callers cannot tell a retryable failure from a permanent one.
      return {
        message: customerVisibleErrorMessage({
          message:
            typeof message === "string" && message.trim()
              ? `${errorPrefix}：${message}（${response.status}）`
              : `${errorPrefix}（${response.status}）`,
          code,
          requestId: response.headers?.get?.("X-Request-Id") ?? "",
        }),
        code,
        retryable,
      };
    }
  } catch {
    // A missing or non-JSON error body must not hide the HTTP status.
  }
  return { message: `${errorPrefix}（${response.status}）` };
}

async function requestAdmin(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  return requestApi(path, init, timeoutMs);
}

async function requestControl(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (
    init.method &&
    ["POST", "PUT", "PATCH", "DELETE"].includes(init.method.toUpperCase()) &&
    !headers.has("X-Admin-CSRF")
  ) {
    const csrf = getAdminCsrfToken();
    if (csrf) {
      headers.set("X-Admin-CSRF", csrf);
    }
  }
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers,
      credentials: "include",
      signal: controller.signal,
    });
    if (response.status === 401) {
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

async function requestApi(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = new Headers(init.headers);
  const devUserId = getDevelopmentUserId();

  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const accessToken = workspaceAccessToken();
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  } else if (devUserId) {
    headers.set("X-Dev-User-Id", devUserId);
  }

  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
    if (response.status === 401 && path !== "/api/auth/me") {
      await emitWorkspaceSessionEnded(response);
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

async function emitWorkspaceSessionEnded(response: Response) {
  if (customerSessionToken === null) {
    emitSessionExpired();
    return;
  }

  let lifecycle = CUSTOMER_SESSION_EXPIRED_EVENT;
  try {
    const requestId = response.headers.get("X-Request-Id") ?? "";
    const error = await customerErrorFromResponse(response.clone(), requestId);
    lifecycle = customerLifecycleEvent(error.kind) ?? lifecycle;
  } catch {
    // A proxy/non-JSON 401 still ends the customer session, but it must never
    // be guessed as a permanent device revocation (which would wipe the
    // long-lived device credential).
  }
  window.dispatchEvent(new Event(lifecycle));
}

function contentTypeForFile(file: File): "video/mp4" | "video/quicktime" {
  return file.name.toLowerCase().endsWith(".mov")
    ? "video/quicktime"
    : "video/mp4";
}

async function sha256ForUpload(file: File): Promise<string | null> {
  try {
    if (!globalThis.crypto?.subtle || typeof file.arrayBuffer !== "function") {
      return null;
    }
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      await file.arrayBuffer(),
    );
    return Array.from(new Uint8Array(digest), (value) =>
      value.toString(16).padStart(2, "0"),
    ).join("");
  } catch {
    // Deduplication is an optimization.  Older WebViews must still be able to
    // upload normally when Web Crypto cannot hash a local File.
    return null;
  }
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
      value.role === "auditor" ||
      value.role === "customer")
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

function isShotMotion(value: unknown): value is ShotMotion {
  return (
    isRecord(value) &&
    typeof value.subject_motion_state === "string" &&
    typeof value.subject_direction === "string" &&
    typeof value.subject_displacement === "string" &&
    typeof value.hand_action === "string" &&
    typeof value.camera_motion === "string" &&
    typeof value.relative_motion === "string"
  );
}

function isShotCard(value: unknown): value is ShotCard {
  if (!isRecord(value)) {
    return false;
  }
  if (
    value.person_count !== undefined &&
    value.person_count !== null &&
    typeof value.person_count !== "number"
  ) {
    return false;
  }
  if (
    value.motion !== undefined &&
    value.motion !== null &&
    !isShotMotion(value.motion)
  ) {
    return false;
  }
  if (
    value.segment_kind !== undefined &&
    value.segment_kind !== null &&
    value.segment_kind !== "SHOT_CUT" &&
    value.segment_kind !== "ACTION_BEAT"
  ) {
    return false;
  }
  if (
    value.boundary_reason !== undefined &&
    value.boundary_reason !== null &&
    typeof value.boundary_reason !== "string"
  ) {
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

// ---------------------------------------------------------------------------
// T28 / FE-01 — customer API lane: the activation / device / session adapter.
//
// Every type below is cut from ./generated/api (the server's OpenAPI
// contract) — hand-written shapes are forbidden here, and the drift guards in
// customerApi.test.ts fail `tsc -b` the moment a regeneration changes a field
// set. Credentials travel as explicit call arguments (dev doc §7: no global
// plaintext variable may simulate a persisted session); the desktop-side
// persistence lands with T29's credential adapter in the Tauri layer.
// ---------------------------------------------------------------------------

type CustomerActivationResponse =
  components["schemas"]["CustomerActivationResponse"];
type CustomerLoginResponse = components["schemas"]["LoginResponse"];
type CustomerHeartbeatResponse = components["schemas"]["HeartbeatResponse"];
export type CustomerDeviceListResponse =
  components["schemas"]["DeviceListResponse"];
type CustomerEnrollPendingResponse =
  components["schemas"]["DeviceEnrollPendingResponse"];
type CustomerEnrollConsumedResponse =
  components["schemas"]["DeviceEnrollConsumedResponse"];
type CustomerPairingApproveResponse =
  components["schemas"]["PairingApproveResponse"];

// Task list §10.1: the single SESSION_EXPIRED_EVENT of the internal lane is
// split into expired / replaced / revoked so the customer workspace can show
// the sentence that matches what actually happened, instead of one generic
// “登录已失效” for every 401.
export const CUSTOMER_SESSION_EXPIRED_EVENT =
  "video-replica:customer-session-expired";
export const CUSTOMER_SESSION_REPLACED_EVENT =
  "video-replica:customer-session-replaced";
export const CUSTOMER_SESSION_REVOKED_EVENT =
  "video-replica:customer-session-revoked";

/** The long-lived device credential returned once at bind time. */
export type CustomerDeviceCredential = { kind: "device"; token: string };
/** The short-lived session token from login / switch. */
export type CustomerSessionCredential = { kind: "session"; token: string };
export type CustomerCredential =
  | CustomerDeviceCredential
  | CustomerSessionCredential;

/** Every 401/403/409/429/idempotency answer resolves to exactly one of
 * these states — the UI never has to parse a raw status line (FE-01). */
export type CustomerApiErrorKind =
  | "session-expired"
  | "session-replaced"
  | "credential-revoked"
  | "credential-invalid"
  | "code-suspended"
  | "code-revoked"
  | "other-device-online"
  | "idempotency-conflict"
  | "rate-limited"
  | "bad-request"
  | "unauthorized"
  | "forbidden"
  | "not-found"
  | "conflict"
  | "service-unavailable"
  | "network"
  | "timeout"
  | "unknown";

export class CustomerApiError extends Error {
  readonly status?: number;
  readonly code?: string;
  readonly retryAfterSeconds?: number;
  /** OTHER_DEVICE_ONLINE extras (dev doc §3.3: masked hint + remaining lease). */
  readonly onlineDeviceNameMasked?: string;
  readonly onlineSlotNo?: number;
  readonly leaseExpiresAt?: string;
  /** The X-Request-Id this client sent (and retains) for the failed request —
   * the correlation key for a server-side audit lookup (dev doc §13.2: an
   * IDEMPOTENCY_CONFLICT must be reported with its request id). */
  readonly requestId?: string;
  /** Transport failures (network / timeout) carry no status or server code —
   * their kind is decided at construction and cannot be derived later. */
  private readonly transportKind?: CustomerApiErrorKind;

  constructor(properties: {
    message: string;
    status?: number;
    code?: string;
    retryAfterSeconds?: number;
    onlineDeviceNameMasked?: string;
    onlineSlotNo?: number;
    leaseExpiresAt?: string;
    requestId?: string;
    transportKind?: CustomerApiErrorKind;
  }) {
    super(properties.message);
    this.name = "CustomerApiError";
    this.status = properties.status;
    this.code = properties.code;
    this.retryAfterSeconds = properties.retryAfterSeconds;
    this.onlineDeviceNameMasked = properties.onlineDeviceNameMasked;
    this.onlineSlotNo = properties.onlineSlotNo;
    this.leaseExpiresAt = properties.leaseExpiresAt;
    this.requestId = properties.requestId;
    this.transportKind = properties.transportKind;
  }

  get kind(): CustomerApiErrorKind {
    return this.transportKind ?? customerErrorKind(this.status, this.code);
  }
}

function customerErrorKind(
  status: number | undefined,
  code: string | undefined,
): CustomerApiErrorKind {
  switch (code) {
    case "SESSION_EXPIRED":
      return "session-expired";
    case "SESSION_REPLACED":
      return "session-replaced";
    case "DEVICE_REVOKED":
      return "credential-revoked";
    case "DEVICE_CREDENTIAL_INVALID":
    case "DEVICE_CREDENTIAL_REQUIRED":
      return "credential-invalid";
    case "CODE_SUSPENDED":
      return "code-suspended";
    case "CODE_REVOKED":
      return "code-revoked";
    case "OTHER_DEVICE_ONLINE":
      return "other-device-online";
    case "IDEMPOTENCY_CONFLICT":
      return "idempotency-conflict";
    case "RATE_LIMITED":
      return "rate-limited";
    case "IDEMPOTENCY_KEY_REQUIRED":
      return "bad-request";
    default:
      break;
  }
  if (status === undefined) {
    return "unknown";
  }
  // No code-suffix matching here: ACTIVATION_UNAVAILABLE / PAIRING_UNAVAILABLE
  // are 400 anti-enumeration rejections (the user must fix the code), not
  // outages. Every true service-interruption code arrives with status 503.
  if (status === 503) {
    return "service-unavailable";
  }
  if (status === 400 || status === 422) {
    return "bad-request";
  }
  if (status === 401) {
    return "unauthorized";
  }
  if (status === 403) {
    return "forbidden";
  }
  if (status === 404) {
    return "not-found";
  }
  if (status === 409) {
    return "conflict";
  }
  return "unknown";
}

/** The lifecycle outcomes that end the customer session for good, mapped to
 * their dedicated events. A mere invalid credential never fires one — that
 * is a caller input problem, not a session the UI must tear down.
 *
 * A suspended code (kind "code-suspended") is deliberately absent: the
 * suspension is reversible (an admin can resume the code), so firing the
 * revoked event would make consumers drop a still-valid device credential —
 * after the resume the user could not log in again without device recovery.
 * The UI surfaces the suspension through the error kind itself (PR #55
 * review); only permanent revocations fire the event. */
function customerLifecycleEvent(
  kind: CustomerApiErrorKind,
): string | undefined {
  if (kind === "session-expired") {
    return CUSTOMER_SESSION_EXPIRED_EVENT;
  }
  if (kind === "session-replaced") {
    return CUSTOMER_SESSION_REPLACED_EVENT;
  }
  if (kind === "credential-revoked" || kind === "code-revoked") {
    return CUSTOMER_SESSION_REVOKED_EVENT;
  }
  return undefined;
}

async function customerErrorFromResponse(
  response: Response,
  requestId: string,
): Promise<CustomerApiError> {
  let code: string | undefined;
  let message = `客户服务请求失败（${response.status}）`;
  let onlineDeviceNameMasked: string | undefined;
  let onlineSlotNo: number | undefined;
  let leaseExpiresAt: string | undefined;
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && isRecord(payload.detail)) {
      const detail = payload.detail;
      if (typeof detail.code === "string") {
        code = detail.code;
      }
      if (typeof detail.message === "string" && detail.message.trim()) {
        message = detail.message;
      }
      if (typeof detail.online_device_name_masked === "string") {
        onlineDeviceNameMasked = detail.online_device_name_masked;
      }
      if (typeof detail.online_slot_no === "number") {
        onlineSlotNo = detail.online_slot_no;
      }
      if (typeof detail.lease_expires_at === "string") {
        leaseExpiresAt = detail.lease_expires_at;
      }
    }
  } catch {
    // A missing or non-JSON error body must not hide the HTTP status.
  }
  const retryAfterHeader = response.headers.get("Retry-After");
  const retryAfterSeconds = retryAfterHeader
    ? Number.parseInt(retryAfterHeader, 10)
    : undefined;
  return new CustomerApiError({
    message: customerVisibleErrorMessage({ message, code, requestId }),
    status: response.status,
    code,
    requestId,
    retryAfterSeconds:
      retryAfterSeconds !== undefined && Number.isNaN(retryAfterSeconds)
        ? undefined
        : retryAfterSeconds,
    onlineDeviceNameMasked,
    onlineSlotNo,
    leaseExpiresAt,
  });
}

type CustomerRequestOptions = {
  method?: string;
  body?: unknown;
  credential?: CustomerCredential;
  idempotencyKey?: string;
  /** Dev doc §13.1: state-changing customer requests carry X-Request-Id.
   * Omit it and the transport mints one (crypto.randomUUID) so every call
   * keeps a correlation key the server audit can be looked up by. */
  requestId?: string;
};

function isReplayed(response: Response): boolean {
  return response.headers.get("X-Idempotent-Replay") === "true";
}

async function requestCustomer(
  path: string,
  options: CustomerRequestOptions,
): Promise<{ response: Response; requestId: string }> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  // Dev doc §13.1: every customer request carries an X-Request-Id. The id
  // travels back inside CustomerApiError so the UI can report it on an
  // IDEMPOTENCY_CONFLICT (§13.2) and the audit trail can be located by it.
  const requestId = options.requestId ?? crypto.randomUUID();
  const headers = new Headers();
  headers.set("X-Request-Id", requestId);
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (options.credential) {
    headers.set("Authorization", `Bearer ${options.credential.token}`);
  }
  if (options.idempotencyKey) {
    headers.set("Idempotency-Key", options.idempotencyKey);
  }
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      method: options.method ?? (options.body !== undefined ? "POST" : "GET"),
      headers,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    return { response, requestId };
  } catch (error) {
    throw customerTransportError(error, requestId);
  } finally {
    window.clearTimeout(timeout);
  }
}

function customerTransportError(
  error: unknown,
  requestId: string,
): CustomerApiError {
  if (error instanceof DOMException && error.name === "AbortError") {
    return new CustomerApiError({
      message: "请求超时，请重试",
      requestId,
      transportKind: "timeout",
    });
  }
  if (error instanceof TypeError) {
    return new CustomerApiError({
      message: "网络连接失败，请检查网络",
      requestId,
      transportKind: "network",
    });
  }
  const message = error instanceof Error ? error.message : "客户服务请求失败";
  return new CustomerApiError({ message, requestId });
}

async function customerJson<T>(
  path: string,
  options: CustomerRequestOptions,
): Promise<{ response: Response; body: T }> {
  const { response, requestId } = await requestCustomer(path, options);
  if (!response.ok) {
    const error = await customerErrorFromResponse(response, requestId);
    const lifecycle = customerLifecycleEvent(error.kind);
    if (lifecycle) {
      window.dispatchEvent(new Event(lifecycle));
    }
    throw error;
  }
  if (response.status === 204) {
    return { response, body: undefined as T };
  }
  return { response, body: (await response.json()) as T };
}

export type CustomerActivateInput = {
  activationCode: string;
  deviceFingerprint: string;
  deviceName: string;
  devicePlatform: string;
  /** Mandatory (dev doc §6.3): activation carries an idempotency key. */
  idempotencyKey: string;
  /** Optional correlation id; omitted, the transport mints one. */
  requestId?: string;
};

/** Redeem an activation code: user + wallet + first device + first charge +
 * first session in one transaction (POST /api/customer/activate). */
export async function customerActivate(
  input: CustomerActivateInput,
): Promise<CustomerActivationResponse> {
  const { body } = await customerJson<CustomerActivationResponse>(
    "/api/customer/activate",
    {
      method: "POST",
      body: {
        activation_code: input.activationCode,
        device_fingerprint: input.deviceFingerprint,
        device_name: input.deviceName,
        device_platform: input.devicePlatform,
      },
      idempotencyKey: input.idempotencyKey,
      requestId: input.requestId,
    },
  );
  return body;
}

export type CustomerRecoverInput = Omit<
  CustomerActivateInput,
  "activationCode"
>;

/** Recover a previously bound desktop from its durable opaque fingerprint.
 * The server only succeeds while the associated activation code, account and
 * device binding are all active; first-time users still need an activation
 * code. The empty code is an explicit wire-level recovery signal. */
export function customerRecover(
  input: CustomerRecoverInput,
): Promise<CustomerActivationResponse> {
  return customerActivate({ ...input, activationCode: "" });
}

export type CustomerLoginResult = {
  /** 201 established / recovered, 200 renewed (the server sets it). */
  status: 200 | 201;
  /** True when the sealed envelope replayed a lost response. */
  replayed: boolean;
  session: CustomerLoginResponse;
};

export type CustomerLoginOptions = {
  idempotencyKey: string;
  /** Presenting the previous session token renews instead of conflicting. */
  sessionToken?: string;
  /** Optional correlation id; omitted, the transport mints one. */
  requestId?: string;
};

async function customerEstablishSession(
  path: string,
  credential: CustomerDeviceCredential,
  options: CustomerLoginOptions,
): Promise<CustomerLoginResult> {
  const { response, body } = await customerJson<CustomerLoginResponse>(path, {
    method: "POST",
    credential,
    body: { session_token: options.sessionToken ?? null },
    idempotencyKey: options.idempotencyKey,
    requestId: options.requestId,
  });
  const status = response.status === 200 ? 200 : 201;
  return { status, replayed: isReplayed(response), session: body };
}

/** Device-credential login (POST /api/customer/sessions/login); 409
 * OTHER_DEVICE_ONLINE carries the masked hint for the conflict dialog. */
export async function customerLogin(
  credential: CustomerDeviceCredential,
  options: CustomerLoginOptions,
): Promise<CustomerLoginResult> {
  return customerEstablishSession(
    "/api/customer/sessions/login",
    credential,
    options,
  );
}

/** The explicit atomic switch (POST /api/customer/sessions/switch): displaces
 * the other device's live lease after the user confirmed the takeover. */
export async function customerSwitch(
  credential: CustomerDeviceCredential,
  options: CustomerLoginOptions,
): Promise<CustomerLoginResult> {
  return customerEstablishSession(
    "/api/customer/sessions/switch",
    credential,
    options,
  );
}

/** Renew the session lease (POST /api/customer/sessions/heartbeat). */
export async function customerHeartbeat(
  credential: CustomerSessionCredential,
): Promise<CustomerHeartbeatResponse> {
  const { body } = await customerJson<CustomerHeartbeatResponse>(
    "/api/customer/sessions/heartbeat",
    { method: "POST", credential },
  );
  return body;
}

/** End the session (POST /api/customer/sessions/logout → 204). */
export async function customerLogout(
  credential: CustomerSessionCredential,
  options: { idempotencyKey: string; requestId?: string },
): Promise<void> {
  await customerJson<undefined>("/api/customer/sessions/logout", {
    method: "POST",
    credential,
    idempotencyKey: options.idempotencyKey,
    requestId: options.requestId,
  });
}

/** The two-slot status view (GET /api/customer/devices). */
export async function customerListDevices(
  credential: CustomerDeviceCredential,
): Promise<CustomerDeviceListResponse> {
  const { body } = await customerJson<CustomerDeviceListResponse>(
    "/api/customer/devices",
    { credential },
  );
  return body;
}

/** Unbind one of the caller's own devices
 * (DELETE /api/customer/devices/{id} → 204). */
export async function customerUnbindDevice(
  credential: CustomerDeviceCredential,
  deviceId: string,
  options: { idempotencyKey: string; requestId?: string },
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/devices/${encodeURIComponent(deviceId)}`,
    {
      method: "DELETE",
      credential,
      idempotencyKey: options.idempotencyKey,
      requestId: options.requestId,
    },
  );
}

export type CustomerEnrollInput = {
  activationCode: string;
  deviceFingerprint: string;
  deviceName: string;
  devicePlatform: string;
  idempotencyKey: string;
  /** Optional correlation id; omitted, the transport mints one. */
  requestId?: string;
};

export type CustomerEnrollResult =
  | { status: 202; replayed: boolean; pending: CustomerEnrollPendingResponse }
  | {
      status: 201;
      replayed: boolean;
      credential: CustomerEnrollConsumedResponse;
    };

/** Start (or finish) the second-device pairing
 * (POST /api/customer/devices/enroll): 202 while waiting for the first
 * device's approval, 201 with the one-time device credential once an approved
 * pairing is consumed. */
export async function customerEnrollDevice(
  input: CustomerEnrollInput,
): Promise<CustomerEnrollResult> {
  const { response, body } = await customerJson<
    CustomerEnrollPendingResponse | CustomerEnrollConsumedResponse
  >("/api/customer/devices/enroll", {
    method: "POST",
    body: {
      activation_code: input.activationCode,
      device_fingerprint: input.deviceFingerprint,
      device_name: input.deviceName,
      device_platform: input.devicePlatform,
    },
    idempotencyKey: input.idempotencyKey,
    requestId: input.requestId,
  });
  const replayed = isReplayed(response);
  if (response.status === 202) {
    return {
      status: 202,
      replayed,
      pending: body as CustomerEnrollPendingResponse,
    };
  }
  return {
    status: 201,
    replayed,
    credential: body as CustomerEnrollConsumedResponse,
  };
}

/** The first bound device approves a PENDING pairing request
 * (POST /api/customer/device-pairings/{id}/approve). */
export async function customerApproveDevicePairing(
  credential: CustomerDeviceCredential,
  pairingId: string,
): Promise<CustomerPairingApproveResponse> {
  const { body } = await customerJson<CustomerPairingApproveResponse>(
    `/api/customer/device-pairings/${encodeURIComponent(pairingId)}/approve`,
    { method: "POST", credential },
  );
  return body;
}

/** Remove an invalid pending/approved pairing request from the customer's
 * active list while the server preserves its audit row. */
export async function customerDismissDevicePairing(
  credential: CustomerDeviceCredential,
  pairingId: string,
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/device-pairings/${encodeURIComponent(pairingId)}`,
    { method: "DELETE", credential },
  );
}

export type CustomerActivationCodeReset =
  components["schemas"]["ActivationCodeResetResponse"];

/** Rotate the active account code. The replacement plaintext is returned
 * once and must never be persisted by the desktop. */
export async function customerResetActivationCode(
  credential: CustomerSessionCredential,
): Promise<CustomerActivationCodeReset> {
  const { body } = await customerJson<CustomerActivationCodeReset>(
    "/api/customer/activation-code/reset",
    { method: "POST", credential },
  );
  return body;
}

/** The customer's wallet balance + billing (GET /api/customer/wallet). */
export async function customerGetWallet(
  credential: CustomerSessionCredential,
): Promise<WalletSnapshot> {
  const { body } = await customerJson<OpenApiWalletSnapshot>(
    "/api/customer/wallet",
    { credential },
  );
  return requireWalletPricing(body);
}

function requireWalletPricing(wallet: OpenApiWalletSnapshot): WalletSnapshot {
  if (
    typeof wallet.internal_unit_price_fen !== "number" ||
    wallet.internal_unit_price_fen <= 0 ||
    typeof wallet.min_recharge_fen !== "number" ||
    wallet.min_recharge_fen <= 0 ||
    typeof wallet.recharge_step_fen !== "number" ||
    wallet.recharge_step_fen <= 0
  ) {
    throw new Error("钱包定价配置不完整，请联系管理员");
  }
  return wallet as WalletSnapshot;
}

export type CustomerProfile = components["schemas"]["CustomerProfileResponse"];

export async function customerGetProfile(
  credential: CustomerSessionCredential,
): Promise<CustomerProfile> {
  const { body } = await customerJson<CustomerProfile>(
    "/api/customer/profile",
    { credential },
  );
  return body;
}

export async function customerUpdateProfile(
  credential: CustomerSessionCredential,
  displayName: string,
): Promise<CustomerProfile> {
  const { body } = await customerJson<CustomerProfile>(
    "/api/customer/profile",
    {
      method: "PATCH",
      credential,
      body: { display_name: displayName },
    },
  );
  return body;
}

/** The customer's wallet transaction ledger (GET /api/customer/wallet/transactions). */
export async function customerListWalletTransactions(
  credential: CustomerSessionCredential,
): Promise<WalletTransactionPage> {
  const { body } = await customerJson<WalletTransactionPage>(
    "/api/customer/wallet/transactions",
    { credential },
  );
  return body;
}

/** The customer's own recharge orders (GET /api/customer/recharge-orders). */
export async function customerListRechargeOrders(
  credential: CustomerSessionCredential,
): Promise<RechargeOrderPage> {
  const { body } = await customerJson<RechargeOrderPage>(
    "/api/customer/recharge-orders",
    { credential },
  );
  return body;
}

/** Create a customer recharge order
 * (POST /api/customer/recharge-orders → 201 PENDING + ZPay payment form). */
export async function customerCreateRechargeOrder(
  credential: CustomerSessionCredential,
  amountFen: number,
  options: { idempotencyKey: string; requestId?: string },
): Promise<CreatedRechargeOrder> {
  const { body } = await customerJson<CreatedRechargeOrder>(
    "/api/customer/recharge-orders",
    {
      method: "POST",
      credential,
      body: { amount_fen: amountFen },
      idempotencyKey: options.idempotencyKey,
      requestId: options.requestId,
    },
  );
  return body;
}

export type CustomerPaymentCode =
  components["schemas"]["CustomerPaymentCodeResponse"];

/** Generate a display-only QR code for an owned pending recharge order.
 * Merchant credentials and signed protocol fields remain on the server. */
export async function customerCreateRechargePaymentCode(
  credential: CustomerSessionCredential,
  orderNo: string,
): Promise<CustomerPaymentCode> {
  const { body } = await customerJson<CustomerPaymentCode>(
    `/api/customer/recharge-orders/${encodeURIComponent(orderNo)}/payment-code`,
    { method: "POST", credential },
  );
  return body;
}

/** Poll a customer recharge order
 * (GET /api/customer/recharge-orders/{order_no}). */
export async function customerGetRechargeOrder(
  credential: CustomerSessionCredential,
  orderNo: string,
): Promise<RechargeOrder> {
  const { body } = await customerJson<RechargeOrder>(
    `/api/customer/recharge-orders/${encodeURIComponent(orderNo)}`,
    { credential },
  );
  return body;
}

/** Close an unpaid recharge order. The server keeps the CLOSED row for
 * callback reconciliation and audit instead of physically deleting it. */
export async function customerCloseRechargeOrder(
  credential: CustomerSessionCredential,
  orderNo: string,
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/recharge-orders/${encodeURIComponent(orderNo)}`,
    { method: "DELETE", credential },
  );
}
