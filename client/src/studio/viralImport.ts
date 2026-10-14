const inMemoryKeys = new Map<string, string>();

function importStorageKey(accountId: string, actionKey: string) {
  return `studio:viral-import:${accountId}:${actionKey}`;
}

export class ViralImportPollingTimeoutError extends Error {}

export function viralImportIdempotencyKey(
  accountId: string,
  actionKey: string,
): string {
  const storageKey = importStorageKey(accountId, actionKey);
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (stored) return stored;
    const created = crypto.randomUUID();
    window.sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    const stored = inMemoryKeys.get(storageKey);
    if (stored) return stored;
    const created = crypto.randomUUID();
    inMemoryKeys.set(storageKey, created);
    return created;
  }
}

export function clearViralImportIdempotencyKey(
  accountId: string,
  actionKey: string,
  expectedKey: string,
) {
  const storageKey = importStorageKey(accountId, actionKey);
  try {
    if (window.sessionStorage.getItem(storageKey) === expectedKey) {
      window.sessionStorage.removeItem(storageKey);
    }
  } catch {
    // The in-memory fallback below still keeps this tab correct.
  }
  if (inMemoryKeys.get(storageKey) === expectedKey) {
    inMemoryKeys.delete(storageKey);
  }
}

/** 前端等待超时只说明服务端导入慢，任务仍在后台跑：保留幂等键，下次点击接回同一任务。 */
export const VIRAL_IMPORT_POLLING_TIMEOUT_MESSAGE =
  "导入仍在后台进行，稍后再点一次会接回同一导入任务，不会重复创建。";

export function shouldClearViralImportIdempotencyKey(error: unknown) {
  // 轮询超时不清键（FIX-09）：早先清键后再点会建出新的导入任务和一个空壳项目，
  // 而原任务稍后完成却再也没有人接收结果。
  if (error instanceof ViralImportPollingTimeoutError) return false;
  if (!error || typeof error !== "object") return false;
  if ("retryable" in error && error.retryable === false) return true;
  if (!("status" in error) || typeof error.status !== "number") return false;
  return error.status >= 400 && error.status < 500;
}
