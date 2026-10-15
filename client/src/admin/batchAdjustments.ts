import { createCustomerAdjustment } from "../api.admin";
import { apiBaseUrl } from "../apiBase";

export interface AdjustmentJob {
  /** 本批内唯一的行标识（批量赠送用 user_id，批量补偿用记录编号）。 */
  id: string;
  userId: string;
  label: string;
  credits: number;
  sourceDocumentType: string;
  sourceDocumentRef: string;
  /** 同一业务操作跨重试、刷新与操作员保持不变；批量赠送恢复批次时按原值重发。 */
  operationId?: string;
}

export type JobOutcome = { ok: boolean; message: string };

/**
 * 逐户调账的批量执行器（整改 T31/T33 共用）。
 *
 * 每个任务一把独立幂等键并保存在调用方传入的 keys 里：失败后重试沿用同一把键，
 * 若上次其实已入账只是响应丢了，服务端按重放返回原结果，不会重复发放。
 * 已成功的任务（base 中 ok=true）直接跳过，所以「重试失败部分」就是同一个函数再跑一遍。
 * 一户失败不中断后面的客户；钱包写入始终只发生在调账端点里。
 *
 * ``onOutcomes`` 抛错会中断整批（调用方用它落盘恢复记录：存不下就不能继续发）。
 */
export async function runAdjustmentBatch({
  jobs,
  base,
  keys,
  reason,
  onProgress,
  onOutcomes,
}: {
  jobs: AdjustmentJob[];
  base: Record<string, JobOutcome>;
  keys: Map<string, string>;
  reason: string;
  onProgress: (text: string) => void;
  onOutcomes: (outcomes: Record<string, JobOutcome>) => void;
}): Promise<Record<string, JobOutcome>> {
  const next: Record<string, JobOutcome> = { ...base };
  const pending = jobs.filter((job) => base[job.id]?.ok !== true);
  let index = 0;
  for (const job of pending) {
    index += 1;
    onProgress(`正在发放（${index}/${pending.length}）：${job.label}`);
    let key = keys.get(job.id);
    if (!key) {
      key = crypto.randomUUID();
      keys.set(job.id, key);
    }
    try {
      await createCustomerAdjustment(
        job.userId,
        {
          sourceDocumentType: job.sourceDocumentType,
          sourceDocumentRef: job.sourceDocumentRef,
          credits: job.credits,
          // 逐字段照抄恢复记录：请求体是服务端幂等哈希的一部分，少一个字段就会
          // 在重放原幂等键时撞 409，或被当成新请求再发一次。
          ...(job.operationId === undefined
            ? {}
            : { operationId: job.operationId }),
        },
        reason,
        key,
      );
      next[job.id] = { ok: true, message: "" };
    } catch (cause) {
      next[job.id] = {
        ok: false,
        message: cause instanceof Error ? cause.message : "发放失败",
      };
    }
    onOutcomes({ ...next });
  }
  return next;
}

// --- 批量赠送的恢复记录（迁移自 CustomerLifecyclePage，改由双方共用）-------------
//
// 发放到一半刷新、崩溃或关掉页面时，已受理的客户不能重复发放、未受理的也不能
// 静默丢失：整批请求在第一次发送前就落到 localStorage，每成功一户再落一次进度。
// 恢复时逐户沿用原 operationId 与幂等键重发，服务端按重放返回原结果。

/** 恢复记录里最多接受的客户数：选择可能跨页，这个上限只用来拒绝损坏/伪造的记录。 */
export const MAX_GRANT_TARGETS = 200;
/** 与调账端点接受的积分上限一致，防住手改 localStorage 造出的天文数字。 */
export const MAX_GRANT_CREDITS = 2_147_483_647;

export type GrantTarget = {
  userId: string;
  username: string;
  operationId: string;
  idempotencyKey: string;
  done: boolean;
};

export type GrantBatch = {
  version: 1;
  id: string;
  apiBase: string;
  sourceDocumentRef: string;
  credits: number;
  reason: string;
  targets: GrantTarget[];
};

export function batchStorageKey(base = apiBaseUrl()): string {
  // 同一浏览器切换服务端时不能把旧环境的发放请求带到新环境。
  return `admin:customer-grant-batch:v1:${base}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNonBlank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/** 读取未完成的发放批次；格式不合法时抛错（调用方必须停下来人工核对，不能猜）。 */
export function readGrantBatch(): GrantBatch | null {
  const stored = localStorage.getItem(batchStorageKey());
  if (stored === null) return null;
  const value: unknown = JSON.parse(stored);
  if (
    !isRecord(value) ||
    value.version !== 1 ||
    !isNonBlank(value.id) ||
    value.apiBase !== apiBaseUrl() ||
    !isNonBlank(value.sourceDocumentRef) ||
    !isNonBlank(value.reason) ||
    typeof value.credits !== "number" ||
    !Number.isSafeInteger(value.credits) ||
    value.credits <= 0 ||
    value.credits > MAX_GRANT_CREDITS ||
    !Array.isArray(value.targets) ||
    value.targets.length === 0 ||
    value.targets.length > MAX_GRANT_TARGETS ||
    !value.targets.every(
      (target: unknown) =>
        isRecord(target) &&
        isNonBlank(target.userId) &&
        isNonBlank(target.username) &&
        isNonBlank(target.operationId) &&
        target.operationId.length <= 128 &&
        isNonBlank(target.idempotencyKey) &&
        typeof target.done === "boolean",
    )
  ) {
    throw new Error("批次恢复记录格式无效");
  }
  const batch = value as GrantBatch;
  for (const field of ["userId", "operationId", "idempotencyKey"] as const) {
    if (
      new Set(batch.targets.map((target) => target[field])).size !==
      batch.targets.length
    ) {
      throw new Error("批次恢复记录包含重复目标或标识");
    }
  }
  return batch;
}

/** 落盘恢复记录；写不进去就抛错，让调用方停止发放而不是裸奔。 */
export function persistGrantBatch(batch: GrantBatch): void {
  if (batch.apiBase !== apiBaseUrl()) {
    throw new Error("服务端地址已更改，已暂停原批次。请切回原服务端后继续。");
  }
  try {
    const key = batchStorageKey(batch.apiBase);
    const serialized = JSON.stringify(batch);
    localStorage.setItem(key, serialized);
    if (localStorage.getItem(key) !== serialized) throw new Error();
  } catch {
    throw new Error(
      "无法保存批次恢复记录，已暂停发放。请恢复浏览器存储后继续原批次。",
    );
  }
}

export function clearGrantBatch(base = apiBaseUrl()): void {
  localStorage.removeItem(batchStorageKey(base));
}
