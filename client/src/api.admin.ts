// T32 — admin activation frontend adapter.
//
// The customer V3 control plane lives behind the T09 admin session
// (HttpOnly `admin_session` cookie on /api/control) plus the per-session CSRF
// token returned by the login/session response. The CSRF token is held in a
// module-level variable only: it never reaches localStorage, sessionStorage,
// or any other browser persistence. The server can deterministically restore
// it from the HttpOnly session cookie after a page refresh.
//
// Every write goes out with the dev-doc §15 admin write contract:
// `confirm: true`, a non-blank `reason`, and a unique `Idempotency-Key`
// header, alongside the `X-Admin-CSRF` header; responses carry the audit
// `request_id` that the pages surface to the operator.

import {
  clearAdminCsrfToken,
  getAdminCsrfToken,
  resolveApiBaseUrl,
  setAdminCsrfToken,
} from "./api";
import type { components } from "./generated/api";

const DEFAULT_TIMEOUT_MS = 5_000;
const CSRF_HEADER = "X-Admin-CSRF";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";

// A8（2026-09-02 评估）：管理端错误类统一为一个实现。历史上五个域各复制
// 了同一个类（AdminCustomerError / AdminDeviceError / AdminAdjustmentError /
// AdminSessionError / AdminAuditError），语义完全相同；现在它们都是
// AdminControlError 的别名，`instanceof` 在所有调用点继续成立。
export class AdminControlError extends Error {
  readonly status: number | undefined;
  readonly code: string | undefined;

  constructor(message: string, status?: number, code?: string) {
    super(message);
    this.name = "AdminControlError";
    this.status = status;
    this.code = code;
  }
}

export class AdminActivationError extends AdminControlError {
  constructor(message: string, status?: number, code?: string) {
    super(message, status, code);
    this.name = "AdminActivationError";
  }
}

/** Drop the in-memory session state (logout, expiry, tests). */
export function clearAdminActivationSession(): void {
  clearAdminCsrfToken();
}

function requireCsrfToken(): string {
  const token = getAdminCsrfToken();
  if (!token) {
    throw new AdminActivationError(
      "管理登录令牌缺失，请重新登录后再执行写操作",
      401,
      "ADMIN_CSRF_UNAVAILABLE",
    );
  }
  return token;
}

function newIdempotencyKey(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
    return cryptoRef.randomUUID();
  }
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

/**
 * Mint an idempotency key for one logical submission.
 *
 * The server (T12) keeps a replay snapshot keyed by (actor, route, key digest)
 * so that a retry after an ambiguous failure (timeout / network error) replays
 * the original response instead of creating a second batch. Pages therefore
 * mint a key on the first submit attempt and reuse it for retries of that
 * same submission; a fresh key is minted only after the previous submission
 * reached a definitive outcome.
 */
export function createIdempotencyKey(): string {
  return newIdempotencyKey();
}

function apiBaseUrl(): string {
  return resolveApiBaseUrl(
    import.meta.env.VITE_API_BASE_URL,
    import.meta.env.PROD,
    window.location,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function parseActivationError(
  response: Response,
  fallback: string,
): Promise<AdminActivationError> {
  let code: string | undefined;
  let message: string | undefined;
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && isRecord(payload.detail)) {
      code =
        typeof payload.detail.code === "string"
          ? payload.detail.code
          : undefined;
      message =
        typeof payload.detail.message === "string"
          ? payload.detail.message
          : undefined;
    }
  } catch {
    // A non-JSON body must not hide the HTTP status.
  }
  const text = message?.trim()
    ? `${fallback}：${message}（${response.status}）`
    : `${fallback}（${response.status}）`;
  return new AdminActivationError(text, response.status, code);
}

