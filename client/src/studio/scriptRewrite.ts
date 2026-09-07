export type ScriptRewriteScope = {
  accountId: string;
  projectId: string;
  sourceAssetId: string;
  identityId: string;
  scriptId: string;
  scriptVersion: number;
  text: string;
};

const inMemoryKeys = new Map<string, Map<string, string>>();
const ACTIVE_FINGERPRINT = "$active";

function accountStorageKey(accountId: string) {
  return `studio:script-rewrite:${accountId}`;
}

function scopeFingerprint(scope: ScriptRewriteScope) {
  return JSON.stringify([
    scope.projectId,
    scope.sourceAssetId,
    scope.identityId,
    scope.scriptId,
    scope.scriptVersion,
    scope.text,
  ]);
}

function memoryKeys(accountId: string) {
  const storageKey = accountStorageKey(accountId);
  let keys = inMemoryKeys.get(storageKey);
  if (!keys) {
    keys = new Map();
    inMemoryKeys.set(storageKey, keys);
  }
  return keys;
}

function readStoredKeys(accountId: string): Record<string, string> {
  try {
    const decoded = JSON.parse(
      window.sessionStorage.getItem(accountStorageKey(accountId)) ?? "{}",
    );
    return decoded && typeof decoded === "object"
      ? (decoded as Record<string, string>)
      : {};
  } catch {
    return {};
  }
}

function writeStoredKeys(accountId: string, keys: Record<string, string>) {
  try {
    window.sessionStorage.setItem(
      accountStorageKey(accountId),
      JSON.stringify(keys),
    );
  } catch {
    // The in-memory copy still preserves retries for this mount.
  }
}

export function scriptRewriteIdempotencyKey(scope: ScriptRewriteScope): string {
  const fingerprint = scopeFingerprint(scope);
  const stored = readStoredKeys(scope.accountId);
  const memory = memoryKeys(scope.accountId);
  const previousFingerprint = stored[ACTIVE_FINGERPRINT];
  if (previousFingerprint && previousFingerprint !== fingerprint) {
    delete stored[previousFingerprint];
    memory.delete(previousFingerprint);
  }
  stored[ACTIVE_FINGERPRINT] = fingerprint;
  if (typeof stored[fingerprint] === "string" && stored[fingerprint]) {
    writeStoredKeys(scope.accountId, stored);
    return stored[fingerprint];
  }
  const existing = memory.get(fingerprint);
  if (existing && previousFingerprint === fingerprint) {
    writeStoredKeys(scope.accountId, stored);
    return existing;
  }
  const created = crypto.randomUUID();
  memory.set(fingerprint, created);
  stored[fingerprint] = created;
  writeStoredKeys(scope.accountId, stored);
  return created;
}

export function clearScriptRewriteIdempotencyKey(
  scope: ScriptRewriteScope,
  expectedKey: string,
) {
  const fingerprint = scopeFingerprint(scope);
  const stored = readStoredKeys(scope.accountId);
  if (stored[fingerprint] === expectedKey) {
    delete stored[fingerprint];
    writeStoredKeys(scope.accountId, stored);
  }
  const memory = memoryKeys(scope.accountId);
  if (memory.get(fingerprint) === expectedKey) memory.delete(fingerprint);
}

export function shouldClearScriptRewriteIdempotencyKey(error: unknown) {
  if (!error || typeof error !== "object") return false;
  const candidate = error as {
    code?: string;
    retryable?: boolean;
    status?: number;
  };
  if (candidate.code === "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN") return false;
  if (candidate.retryable === true) return false;
  if (candidate.retryable === false) return true;
  return (
    candidate.status !== undefined &&
    candidate.status >= 400 &&
    candidate.status < 500
  );
}
