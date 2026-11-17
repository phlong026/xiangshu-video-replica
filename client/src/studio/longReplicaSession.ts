import type { ReplicaPromptTask, ReplicaPromptVersion } from "../api";

export type LongReplicaRun = {
  projectId: string;
  assetId: string;
  taskId: string;
};

type LongReplicaSession = {
  run: LongReplicaRun | null;
  result: ReplicaPromptVersion | null;
  pendingAutoStart: boolean;
  starting: Promise<ReplicaPromptTask> | null;
};

// 仅持久化账号隔离的任务定位，正文和任务状态仍从服务端恢复。
const sessions = new Map<string, LongReplicaSession>();

function storageKey(accountId: string) {
  return `long-replica-run:${accountId}`;
}
function readRun(accountId: string): LongReplicaRun | null {
  try {
    const value = JSON.parse(
      localStorage.getItem(storageKey(accountId)) ?? "null",
    ) as LongReplicaRun | null;
    if (
      value &&
      [value.projectId, value.assetId, value.taskId].every(
        (id) => typeof id === "string" && id.length <= 128,
      ) &&
      value.projectId &&
      value.assetId
    )
      return value;
  } catch {
    /* 旧数据或损坏记录不阻塞页面。 */
  }
  return null;
}
function newSession(
  accountId: string,
  initial: LongReplicaRun | null,
): LongReplicaSession {
  let run = initial;
  const session: LongReplicaSession = {
    get run() {
      return run;
    },
    set run(next) {
      run = next;
      if (sessions.get(accountId) !== session) return;
      try {
        if (next)
          localStorage.setItem(storageKey(accountId), JSON.stringify(next));
        else localStorage.removeItem(storageKey(accountId));
      } catch {
        /* 存储不可用时仍可在本进程恢复。 */
      }
    },
    result: null,
    pendingAutoStart: false,
    starting: null,
  };
  return session;
}

export function getLongReplicaSession(accountId: string): LongReplicaSession {
  let session = sessions.get(accountId);
  if (!session) {
    session = newSession(accountId, readRun(accountId));
    sessions.set(accountId, session);
  }
  return session;
}

export function isCurrentLongReplicaSession(
  accountId: string,
  session: LongReplicaSession,
): boolean {
  return sessions.get(accountId) === session;
}

export function clearLongReplicaSession(accountId: string): void {
  sessions.delete(accountId);
  try {
    localStorage.removeItem(storageKey(accountId));
  } catch {
    /* 允许禁用存储。 */
  }
}

/** 工作台交接整片时同时绑定账号，其他账号不能消费这次自动启动意图。 */
export function handOffLongReplicaSource(
  accountId: string,
  source: { projectId: string; assetId: string; taskId?: string },
): void {
  const session = newSession(accountId, null);
  sessions.set(accountId, session);
  session.run = { ...source, taskId: source.taskId ?? "" };
  session.pendingAutoStart = !source.taskId;
}
