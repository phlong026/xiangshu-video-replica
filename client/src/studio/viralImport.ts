const inMemoryKeys = new Map<string, string>();

export function viralImportIdempotencyKey(actionKey: string): string {
  const storageKey = `studio:viral-import:${actionKey}`;
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (stored) return stored;
    const created = crypto.randomUUID();
    window.sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    const stored = inMemoryKeys.get(actionKey);
    if (stored) return stored;
    const created = crypto.randomUUID();
    inMemoryKeys.set(actionKey, created);
    return created;
  }
}
