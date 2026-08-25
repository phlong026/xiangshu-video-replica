import { type FormEvent, useState } from "react";

import {
  type ActivationDeliverResult,
  AdminActivationError,
  adminActivationErrorMessage,
  createIdempotencyKey,
  deliverActivationCode,
} from "../api.admin";

/**
 * T32 — the delivery page: record the channel hand-off that flips a GENERATED
 * code to ISSUED. The write carries the channel, optional external order and
 * recipient references, the mandatory reason and confirmation, and surfaces
 * the delivery + audit request ids on success.
 */
export function DeliveriesPage({
  readOnly = false,
  onSessionExpired,
}: {
  readOnly?: boolean;
  onSessionExpired?: () => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [codeId, setCodeId] = useState("");
  const [channel, setChannel] = useState("");
  const [externalOrderRef, setExternalOrderRef] = useState("");
  const [recipientRef, setRecipientRef] = useState("");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [result, setResult] = useState<ActivationDeliverResult | null>(null);
  // One idempotency key per logical delivery, preserved across ambiguous
  // retries: if the server committed GENERATED→ISSUED but the response
  // timed out, retrying with the same key replays the successful result
  // instead of minting a fresh key that answers a transition error.
  const [deliverKey, setDeliverKey] = useState<string | null>(null);

  async function submitDeliver(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!codeId.trim()) {
      setError("请填写激活码 ID");
      return;
    }
    if (!channel.trim()) {
      setError("请填写发放渠道");
      return;
    }
    if (!reason.trim()) {
      setError("请填写发放原因");
      return;
    }
    if (!confirmed) {
      setError("请先勾选确认发放");
      return;
    }
    setBusy(true);
    const key = deliverKey ?? createIdempotencyKey();
    setDeliverKey(key);
    try {
      const delivered = await deliverActivationCode(
        codeId.trim(),
        {
          channel: channel.trim(),
          external_order_ref: externalOrderRef.trim() || undefined,
          recipient_ref: recipientRef.trim() || undefined,
          reason: reason.trim(),
        },
        key,
      );
      setResult(delivered);
      // The logical delivery finished: the next submission is a new one.
      setDeliverKey(null);
    } catch (cause) {
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
        onSessionExpired?.();
        return;
      }
      setError(
        adminActivationErrorMessage(cause, "发放激活码失败", {
          CODE_TRANSITION_INVALID: "仅已生成的激活码可以发放",
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-panel" aria-label="激活码发放">
      {readOnly ? (
        <p className="wallet-notice" role="status">
          当前为只读模式，写操作不可用。
        </p>
      ) : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <form className="admin-form" onSubmit={submitDeliver}>
        <h2>发放激活码</h2>
        <label>
          激活码 ID
          <input
            placeholder="例如：code-1"
            value={codeId}
            onChange={(event) => setCodeId(event.target.value)}
          />
        </label>
        <label>
          发放渠道
          <input
            placeholder="例如：offline / email / 渠道商平台"
            value={channel}
            onChange={(event) => setChannel(event.target.value)}
          />
        </label>
        <label>
          外部订单号（可选）
          <input
            value={externalOrderRef}
            onChange={(event) => setExternalOrderRef(event.target.value)}
          />
        </label>
        <label>
          收件人引用（可选）
          <input
            value={recipientRef}
            onChange={(event) => setRecipientRef(event.target.value)}
          />
        </label>
        <label>
          发放原因
          <input
            placeholder="例如：线下渠道发货"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <label>
          <input
            checked={confirmed}
            type="checkbox"
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          我已确认发放
        </label>
        <button disabled={readOnly || busy} type="submit">
          发放
        </button>
      </form>

      {result ? (
        <div className="admin-result">
          <h3>发放成功</h3>
          <p>
            激活码：<code>{result.code_id}</code>
          </p>
          <p>
            状态：
            <code>{result.status === "ISSUED" ? "已发放" : result.status}</code>
          </p>
          <p>
            发放记录：<code>{result.delivery_id}</code>
          </p>
          <p>
            request id：<code>{result.request_id}</code>
          </p>
        </div>
      ) : null}
    </section>
  );
}