async function requestControl(
  path: string,
  init: RequestInit & { headers?: Record<string, string> },
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const headers: Record<string, string> = { ...(init.headers ?? {}) };
  if (init.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  try {
    return await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers,
      credentials: "include",
      signal: controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new AdminActivationError("请求超时，请重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function adminWrite<T>(
  path: string,
  fields: Record<string, unknown>,
  reason: string,
  fallback: string,
  idempotencyKey?: string,
  method: "POST" | "PATCH" | "PUT" = "POST",
): Promise<T> {
  const csrf = requireCsrfToken();
  const response = await requestControl(path, {
    method,
    headers: {
      [CSRF_HEADER]: csrf,
      [IDEMPOTENCY_KEY_HEADER]: idempotencyKey ?? newIdempotencyKey(),
    },
    body: JSON.stringify({ ...fields, confirm: true, reason }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, fallback);
  }
  return (await response.json()) as T;
}

export async function adminRead<T>(path: string, fallback: string): Promise<T> {
  const response = await requestControl(path, { method: "GET" });
  if (!response.ok) {
    throw await parseActivationError(response, fallback);
  }
  return response.json() as Promise<T>;
}

export interface AdminRechargeOrder {
  id: string;
  user_id: string;
  username: string;
  display_name?: string;
  order_no: string;
  status: "PENDING" | "PAID" | "FAILED" | "CLOSED";
  amount_fen: number;
  credits: number;
  channel: string;
  provider_trade_no: string | null;
  created_at: string;
  paid_at: string | null;
}

export interface AdminWalletTransaction {
  id: string;
  user_id: string;
  username: string;
  type: "CHARGE" | "RESERVE" | "SETTLE" | "RELEASE";
  available_delta: number;
  reserved_delta: number;
  available_balance_after: number | null;
  reserved_balance_after: number | null;
  recharge_order_id: string | null;
  task_id: string | null;
  billing_round: number | null;
  created_at: string;
}

interface AdminListPage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export async function listAdminRechargeOrders(
  options: {
    status?: string;
    userId?: string;
    username?: string;
    channel?: string;
    createdFrom?: string;
    createdTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<AdminListPage<AdminRechargeOrder>> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.status) params.set("status", options.status);
  if (options.userId) params.set("user_id", options.userId);
  if (options.username) params.set("username", options.username);
  if (options.channel) params.set("channel", options.channel);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  return adminRead(
    `/api/control/recharge-orders?${params}`,
    "读取充值订单失败",
  );
}

export async function listAdminWalletTransactions(
  options: {
    userId?: string;
    username?: string;
    type?: string;
    createdFrom?: string;
    createdTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<AdminListPage<AdminWalletTransaction>> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.userId) params.set("user_id", options.userId);
  if (options.username) params.set("username", options.username);
  if (options.type) params.set("type", options.type);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  return adminRead(
    `/api/control/wallet-transactions?${params}`,
    "读取额度流水失败",
  );
}

// ---------------------------------------------------------------------------
// Session (T09)
// ---------------------------------------------------------------------------

export type AdminActorInfo = {
  user_id: string;
  username: string;
  display_name: string;
  role: string;
};

export type AdminExchangeResult = {
  session_id: string;
  expires_at: string;
  csrf_token: string;
  actor: AdminActorInfo;
};

export type AdminSessionInfo = {
  session_id: string;
  expires_at: string;
  last_activity_at: string;
  csrf_token: string | null;
  actor: AdminActorInfo;
};

export async function loginAdminWithPassword(
  username: string,
  password: string,
): Promise<AdminExchangeResult> {
  const response = await requestControl("/api/control/admin/session/password", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, "后台登录失败");
  }
  const payload = (await response.json()) as AdminExchangeResult;
  setAdminCsrfToken(payload.csrf_token);
  return payload;
}

export async function exchangeAdminSession(
  credential: string,
): Promise<AdminExchangeResult> {
  const response = await requestControl("/api/control/admin/session/exchange", {
    method: "POST",
    body: JSON.stringify({ credential }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, "管理登录失败");
  }
  const payload = (await response.json()) as AdminExchangeResult;
  // Memory only — never persisted (No-Go red line).
  setAdminCsrfToken(payload.csrf_token);
  return payload;
}

export async function fetchAdminSession(): Promise<AdminSessionInfo> {
  const response = await requestControl("/api/control/admin/session", {
    method: "GET",
  });
  if (!response.ok) {
    throw await parseActivationError(response, "读取管理会话失败");
  }
  const payload = (await response.json()) as AdminSessionInfo;
  if (payload.csrf_token) {
    setAdminCsrfToken(payload.csrf_token);
  } else {
    clearAdminCsrfToken();
  }
  return payload;
}

export async function recoverAdminPassword(password: string): Promise<void> {
  const csrf = requireCsrfToken();
  const response = await requestControl("/api/control/admin/password", {
    method: "PUT",
    headers: { [CSRF_HEADER]: csrf },
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, "设置管理员密码失败");
  }
  clearAdminCsrfToken();
}

export async function deleteAdminSession(): Promise<void> {
  const csrf = requireCsrfToken();
  const response = await requestControl("/api/control/admin/session", {
    method: "DELETE",
    headers: { [CSRF_HEADER]: csrf },
  });
  if (!response.ok) {
    throw await parseActivationError(response, "退出管理登录失败");
  }
  clearAdminCsrfToken();
}

// ---------------------------------------------------------------------------
// Activation codes (T12)
// ---------------------------------------------------------------------------

export type ActivationCodeListItem = {
  code_id: string;
  batch_id: string;
  masked_code: string;
  status: string;
  bound_user_id: string | null;
  bound_username: string | null;
  issued_at: string | null;
  expires_at?: string;
  created_at?: string;
  archived_at: string | null;
  devices: ActivationCodeDevice[];
  pending_pairings: ActivationCodePendingPairing[];
};

export type ActivationCodePendingPairing = {
  pairing_request_id: string;
  display_name: string;
  platform: string;
  status: "PENDING" | "APPROVED";
  created_at: string;
  expires_at: string;
};

export type ActivationCodeDevice = {
  device_id: string;
  slot_no: number;
  display_name: string | null;
  platform: string;
  status: string;
  bound_at: string | null;
  last_active_at: string | null;
  unbound_at: string | null;
  revoked_at: string | null;
};

export type ActivationCodePage = {
  items: ActivationCodeListItem[];
  total: number;
  limit: number;
  offset: number;
};

export type ActivationBatchResult = {
  batch_id: string;
  name: string;
  face_value_fen: number;
  unit_price_fen_snapshot: number;
  credits_snapshot: number;
  quantity: number;
  activation_expires_at: string;
  status: string;
  created_by_user_id: string;
  request_id: string;
};

export type ActivationGenerateResult = {
  batch_id: string;
  export_id: string;
  expires_at: string;
  codes: Array<{ code_id: string; masked_code: string }>;
  request_id: string;
};

export type ActivationDownloadResult = {
  export_id: string;
  batch_id: string;
  codes: string[];
  downloaded_at: string;
  request_id: string;
};

export type ActivationDeliverResult = {
  code_id: string;
  status: string;
  delivery_id: string;
  request_id: string;
};

export type ActivationCodeMutationResult = {
  code_id: string;
  status: string;
  request_id: string;
};

export type ActivationCodeArchiveResult = {
  code_id: string;
  archived_at: string;
  request_id: string;
};

export type PairingAdminResult = {
  pairing_id: string;
  status: string;
  outcome?: string;
  replaced_device_id?: string;
  request_id: string;
};

export type ActivationCodeRevealResult = {
  code_id: string;
  activation_code: string;
  masked_code: string;
  request_id: string;
};

export async function createActivationCodeBatch(
  input: {
    name: string;
    face_value_fen: number;
    credits: number;
    quantity: number;
    activation_expires_at: string;
    confirm_grant?: boolean;
    reason: string;
  },
  idempotencyKey?: string,
): Promise<ActivationBatchResult> {
  return adminWrite<ActivationBatchResult>(
    "/api/control/activation-code-batches",
    {
      name: input.name,
      face_value_fen: input.face_value_fen,
      credits: input.credits,
      quantity: input.quantity,
      activation_expires_at: input.activation_expires_at,
      confirm_grant: input.confirm_grant ?? false,
    },
    input.reason,
    "创建激活码批次失败",
    idempotencyKey,
  );
}

export async function generateActivationCodes(
  batchId: string,
  quantity: number,
  reason: string,
  idempotencyKey?: string,
  autoIssue = false,
): Promise<ActivationGenerateResult> {
  return adminWrite<ActivationGenerateResult>(
    `/api/control/activation-code-batches/${encodeURIComponent(batchId)}/generate`,
    autoIssue ? { quantity, auto_issue: true } : { quantity },
    reason,
    "生成激活码失败",
    idempotencyKey,
  );
}

export async function downloadActivationCodeExport(
  exportId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationDownloadResult> {
  return adminWrite<ActivationDownloadResult>(
    `/api/control/activation-code-exports/${encodeURIComponent(exportId)}/download`,
    {},
    reason,
    "下载明文码失败",
    idempotencyKey,
  );
}

export async function listActivationCodes({
  batch_id,
  status,
  search,
  include_archived,
  limit = 50,
  offset = 0,
}: {
  batch_id?: string;
  status?: string;
  /** 服务端搜索：匹配掩码码或绑定用户名（A9）。 */
  search?: string;
  /** C7：true 时回看已归档码（归档只隐藏，不删史）。 */
  include_archived?: boolean;
  limit?: number;
  offset?: number;
} = {}): Promise<ActivationCodePage> {
  const query = new URLSearchParams();
  if (batch_id) {
    query.set("batch_id", batch_id);
  }
  if (status) {
    query.set("status", status);
  }
  if (search) {
    query.set("search", search);
  }
  if (include_archived) {
    query.set("include_archived", "true");
  }
  query.set("limit", String(limit));
  query.set("offset", String(offset));
  const response = await requestControl(
    `/api/control/activation-codes?${query}`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取激活码列表失败");
  }
  return (await response.json()) as ActivationCodePage;
}

export async function revealActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeRevealResult> {
  return adminWrite<ActivationCodeRevealResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/reveal`,
    {},
    reason,
    "读取激活码失败",
    idempotencyKey,
  );
}

export async function deliverActivationCode(
  codeId: string,
  input: {
    channel: string;
    external_order_ref?: string;
    recipient_ref?: string;
    reason: string;
  },
  idempotencyKey?: string,
): Promise<ActivationDeliverResult> {
  return adminWrite<ActivationDeliverResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/deliver`,
    {
      channel: input.channel,
      external_order_ref: input.external_order_ref || undefined,
      recipient_ref: input.recipient_ref || undefined,
    },
    input.reason,
    "发放激活码失败",
    idempotencyKey,
  );
}

export async function suspendActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeMutationResult> {
  return adminWrite<ActivationCodeMutationResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/suspend`,
    {},
    reason,
    "暂停激活码失败",
    idempotencyKey,
  );
}

export async function resumeActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeMutationResult> {
  return adminWrite<ActivationCodeMutationResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/resume`,
    {},
    reason,
    "恢复激活码失败",
    idempotencyKey,
  );
}

export async function revokeActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeMutationResult> {
  return adminWrite<ActivationCodeMutationResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/revoke`,
    {},
    reason,
    "作废激活码失败",
    idempotencyKey,
  );
}

export async function archiveActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeArchiveResult> {
  return adminWrite<ActivationCodeArchiveResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/archive`,
    {},
    reason,
    "删除激活码失败",
    idempotencyKey,
  );
}

export async function approveDevicePairing(
  pairingId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<PairingAdminResult> {
  return adminWrite<PairingAdminResult>(
    `/api/control/device-pairings/${encodeURIComponent(pairingId)}/approve`,
    {},
    reason,
    "批准设备配对失败",
    idempotencyKey,
  );
}

export async function replaceDeviceForPairing(
  pairingId: string,
  replaceDeviceId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<PairingAdminResult> {
  return adminWrite<PairingAdminResult>(
    `/api/control/device-pairings/${encodeURIComponent(pairingId)}/replace-device`,
    { replace_device_id: replaceDeviceId },
    reason,
    "更换设备失败",
    idempotencyKey,
  );
}

// ---------------------------------------------------------------------------
// Deterministic operator-facing messages
// ---------------------------------------------------------------------------

const CODE_MESSAGES: Record<string, string> = {
  EXCHANGE_CREDENTIAL_INVALID: "交换凭据无效或已过期，请重新获取",
  EXCHANGE_CREDENTIAL_REUSED: "交换凭据已被使用，请重新获取",
  ADMIN_ACTOR_INVALID: "操作员账号不可用",
  ADMIN_ROLE_REQUIRED: "仅管理员或审计员可登录管理端",
  ADMIN_LOGIN_INVALID: "管理员账号或密码错误",
  ADMIN_PASSWORD_INVALID: "密码需为 12 至 128 个字符",
  ADMIN_PASSWORD_RECOVERY_REQUIRED: "请先使用一次性恢复凭据验证身份",
  ADMIN_SESSION_INVALID: "会话已失效，请重新登录",
  ADMIN_SESSION_EXPIRED: "会话已过期，请重新登录",
  ADMIN_SESSIONS_UNAVAILABLE: "管理会话服务暂不可用，请稍后重试",
  ADMIN_CSRF_REQUIRED: "缺少 CSRF 令牌，请重新登录",
  ADMIN_CSRF_INVALID: "CSRF 令牌不匹配，请重新登录",
  ADMIN_CSRF_UNAVAILABLE: "管理登录令牌缺失，请重新登录后再执行写操作",
  AUDITOR_READ_ONLY: "审计员只读，无法执行写操作",
  IDEMPOTENCY_KEY_REQUIRED: "缺少幂等键，请重试",
  IDEMPOTENCY_CONFLICT: "幂等键冲突：该键已被其他请求使用",
  CONFIRMATION_REQUIRED: "服务端要求显式确认，请勾选确认后重试",
  REASON_REQUIRED: "服务端要求填写操作原因，请补充后重试",
  BATCH_VALIDATION_FAILED: "批次参数校验失败，请检查后重试",
  BATCH_NOT_FOUND: "批次不存在",
  BATCH_NOT_OPEN: "批次已关闭，无法生成激活码",
  BATCH_BUDGET_EXCEEDED: "生成数量超出批次预算",
  ACTIVATION_KEYS_UNAVAILABLE: "激活码密钥未配置，请联系运维",
  ACTIVATION_DETAILS_UNAVAILABLE: "激活码详情暂时无法读取，请稍后重试",
  ACTIVATION_SERVICE_UNAVAILABLE: "激活码服务暂不可用，请稍后重试",
  EXPORT_NOT_FOUND: "导出包不存在",
  EXPORT_ALREADY_DOWNLOADED: "该导出包已被下载过，无法再次下载",
  EXPORT_EXPIRED: "该导出包已过期，请重新生成",
  CODE_NOT_FOUND: "激活码不存在",
  CODE_TRANSITION_INVALID: "当前状态不允许该操作",
  CODE_NOT_ACTIVATED: "仅已激活的激活码可以恢复",
  DELIVERY_VALIDATION_FAILED: "发放参数校验失败，请检查后重试",
};

export function adminActivationErrorMessage(
  cause: unknown,
  fallback: string,
  overrides: Record<string, string> = {},
): string {
  if (cause instanceof AdminActivationError) {
    const code = cause.code ?? "";
    const mapped = overrides[code] ?? CODE_MESSAGES[code];
    if (mapped) {
      return mapped;
    }
    return cause.message.trim() || fallback;
  }
  if (cause instanceof Error && cause.message.trim()) {
    return cause.message;
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// T33 — Customer management APIs (ADM-02)
// ---------------------------------------------------------------------------

export const AdminCustomerError = AdminControlError;

export interface CustomerListItem {
  user_id: string;
  username: string;
  display_name?: string;
  created_at: string;
  activation_code_id?: string;
  activation_code: string;
  status: string;
  available_credits?: number;
  reserved_credits?: number;
  device_slots_used?: number;
  device_slots_total?: number;
  generation_total?: number;
  generation_succeeded?: number;
  generation_failed?: number;
  generation_in_progress?: number;
  generation_attention?: number;
  credits_spent?: number;
}

export interface CustomerListResponse {
  items: CustomerListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CustomerListOptions {
  limit?: number;
  offset?: number;
  username_filter?: string;
  status?: string;
  createdFrom?: string;
  createdTo?: string;
  balanceMin?: number;
  balanceMax?: number;
}

/**
 * Fetch the customer list with the management-wide limit/offset contract (A5).
 *
 * GET /api/control/customers?limit=&offset=&username=
 */
export async function listCustomers(
  options: CustomerListOptions = {},
): Promise<CustomerListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));
  if (options.username_filter) params.set("username", options.username_filter);
  if (options.status) params.set("status", options.status);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  if (options.balanceMin !== undefined)
    params.set("balance_min", String(options.balanceMin));
  if (options.balanceMax !== undefined)
    params.set("balance_max", String(options.balanceMax));

  const response = await requestControl(
    `/api/control/customers?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取客户列表失败");
  }

  return response.json() as Promise<CustomerListResponse>;
}

export interface CustomerUnitPrice {
  user_id: string;
  unit_price_fen: number;
  custom_unit_price_fen: number | null;
  default_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
  updated_at: string | null;
  request_id: string | null;
}

/** Read the effective sale price for one activated customer. */
export async function fetchCustomerUnitPrice(
  userId: string,
): Promise<CustomerUnitPrice> {
  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/unit-price`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取客户单价失败");
  }
  return response.json() as Promise<CustomerUnitPrice>;
}

/**
 * Set a customer's sale price in fen, or pass null to restore the global
 * default. The internal cost/base price is deliberately not consulted.
 */
export async function updateCustomerUnitPrice(
  userId: string,
  unitPriceFen: number | null,
  reason: string,
  idempotencyKey: string = newIdempotencyKey(),
): Promise<CustomerUnitPrice> {
  const csrf = requireCsrfToken();
  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/unit-price`,
    {
      method: "PUT",
      headers: {
        [CSRF_HEADER]: csrf,
        [IDEMPOTENCY_KEY_HEADER]: idempotencyKey,
      },
      body: JSON.stringify({
        confirm: true,
        reason,
        unit_price_fen: unitPriceFen,
      }),
    },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "保存客户单价失败");
  }
  return response.json() as Promise<CustomerUnitPrice>;
}

// ---------------------------------------------------------------------------
// T33 — Device management APIs (ADM-02)
// ---------------------------------------------------------------------------

export const AdminDeviceError = AdminControlError;

export interface DeviceListItem {
  device_id: string;
  activation_code_id: string;
  user_id: string;
  slot_no: number;
  display_name: string | null;
  platform: string;
  status: string;
  bound_at: string | null;
  unbound_at: string | null;
  revoked_at: string | null;
  username?: string;
  activation_code?: string;
  last_heartbeat_at?: string | null;
  online?: boolean;
}

export interface DeviceSummary {
  bound: number;
  online: number;
  revoked_today: number;
  unbound: number;
}

export interface DeviceListResponse {
  items: DeviceListItem[];
  total: number;
  limit: number;
  offset: number;
  summary?: DeviceSummary;
}

export interface DeviceListOptions {
  status?: string;
  platform?: string;
  userId?: string;
  limit?: number;
  offset?: number;
}

/**
 * Fetch the device list with offset-based pagination.
 *
 * GET /api/control/devices?status=&limit=&offset=
 */
export async function listDevices(
  options: DeviceListOptions = {},
): Promise<DeviceListResponse> {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.platform) params.set("platform", options.platform);
  if (options.userId) params.set("user_id", options.userId);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/devices?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取设备列表失败");
  }

  return response.json() as Promise<DeviceListResponse>;
}

export interface DeviceOperationResult {
  device_id: string;
  status: "UNBOUND" | "REVOKED";
  outcome: string;
  request_id: string;
}

export async function unbindDevice(
  deviceId: string,
  reason: string,
): Promise<DeviceOperationResult> {
  return adminWrite<DeviceOperationResult>(
    `/api/control/devices/${encodeURIComponent(deviceId)}/unbind`,
    {},
    reason,
    "设备下线失败",
  );
}

export async function revokeDeviceCredential(
  deviceId: string,
  reason: string,
): Promise<DeviceOperationResult> {
  return adminWrite<DeviceOperationResult>(
    `/api/control/devices/${encodeURIComponent(deviceId)}/revoke-credential`,
    {},
    reason,
    "撤销设备凭据失败",
  );
}

// ---------------------------------------------------------------------------
// T33 — Adjustment history APIs (ADM-02)
// ---------------------------------------------------------------------------

export const AdminAdjustmentError = AdminControlError;

export interface AdjustmentListItem {
  adjustment_id: string;
  order_id: string;
  admin_user_id: string;
  source_document_type: string;
  source_document_ref: string;
  reason: string;
  request_id: string;
  created_at: string;
  amount_fen: number;
  credits: number;
  pricing_scope: string;
  status: string;
  admin_username?: string;
  target_user_id?: string;
  target_username?: string;
  balance_before?: number | null;
  balance_after?: number | null;
}

export interface GlobalAdjustmentListOptions extends AdjustmentListOptions {
  actorUsername?: string;
  targetUsername?: string;
  sourceDocumentType?: string;
  createdFrom?: string;
  createdTo?: string;
}

export async function listAllAdminAdjustments(
  options: GlobalAdjustmentListOptions = {},
): Promise<AdjustmentListResponse> {
  const params = new URLSearchParams();
  if (options.actorUsername)
    params.set("actor_username", options.actorUsername);
  if (options.targetUsername)
    params.set("target_username", options.targetUsername);
  if (options.sourceDocumentType)
    params.set("source_document_type", options.sourceDocumentType);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));
  return adminRead<AdjustmentListResponse>(
    `/api/control/adjustments?${params.toString()}`,
    "读取调账记录失败",
  );
}

export interface AdjustmentListResponse {
  items: AdjustmentListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdjustmentListOptions {
  limit?: number;
  offset?: number;
  sort?: "asc" | "desc";
}

/**
 * Fetch the adjustment history for a specific user with offset-based pagination.
 *
 * GET /api/control/customers/{user_id}/adjustments?limit=&offset=
 */
export async function listAdminAdjustments(
  userId: string,
  options: AdjustmentListOptions = {},
): Promise<AdjustmentListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));
  if (options.sort !== undefined) params.set("sort", options.sort);

  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/adjustments?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    let detail = "读取调账历史失败";
    try {
      const body: unknown = await response.json();
      if (typeof body === "object" && body !== null && !Array.isArray(body)) {
        const d = (body as Record<string, unknown>).detail;
        if (typeof d === "object" && d !== null) {
          const msg = (d as Record<string, unknown>).message;
          if (typeof msg === "string" && msg.trim()) {
            detail = `读取调账历史失败：${msg}（${response.status}）`;
          }
        }
      }
    } catch {
      /* non-JSON body */
    }
    throw new AdminAdjustmentError(detail, response.status);
  }

  return response.json() as Promise<AdjustmentListResponse>;
}

// ---------------------------------------------------------------------------
// T34 — Customer session API (ADM-02)
// ---------------------------------------------------------------------------

export const AdminSessionError = AdminControlError;

export interface CustomerSessionListItem {
  session_id: string;
  user_id: string;
  username: string;
  device_id: string;
  session_epoch: number;
  lease_until: string;
  last_heartbeat_at: string;
  created_at: string;
  updated_at: string;
  device_name: string;
  platform: string;
  slot_no: number;
  device_status: string;
}

export interface CustomerSessionListResponse {
  items: CustomerSessionListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CustomerSessionListOptions {
  status?: string;
  limit?: number;
  offset?: number;
}

/**
 * Overview of every currently live customer session (A11).
 *
 * GET /api/control/customer-sessions/live?limit=&offset=
 */
export async function listLiveSessions(
  options: { limit?: number; offset?: number } = {},
): Promise<CustomerSessionListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/customer-sessions/live?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取在线会话失败");
  }

  return response.json() as Promise<CustomerSessionListResponse>;
}

/**
 * Fetch the live customer session state (the 029 one-row-per-user model).
 *
 * GET /api/control/customers/{user_id}/sessions?status=&limit=&offset=
 */
export async function listCustomerSessions(
  userId: string,
  options: CustomerSessionListOptions = {},
): Promise<CustomerSessionListResponse> {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/sessions?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取客户会话失败");
  }

  return response.json() as Promise<CustomerSessionListResponse>;
}

export async function revokeCustomerSession(
  sessionId: string,
  sessionEpoch: number,
  reason: string,
  idempotencyKey: string,
): Promise<{ request_id: string }> {
  const response = await requestControl(
    `/api/control/customer-sessions/${encodeURIComponent(sessionId)}/revoke`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        confirm: true,
        reason,
        session_epoch: sessionEpoch,
      }),
    },
  );
  if (!response.ok) throw await parseActivationError(response, "结束会话失败");
  return response.json() as Promise<{ request_id: string }>;
}

// ---------------------------------------------------------------------------
// T34 — Audit log API (ADM-02)
// ---------------------------------------------------------------------------

export const AdminAuditError = AdminControlError;

export interface AuditLogItem {
  event_id: string;
  event_type: string;
  actor_user_id: string;
  actor_username: string;
  target_user_id: string;
  target_username?: string;
  source_document_type: string;
  source_document_ref: string;
  reason: string;
  request_id: string;
  created_at: string;
  change_subject?: string | null;
  old_unit_price_fen?: number | null;
  new_unit_price_fen?: number | null;
}

export interface AuditLogResponse {
  items: AuditLogItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditLogOptions {
  eventType?: string;
  actorUserId?: string;
  targetUserId?: string;
  actorUsername?: string;
  targetUsername?: string;
  createdFrom?: string;
  createdTo?: string;
  limit?: number;
  offset?: number;
}

/**
 * Fetch the unified audit trail (A10: five audited surfaces UNIONed) with
 * pagination and combined filters.
 *
 * GET /api/control/audit-log?event_type=&actor_user_id=&target_user_id=
 *   &created_from=&created_to=&limit=&offset=
 */
export async function listAuditLog(
  options: AuditLogOptions = {},
): Promise<AuditLogResponse> {
  const params = new URLSearchParams();
  if (options.eventType) params.set("event_type", options.eventType);
  if (options.actorUserId) params.set("actor_user_id", options.actorUserId);
  if (options.targetUserId) params.set("target_user_id", options.targetUserId);
  if (options.actorUsername)
    params.set("actor_username", options.actorUsername);
  if (options.targetUsername)
    params.set("target_username", options.targetUsername);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/audit-log?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取审计日志失败");
  }

  return response.json() as Promise<AuditLogResponse>;
}

// ---------------------------------------------------------------------------
// Generation records — paid media and AI-operation traceability
// ---------------------------------------------------------------------------

export type AdminGenerationRecord =
  components["schemas"]["ControlGenerationRecord"];
export type AdminGenerationRecordPage =
  components["schemas"]["ControlGenerationRecordPage"];

export async function getAdminGenerationRecords(
  options: {
    limit?: number;
    offset?: number;
    username?: string;
    status?: string;
    recordType?: string;
    createdFrom?: string;
    createdTo?: string;
  } = {},
): Promise<AdminGenerationRecordPage> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.username) params.set("username", options.username);
  if (options.status) params.set("status", options.status);
  if (options.recordType) params.set("record_type", options.recordType);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  const response = await requestControl(
    `/api/control/generation-records?${params.toString()}`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取生成记录失败");
  }
  return response.json() as Promise<AdminGenerationRecordPage>;
}

// ---------------------------------------------------------------------------
// T23 — Admin adjustment write (ADM-02)
// ---------------------------------------------------------------------------

export interface AdjustmentWriteInput {
  sourceDocumentType: string;
  sourceDocumentRef: string;
  credits: number;
}

export interface AdjustmentWriteResult {
  adjustment_id: string;
  order_id: string;
  credits: string;
  amount_fen: string;
  pricing_scope: string;
  wallet_balance_after: number;
  source_document_type: string;
  source_document_ref: string;
  request_id: string;
}

/**
 * Create an admin adjustment: PAID order + CHARGE + wallet + audit row.
 * The write contract (confirm + reason + idempotency key) rides the shared
 * adminWrite path, and the key is preserved across ambiguous retries.
 *
 * POST /api/control/customers/{user_id}/adjustments
 */
export async function createCustomerAdjustment(
  userId: string,
  input: AdjustmentWriteInput,
  reason: string,
  idempotencyKey?: string,
): Promise<AdjustmentWriteResult> {
  return adminWrite<AdjustmentWriteResult>(
    `/api/control/customers/${encodeURIComponent(userId)}/adjustments`,
    {
      source_document_type: input.sourceDocumentType,
      source_document_ref: input.sourceDocumentRef,
      credits: input.credits,
    },
    reason,
    "创建后台调账失败",
    idempotencyKey,
  );
}

// ---------------------------------------------------------------------------
// Queue-mode switch (M4/M5 review M2 follow-up, PR #68 Codex P1): the
// production control-plane read/write for the fair-queue rollout switch.
// ---------------------------------------------------------------------------

export async function fetchQueueMode(): Promise<boolean> {
  const response = await requestControl("/api/control/settings/queue-mode", {
    method: "GET",
  });
  if (!response.ok) {
    throw await parseActivationError(response, "读取队列模式失败");
  }
  const payload = (await response.json()) as { fair_queue_enabled: boolean };
  return payload.fair_queue_enabled;
}

export async function updateQueueMode(
  enabled: boolean,
  reason: string,
  idempotencyKey?: string,
): Promise<boolean> {
  const payload = await adminWrite<{ fair_queue_enabled: boolean }>(
    "/api/control/settings/queue-mode",
    { fair_queue_enabled: enabled },
    reason,
    "切换队列模式失败",
    idempotencyKey,
    "PATCH",
  );
  return payload.fair_queue_enabled;
}

export type ViralRuntimeControls = {
  collection_enabled: boolean;
  import_enabled: boolean;
  pending_imports: number;
  running_imports: number;
  failed_imports: number;
  pending_refreshes: number;
  running_refreshes: number;
  failed_refreshes: number;
  source_configured: boolean;
  platforms: Array<{
    platform: "douyin" | "wechat_channels";
    cached_videos: number;
    last_fetched_at: string | null;
    refresh_status:
      | "not_configured"
      | "configured_only"
      | "refreshing"
      | "ok"
      | "error";
    last_refresh_error: string | null;
  }>;
};

export async function fetchViralRuntimeControls(): Promise<ViralRuntimeControls> {
  const response = await requestControl("/api/control/settings/viral", {
    method: "GET",
  });
  if (!response.ok) {
    throw await parseActivationError(response, "读取爆款视频运行状态失败");
  }
  return (await response.json()) as ViralRuntimeControls;
}

export async function updateViralRuntimeControls(
  controls: Pick<ViralRuntimeControls, "collection_enabled" | "import_enabled">,
  reason: string,
  idempotencyKey?: string,
): Promise<ViralRuntimeControls> {
  return adminWrite<ViralRuntimeControls>(
    "/api/control/settings/viral",
    controls,
    reason,
    "更新爆款视频运行开关失败",
    idempotencyKey,
    "PATCH",
  );
}

export async function updateViralVideoAvailability(
  platform: "douyin" | "wechat_channels",
  videoId: string,
  status: "AVAILABLE" | "HIDDEN" | "UNAVAILABLE",
  reason: string,
  idempotencyKey?: string,
): Promise<{ platform: string; video_id: string; status: string }> {
  return adminWrite(
    `/api/control/viral/videos/${encodeURIComponent(platform)}/${encodeURIComponent(videoId)}/availability`,
    { status },
    reason,
    "更新爆款视频可用状态失败",
    idempotencyKey,
    "PATCH",
  );
}

// ---------------------------------------------------------------------------
// Operation rates (W10 — 费率管理：上游成本费率与对外售价)
// ---------------------------------------------------------------------------

export type OperationRate = {
  subject: string;
  kind: "upstream_cost" | "external_price";
  unit: "second" | "image" | "call";
  resolution: string | null;
  unit_price_fen: number;
  updated_at: string;
  updated_by_username: string | null;
};

export type OperationRateHistory = {
  subject: string;
  old_unit_price_fen: number | null;
  new_unit_price_fen: number;
  reason: string;
  actor_username: string | null;
  created_at: string;
};

export type OperationRatesResponse = {
  rates: OperationRate[];
  history: OperationRateHistory[];
};

export async function listOperationRates(): Promise<OperationRatesResponse> {
  const response = await requestControl("/api/control/settings/rates", {});
  if (!response.ok) {
    throw await parseActivationError(response, "读取费率失败");
  }
  return (await response.json()) as OperationRatesResponse;
}

export async function updateOperationRates(
  updates: Array<{ subject: string; unit_price_fen: number }>,
  reason: string,
  idempotencyKey?: string,
): Promise<OperationRatesResponse> {
  return adminWrite<OperationRatesResponse>(
    "/api/control/settings/rates",
    { updates },
    reason,
    "费率调整失败",
    idempotencyKey,
    "PUT",
  );
}

// ---------------------------------------------------------------------------
// Profit overview (W8 — 经营分析：每日对外售价与日利润)
// ---------------------------------------------------------------------------

export type DailyPriceRow = {
  price_date: string;
  price_768p_fen: number;
  price_2k_fen: number;
  note: string | null;
  created_by_username: string | null;
};

export type ProfitDayRow = {
  day: string;
  video_count: number;
  settled_seconds: number;
  revenue_fen: number;
  cost_fen: number | null;
  gross_fen: number | null;
  margin_pct: number | null;
};

export type ProfitOverviewResponse = {
  prices: DailyPriceRow[];
  days: ProfitDayRow[];
  cost_coverage_note: string;
};

export async function listProfitOverview(
  lookbackDays = 30,
): Promise<ProfitOverviewResponse> {
  const response = await requestControl(
    `/api/control/profit/overview?lookback_days=${lookbackDays}`,
    {},
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取经营分析失败");
  }
  return (await response.json()) as ProfitOverviewResponse;
}

export async function upsertDailyPrice(
  input: {
    price_date: string;
    price_768p_fen: number;
    price_2k_fen: number;
    note?: string;
  },
  reason: string,
  idempotencyKey?: string,
): Promise<DailyPriceRow[]> {
  return adminWrite<DailyPriceRow[]>(
    "/api/control/profit/daily-price",
    {
      price_date: input.price_date,
      price_768p_fen: input.price_768p_fen,
      price_2k_fen: input.price_2k_fen,
      note: input.note ?? "",
    },
    reason,
    "保存每日售价失败",
    idempotencyKey,
    "PUT",
  );
}

// ---------------------------------------------------------------------------
// Dashboard summary (W15 — 总览仪表盘)
// ---------------------------------------------------------------------------

export type DashboardTrendPoint = {
  day: string;
  succeeded: number;
  failed: number;
};

export type DashboardSummary = {
  today: {
    generation_count: number;
    succeeded: number;
    success_rate_pct?: number | null;
    output_seconds?: number;
    cost_fen?: number;
    revenue_fen?: number;
    gross_fen?: number | null;
    margin_pct?: number | null;
    online_devices: number;
    active_customers: number;
    recharge_fen: number;
    recharge_orders?: number;
  };
  trend: Array<DashboardTrendPoint & { cost_fen?: number }>;
  todos: {
    pending_pairings: number;
    failed_tasks_7d: number;
    reconciliation_problems: number;
    expiring_codes_7d: number;
    unconfigured_rates?: number;
    unknown_cost_records?: number;
  };
  device_slots: { bound: number; total: number };
};

export async function getDashboardSummary(): Promise<DashboardSummary> {
  const response = await requestControl("/api/control/dashboard/summary", {});
  if (!response.ok) {
    throw await parseActivationError(response, "读取仪表盘失败");
  }
  return (await response.json()) as DashboardSummary;
}
