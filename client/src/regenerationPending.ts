import type { GenerationPriceQuote, PaidRegenerationInput } from "./api";

export type PendingRegeneration = {
  kind: "batch" | "task";
  sourceId: string;
  batchId: string;
  account: string;
  actionKey: string;
  request: PaidRegenerationInput;
  quote: GenerationPriceQuote;
  submitted: boolean;
};

function key(account: string, kind: "batch" | "task", sourceId: string) {
  return `generation.pending.v1:${JSON.stringify([account, kind, sourceId])}`;
}

function sameRequest(
  left: PendingRegeneration,
  right: PendingRegeneration,
): boolean {
  return (
    left.request.idempotency_key === right.request.idempotency_key &&
    left.request.quote_token === right.request.quote_token &&
    left.request.max_credits === right.request.max_credits &&
    left.request.generation_reason === right.request.generation_reason &&
    left.request.estimated_cost_snapshot ===
      right.request.estimated_cost_snapshot
  );
}

function unavailable(): Error {
  return new Error(
    "无法安全保存或读取原生成请求，请检查本机存储后再核对，暂不新建付费任务。",
  );
}

export function readPendingRegeneration(
  account: string,
  kind: "batch" | "task",
  sourceId: string,
): PendingRegeneration | null {
  try {
    const raw = localStorage.getItem(key(account, kind, sourceId));
    if (raw === null) return null;
    const value = JSON.parse(raw) as PendingRegeneration;
    if (
      value.account !== account ||
      value.kind !== kind ||
      value.sourceId !== sourceId ||
      typeof value.batchId !== "string" ||
      !value.batchId ||
      typeof value.actionKey !== "string" ||
      !value.actionKey ||
      value.submitted !== true ||
      typeof value.request?.idempotency_key !== "string" ||
      !value.request.idempotency_key ||
      typeof value.request.generation_reason !== "string" ||
      typeof value.quote?.quote_token !== "string" ||
      !value.quote.quote_token ||
      value.request.quote_token !== value.quote.quote_token ||
      !Number.isSafeInteger(value.quote.max_credits) ||
      value.quote.max_credits < 0 ||
      value.request.max_credits !== value.quote.max_credits ||
      value.quote.estimated_credits !== value.quote.max_credits ||
      !Number.isSafeInteger(value.quote.quantity) ||
      value.quote.quantity < 1
    )
      throw unavailable();
    return value;
  } catch {
    throw unavailable();
  }
}

export async function savePendingRegeneration(
  value: PendingRegeneration,
): Promise<void> {
  if (!navigator.locks) throw unavailable();
  await navigator.locks.request(
    key(value.account, value.kind, value.sourceId),
    () => {
      const current = readPendingRegeneration(
        value.account,
        value.kind,
        value.sourceId,
      );
      if (current && !sameRequest(current, value)) {
        throw new Error("该任务已有待核对请求，请取消并重新打开以核对原请求。");
      }
      try {
        const storageKey = key(value.account, value.kind, value.sourceId);
        const raw = JSON.stringify(value);
        localStorage.setItem(storageKey, raw);
        if (localStorage.getItem(storageKey) !== raw) throw unavailable();
      } catch {
        throw unavailable();
      }
    },
  );
}

export async function clearPendingRegeneration(
  value: PendingRegeneration,
): Promise<void> {
  if (!navigator.locks) throw unavailable();
  await navigator.locks.request(
    key(value.account, value.kind, value.sourceId),
    () => {
      try {
        const storageKey = key(value.account, value.kind, value.sourceId);
        // 保存和清理使用同一跨窗口锁；迟到响应不得删除新轮次。
        const current = readPendingRegeneration(
          value.account,
          value.kind,
          value.sourceId,
        );
        if (current && !sameRequest(current, value)) return;
        localStorage.removeItem(storageKey);
        if (localStorage.getItem(storageKey) !== null) throw unavailable();
      } catch {
        throw unavailable();
      }
    },
  );
}
