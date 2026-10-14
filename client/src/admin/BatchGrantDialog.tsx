import { useRef, useState } from "react";
import { createCustomerAdjustment } from "../api.admin";
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
    fingerprint: string;
    ref: string;
    keys: Map<string, string>;
  } | null>(null);

  const failed = targets.filter((item) => outcomes[item.user_id]?.ok === false);
  const succeeded = targets.filter((item) => outcomes[item.user_id]?.ok);
  const isRetry = failed.length > 0;

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
    const amount = Number.parseInt(credits, 10);
    if (
      !Number.isInteger(amount) ||
      amount <= 0 ||
      String(amount) !== credits
    ) {
      setError("赠送积分必须是正整数。");
      return;
    }
    // 金额 / 单号 / 原因任一变化都视为新的一批，换一组幂等键。
    const fingerprint = `${amount}|${ref.trim()}|${reason}`;
    const fresh = keysRef.current?.fingerprint !== fingerprint;
    if (fresh) {
      keysRef.current = {
        fingerprint,
        ref: ref.trim() || `batch-${Date.now()}`,
        keys: new Map(),
      };
      setOutcomes({});
    }
    const batch = keysRef.current as NonNullable<typeof keysRef.current>;
    const base: Record<string, Outcome> = fresh ? {} : outcomes;
    const pending = targets.filter((item) => base[item.user_id]?.ok !== true);
    setSaving(true);
    setError("");
    const next: Record<string, Outcome> = { ...base };
    try {
      let index = 0;
      for (const item of pending) {
        index += 1;
        setProgress(`正在发放（${index}/${pending.length}）：${item.username}`);
        let key = batch.keys.get(item.user_id);
        if (!key) {
          key = crypto.randomUUID();
          batch.keys.set(item.user_id, key);
        }
        try {
          await createCustomerAdjustment(
            item.user_id,
            {
              sourceDocumentType: "FREE_GRANT",
              sourceDocumentRef: batch.ref,
              credits: amount,
            },
            reason,
            key,
          );
          next[item.user_id] = { ok: true, message: "" };
        } catch (cause) {
          next[item.user_id] = {
            ok: false,
            message: cause instanceof Error ? cause.message : "发放失败",
          };
        }
        setOutcomes({ ...next });
      }
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
          value={credits}
          onChange={(event) => setCredits(event.target.value)}
        />
      </label>
      <label>
        发放单号（可追溯）
        <input
          value={ref}
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
