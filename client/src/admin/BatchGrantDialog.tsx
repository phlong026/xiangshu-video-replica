import { useEffect, useRef, useState } from "react";
import { apiBaseUrl } from "../apiBase";
import {
  clearGrantBatch,
  type GrantBatch,
  MAX_GRANT_CREDITS,
  persistGrantBatch,
  readGrantBatch,
  runAdjustmentBatch,
} from "./batchAdjustments";
import { ConfirmDialog } from "./ui/ConfirmDialog";

export interface BatchTarget {
  user_id: string;
  username: string;
}

type Outcome = { ok: boolean; message: string };

/**
 * 批量赠送积分（整改 T31）：客户运营与客户列表共用同一实现。
 *
 * 逐户调用既有调账端点，每户一把独立幂等键与一个 operationId：部分失败时对话框保持
 * 打开，显示「成功 N / 失败 M」与失败原因，「重试失败部分」只重发失败的客户并沿用原
 * 标识——某一户其实已经入账只是响应丢了，重放得到原结果，不会重复发放。
 *
 * 整批请求在第一次发送前落到 localStorage、每成功一户再落一次进度：发放到一半刷新、
 * 崩溃或关掉页面，重开对话框会按记录继续（只发未完成的客户），不是按当前勾选另起
 * 一批。落盘失败会中断整批——存不下恢复记录就不能继续发钱。
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
  const [notice, setNotice] = useState("");
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  /** 恢复记录读不出来：禁止另起一批，先人工核对，避免覆盖发放流水。 */
  const [blocked, setBlocked] = useState(false);
  /** 本批的真源：有恢复记录时以记录为准（当前勾选不作为新批次）。 */
  const batchRef = useRef<GrantBatch | null>(null);

  useEffect(() => {
    if (!open) return;
    setError("");
    setNotice("");
    setProgress("");
    setOutcomes({});
    let restored: GrantBatch | null = null;
    try {
      restored = readGrantBatch();
    } catch {
      // 记录读不出来就不能另起一批：覆盖掉它等于丢掉一笔说不清的发放流水。
      batchRef.current = null;
      setBlocked(true);
      setError(
        "无法读取批次恢复记录，已暂停赠送。请先核对发放流水并恢复浏览器存储，勿直接清除记录后重发。",
      );
      return;
    }
    setBlocked(false);
    batchRef.current = restored;
    if (restored === null) return;
    const done = restored.targets.filter((target) => target.done);
    setCredits(String(restored.credits));
    setRef(restored.sourceDocumentRef);
    setOutcomes(
      Object.fromEntries(
        done.map((target) => [
          target.userId,
          { ok: true, message: "" } as Outcome,
        ]),
      ),
    );
    setNotice(
      `发现未完成的发放批次：还有 ${restored.targets.length - done.length} 名客户未发放；继续后沿用原幂等键与操作号，已完成的 ${done.length} 名不会重复发放。`,
    );
  }, [open]);

  const record = batchRef.current;
  const effective: BatchTarget[] = record
    ? record.targets.map((target) => ({
        user_id: target.userId,
        username: target.username,
      }))
    : targets;
  const failed = effective.filter(
    (item) => outcomes[item.user_id]?.ok === false,
  );
  const succeeded = effective.filter((item) => outcomes[item.user_id]?.ok);
  const isRetry = failed.length > 0;
  const locked = record !== null;
  const unfinishedCount = record
    ? record.targets.filter((target) => !target.done).length
    : 0;

  function close() {
    if (saving) return;
    if (succeeded.length > 0) onFinished(succeeded.map((item) => item.user_id));
    setOutcomes({});
    setError("");
    setNotice("");
    setProgress("");
    onClose();
  }

  async function run(reason: string) {
    // 记录不可读时连新批次也不许建：错误已经在打开时给出，等人工核对。
    if (blocked) return;
    let batch = batchRef.current;
    if (batch === null) {
      const amount = Number.parseInt(credits, 10);
      if (
        !Number.isInteger(amount) ||
        amount <= 0 ||
        String(amount) !== credits ||
        amount > MAX_GRANT_CREDITS
      ) {
        setError(`赠送积分必须是 1 至 ${MAX_GRANT_CREDITS} 的正整数。`);
        return;
      }
      if (targets.length === 0) {
        setError("请先选择要赠送的客户。");
        return;
      }
      batch = {
        version: 1,
        id: crypto.randomUUID(),
        apiBase: apiBaseUrl(),
        sourceDocumentRef: ref.trim() || `batch-${Date.now()}`,
        credits: amount,
        reason,
        targets: targets.map((item) => ({
          userId: item.user_id,
          username: item.username,
          operationId: crypto.randomUUID(),
          idempotencyKey: crypto.randomUUID(),
          done: false,
        })),
      };
      batchRef.current = batch;
      setRef(batch.sourceDocumentRef);
    }
    // 发送前确认恢复记录可写、且仍绑定当前服务端：写不进去（或已切到别的服务端）
    // 就停在这里，而不是先把钱发出去再发现进度存不下来。
    try {
      persistGrantBatch(batch);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "无法保存批次恢复记录，已暂停发放。",
      );
      return;
    }
    const base: Record<string, Outcome> = {
      ...Object.fromEntries(
        batch.targets
          .filter((target) => target.done)
          .map((target) => [
            target.userId,
            { ok: true, message: "" } as Outcome,
          ]),
      ),
      ...outcomes,
    };
    const keys = new Map(
      batch.targets.map((target) => [target.userId, target.idempotencyKey]),
    );
    const running = batch;
    setSaving(true);
    setError("");
    let next: Record<string, Outcome> = base;
    try {
      next = await runAdjustmentBatch({
        jobs: running.targets.map((target) => ({
          id: target.userId,
          userId: target.userId,
          label: target.username,
          credits: running.credits,
          sourceDocumentType: "FREE_GRANT",
          sourceDocumentRef: running.sourceDocumentRef,
          operationId: target.operationId,
        })),
        base,
        keys,
        reason: running.reason,
        onProgress: setProgress,
        onOutcomes: (latest) => {
          setOutcomes(latest);
          const snapshot = batchRef.current;
          if (snapshot === null) return;
          const updated: GrantBatch = {
            ...snapshot,
            targets: snapshot.targets.map((target) => ({
              ...target,
              done: latest[target.userId]?.ok === true,
            })),
          };
          // 抛错即中断整批：恢复记录写不进去就不能继续发钱。
          persistGrantBatch(updated);
          batchRef.current = updated;
        },
      });
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "发放已中断，请核对发放流水后继续。",
      );
      return;
    } finally {
      setSaving(false);
      setProgress("");
    }
    const unfinished = running.targets.filter(
      (target) => next[target.userId]?.ok !== true,
    );
    if (unfinished.length === 0) {
      try {
        clearGrantBatch(running.apiBase);
      } catch {
        // 清理失败（隐私模式、配额）不能把已完成的发放当失败：记录里所有客户都是
        // done，下次打开只会走「只清理完成记录」，不会重复发放。
      }
      batchRef.current = null;
      onFinished(running.targets.map((target) => target.userId));
      setOutcomes({});
      setNotice("");
      onClose();
    }
  }

  return (
    <ConfirmDialog
      open={open}
      title={locked ? "继续批量赠送积分" : "批量赠送积分"}
      confirmLabel={
        isRetry
          ? `重试失败部分（${failed.length}）`
          : locked
            ? `继续发放未完成客户（${unfinishedCount}）`
            : "确认发放"
      }
      description={`将向 ${effective.length} 名客户逐户赠送积分并逐户留痕；发放过程不可中途撤销。`}
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
          max={MAX_GRANT_CREDITS}
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
      {notice ? (
        <p role="status" className="admin-hint">
          {notice}
        </p>
      ) : null}
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
