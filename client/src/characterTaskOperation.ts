// 只保存请求指纹和任务编号；原图、姓名与会话凭据不进入浏览器存储。
export type CharacterOperation = {
  storageKey: string;
  key: string;
  taskId?: string;
};

export async function characterOperationDigest(value: string | ArrayBuffer) {
  const bytes =
    typeof value === "string" ? new TextEncoder().encode(value) : value;
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(hash), (part) =>
    part.toString(16).padStart(2, "0"),
  ).join("");
}

export async function openCharacterOperation(
  scope: string,
  parameters: unknown,
): Promise<CharacterOperation> {
  const storageKey = `video-replica:character-operation:v1:${await characterOperationDigest(scope)}:${await characterOperationDigest(JSON.stringify(parameters))}`;
  const raw = localStorage.getItem(storageKey);
  if (raw) {
    const saved = JSON.parse(raw) as Partial<CharacterOperation>;
    if (
      typeof saved.key !== "string" ||
      !saved.key ||
      (saved.taskId !== undefined && typeof saved.taskId !== "string")
    )
      throw new Error(
        "上次人物任务记录无法读取，请先联系管理员核对，避免重复提交。",
      );
    return { storageKey, key: saved.key, taskId: saved.taskId };
  }
  const operation = {
    storageKey,
    key: `character-sheet-${crypto.randomUUID()}`,
  };
  saveCharacterOperation(operation);
  return operation;
}

export function saveCharacterOperation(operation: CharacterOperation) {
  // 写入失败就停止提交，不能在刷新后丢失同次操作身份。
  localStorage.setItem(
    operation.storageKey,
    JSON.stringify({ key: operation.key, taskId: operation.taskId }),
  );
}

export function finishCharacterOperation(operation: CharacterOperation) {
  const saved = localStorage.getItem(operation.storageKey);
  if (saved && (JSON.parse(saved) as CharacterOperation).key === operation.key)
    localStorage.removeItem(operation.storageKey);
}

export function finishObservedCharacterTask(taskId: string) {
  // 恢复面板已经读到终态后，新生成不能再次重放旧结果；其他任务的 key 仍须保留。
  // 清理失败不能阻止只读核对；新提交仍会执行上面的持久化检查，保持收费幂等。
  try {
    const keys = Array.from({ length: localStorage.length }, (_, index) =>
      localStorage.key(index),
    );
    for (const storageKey of keys) {
      if (!storageKey?.startsWith("video-replica:character-operation:v1:"))
        continue;
      let saved: Partial<CharacterOperation> | null;
      try {
        saved = JSON.parse(localStorage.getItem(storageKey) ?? "null");
      } catch {
        continue;
      }
      if (saved?.taskId === taskId) localStorage.removeItem(storageKey);
    }
  } catch {
    // 浏览器禁用存储时仍允许查看已存在的任务，不把读取终态变成生成错误。
  }
}
