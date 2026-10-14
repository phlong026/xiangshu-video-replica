import { createCustomerAdjustment } from "../api.admin";

export interface AdjustmentJob {
  /** 本批内唯一的行标识（批量赠送用 user_id，批量补偿用记录编号）。 */
  id: string;
  userId: string;
  label: string;
  credits: number;
  sourceDocumentType: string;
  sourceDocumentRef: string;
}

export type JobOutcome = { ok: boolean; message: string };

/**
 * 逐户调账的批量执行器（整改 T31/T33 共用）。
 *
 * 每个任务一把独立幂等键并保存在调用方传入的 keys 里：失败后重试沿用同一把键，
 * 若上次其实已入账只是响应丢了，服务端按重放返回原结果，不会重复发放。
 * 已成功的任务（base 中 ok=true）直接跳过，所以「重试失败部分」就是同一个函数再跑一遍。
 * 一户失败不中断后面的客户；钱包写入始终只发生在调账端点里。
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
