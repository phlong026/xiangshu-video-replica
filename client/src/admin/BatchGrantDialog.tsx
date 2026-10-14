import { useRef, useState } from "react";
import { runAdjustmentBatch } from "./batchAdjustments";
import { ConfirmDialog } from "./ui/ConfirmDialog";

export interface BatchTarget {
  user_id: string;
  username: string;
}

type Outcome = { ok: boolean; message: string };

/**
 * 批量赠送积分（整改 T31）：客户运营与客户列表共用。
 *
 * 逐户调用既有调账端点，每户一把独立幂等键：部分失败时对话框保持打开，显示
 * 「成功 N / 失败 M」与失败原因，「重试失败部分」只重发失败的客户并沿用原幂等键
 * ——某一户其实已经入账只是响应丢了，重放得到原结果，不会重复发放。
 * 钱包写入仍只在调账端点里，这里不复制账务逻辑。
 */
export function BatchGrantDialog({
  open,
  targets,
  onClose,
  onFinished,
}: {
  open: boolean;
  targets: BatchTarget[];
  onClose: () => void;
  /** 对话框关闭时若至少有一户成功，通知调用方刷新列表。 */
  onFinished: (succeededIds: string[]) => void;
}) {
  const [credits, setCredits] = useState("");
  const [ref, setRef] = useState("");
  const [saving, setSaving] = useState(false);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const keysRef = useRef<{
    amount: number;
    reason: string;
    ref: string;
    keys: Map<string, string>;
  } | null>(null);

  const failed = targets.filter((item) => outcomes[item.user_id]?.ok === false);
  const succeeded = targets.filter((item) => outcomes[item.user_id]?.ok);
  const isRetry = failed.length > 0;
  const locked = keysRef.current !== null;

  function close() {
    if (saving) return;
    if (succeeded.length > 0) onFinished(succeeded.map((item) => item.user_id));
    setOutcomes({});
    setError("");
    setProgress("");
    keysRef.current = null;
    onClose();
  }

  async function run(reason: string) {
    // 首次提交后锁定金额 / 单号 / 原因：重试沿用同一批幂等键，而幂等键与请求内容绑定，
    // 中途改动会让已到达服务端的客户撞 409，或被当成新请求重复发放。
    if (keysRef.current === null) {
      const amount = Number.parseInt(credits, 10);
      if (
        !Number.isInteger(amount) ||
        amount <= 0 ||
        String(amount) !== credits
      ) {
        setError("赠送积分必须是正整数。");
        return;
      }
      keysRef.current = {
        amount,
        reason,
        ref: ref.trim() || `batch-${Date.now()}`,
        keys: new Map(),
      };
    }
    const batch = keysRef.current;
    const base: Record<string, Outcome> = outcomes;
    setSaving(true);
    setError("");
    let next: Record<string, Outcome> = base;
    try {
      next = await runAdjustmentBatch({
        jobs: targets.map((item) => ({
          id: item.user_id,
          userId: item.user_id,
          label: item.username,
          credits: batch.amount,
          sourceDocumentType: "FREE_GRANT",
          sourceDocumentRef: batch.ref,
        })),
        base,
        keys: batch.keys,
        reason: batch.reason,
        onProgress: setProgress,
        onOutcomes: setOutcomes,
      });
    } finally {
      setSaving(false);
      setProgress("");
    }
    const failedCount = targets.filter(
      (item) => next[item.user_id]?.ok === false,
    ).length;
    if (failedCount === 0) {
      onFinished(targets.map((item) => item.user_id));
      setOutcomes({});
      keysRef.current = null;
      onClose();
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title="批量赠送积分"
      confirmLabel={isRetry ? `重试失败部分（${failed.length}）` : "确认发放"}
      description={`将向 ${targets.length} 名客户逐户赠送积分并逐户留痕；发放过程不可中途撤销。`}
      busy={saving}
      error={progress || error}
      level="reason"
      onClose={close}
      onConfirm={(reason) => void run(reason)}
    >
      <label>
        每户赠送积分（正整数）
        <input
          type="number"
          min={1}
          disabled={locked}
          value={credits}
          onChange={(event) => setCredits(event.target.value)}
        />
      </label>
      <label>
        发放单号（可追溯）
        <input
          value={ref}
          disabled={locked}
          onChange={(event) => setRef(event.target.value)}
          placeholder="如：十月激活活动"
        />
      </label>
      {Object.keys(outcomes).length > 0 ? (
        <div role="status" aria-label="发放结果">
          <p>
            成功 {succeeded.length} / 失败 {failed.length}
          </p>
          <ul>
            {failed.map((item) => (
              <li key={item.user_id}>
                {item.username}：{outcomes[item.user_id]?.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </ConfirmDialog>
  );
}
