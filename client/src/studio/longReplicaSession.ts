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

// 只恢复同一账号在本进程里的任务；登出后旧引用也必须失效，防止迟到响应重新写回。
const sessions = new Map<string, LongReplicaSession>();

export function getLongReplicaSession(accountId: string): LongReplicaSession {
  let session = sessions.get(accountId);
  if (!session) {
    session = {
      run: null,
      result: null,
      pendingAutoStart: false,
      starting: null,
    };
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
}

/** 工作台交接整片时同时绑定账号，其他账号不能消费这次自动启动意图。 */
export function handOffLongReplicaSource(
  accountId: string,
  source: { projectId: string; assetId: string },
): void {
  sessions.set(accountId, {
    run: { ...source, taskId: "" },
    result: null,
    pendingAutoStart: true,
    starting: null,
  });
}
