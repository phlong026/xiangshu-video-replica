import { useRef, useState } from "react";
import type { AdminGenerationRecord } from "../api.admin";
import { type JobOutcome, runAdjustmentBatch } from "./batchAdjustments";
import { ConfirmDialog } from "./ui/ConfirmDialog";

/**
 * 批量补偿（整改 T33）：对勾选的失败记录，按各任务当时扣掉的积分逐条补偿。
 *
 * 每条记录一次独立调账，来源单据是记录编号、幂等键按记录独立：同一条记录不会因为
 * 重试被补两次；部分失败时对话框保持打开，可只重试失败的几条。金额固定为原扣积分，
 * 不提供批量自定义金额——要给不同金额请逐条用单条补偿。
 */
export function BatchCompensateDialog({
  open,
  records,
  onClose,
  onFinished,
}: {
  open: boolean;
  records: AdminGenerationRecord[];
  onClose: () => void;
  onFinished: () => void;
}) {
  const compensable = records.filter((item) => item.charged_credits > 0);
  const skipped = records.length - compensable.length;
  const [saving, setSaving] = useState(false);
  const [progress, setProgress] = useState("");
  const [outcomes, setOutcomes] = useState<Record<string, JobOutcome>>({});
  const keysRef = useRef<{ reason: string; keys: Map<string, string> } | null>(
    null,
  );
  const failed = compensable.filter(
    (item) => outcomes[item.record_id]?.ok === false,
  );
  const succeeded = compensable.filter((item) => outcomes[item.record_id]?.ok);
  const total = compensable.reduce(
    (sum, item) => sum + item.charged_credits,
    0,
  );

  function close() {
    if (saving) return;
    if (succeeded.length > 0) onFinished();
    setOutcomes({});
    keysRef.current = null;
    onClose();
  }

  async function run(typedReason: string) {
    // 首次提交后锁定原因：重试沿用同一批幂等键，而幂等键与请求内容（含原因）绑定，
    // 中途改原因会让已到达服务端的记录撞上 409，或被当成新请求重复补偿。
    if (keysRef.current === null) {
      keysRef.current = { reason: typedReason, keys: new Map() };
    }
    const batch = keysRef.current;
    const reason = batch.reason;
    setSaving(true);
    let next: Record<string, JobOutcome> = {};
    try {
      next = await runAdjustmentBatch({
        jobs: compensable.map((item) => ({
          id: item.record_id,
          userId: item.user_id,
          label: `${item.username} · ${item.record_id}`,
          credits: item.charged_credits,
          sourceDocumentType: "CREDIT_COMPENSATION",
          sourceDocumentRef: item.record_id,
        })),
        base: outcomes,
        keys: batch.keys,
        reason,
        onProgress: setProgress,
        onOutcomes: setOutcomes,
      });
    } finally {
      setSaving(false);
      setProgress("");
    }
    if (compensable.every((item) => next[item.record_id]?.ok)) {
      onFinished();
      setOutcomes({});
      keysRef.current = null;
      onClose();
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title="批量补偿积分"
      confirmLabel={
        failed.length > 0 ? `重试失败部分（${failed.length}）` : "确认补偿"
      }
      description={`${failed.length > 0 ? "重试沿用首次提交的原因。" : ""}将对 ${compensable.length} 条失败记录各补偿其原扣积分，合计 ${total} 积分，逐条留痕${
        skipped > 0 ? `；另有 ${skipped} 条原扣 0 积分，已跳过` : ""
      }。`}
      busy={saving}
      error={progress}
      level="reason"
      onClose={close}
      onConfirm={(reason) => void run(reason)}
    >
      <ul aria-label="待补偿记录">
        {compensable.map((item) => (
          <li key={item.record_id}>
            {item.username} · {item.record_id} · {item.charged_credits} 积分
          </li>
        ))}
      </ul>
      {Object.keys(outcomes).length > 0 ? (
        <div role="status" aria-label="补偿结果">
          <p>
            成功 {succeeded.length} / 失败 {failed.length}
          </p>
          <ul>
            {failed.map((item) => (
              <li key={item.record_id}>
                {item.record_id}：{outcomes[item.record_id]?.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </ConfirmDialog>
  );
}
