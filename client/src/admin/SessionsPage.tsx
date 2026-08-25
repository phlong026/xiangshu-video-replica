import { type FormEvent, useState } from "react";

import {
  type AdjustmentWriteInput,
  AdminAdjustmentError,
  AdminSessionError,
  type CustomerSessionListItem,
  createCustomerAdjustment,
  listCustomerSessions,
} from "../api.admin";

/**
 * T34 / ADM-02 — live customer session view.
 *
 * Queries the 029 one-row-per-user session state through the T34 endpoint
 * and offers the T23 background adjustment write (the Codex review P2
 * "no client call for the existing adjustment POST route" gap). Auditor
 * sessions are read-only: the write form stays hidden for them via the
 * shared admin session role, and the server answers AUDITOR_READ_ONLY
 * anyway as defense in depth.
 */
export function SessionsPage({
  userId,
  readOnly = false,
}: {
  userId?: string;
  readOnly?: boolean;
}) {
  const [queryUserId, setQueryUserId] = useState(userId ?? "");
  const [items, setItems] = useState<CustomerSessionListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // The customer the adjustment form targets: set once a query succeeds,
  // so the T23 write entry is reachable from the sessions tab.
  const [activeUserId, setActiveUserId] = useState<string | null>(
    userId ?? null,
  );

  // Adjustment write state (T23 / BILL-02): one idempotency key per logical
  // write, preserved across ambiguous retries like the other admin pages.
  const [adjustKey, setAdjustKey] = useState<string | null>(null);
  const [credits, setCredits] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [reason, setReason] = useState("");
  const [writeNotice, setWriteNotice] = useState("");

  async function load(userIdToLoad: string) {
    if (!userIdToLoad.trim()) {
      setItems([]);
      setTotal(0);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const response = await listCustomerSessions(userIdToLoad.trim(), {
        limit: 50,
      });
      setItems(response.items);
      setTotal(response.total);
      setActiveUserId(userIdToLoad.trim());
    } catch (cause) {
      if (cause instanceof AdminSessionError) {
        setError(`加载失败：${cause.message}`);
      } else if (cause instanceof Error && cause.message) {
        setError(`加载失败：${cause.message}`);
      } else {
        setError("加载失败：未知错误");
      }
    } finally {
      setLoading(false);
    }
  }

  function handleQuery(e: FormEvent) {
    e.preventDefault();
    setWriteNotice("");
    void load(queryUserId);
  }

  async function submitAdjustment(e: FormEvent) {
    e.preventDefault();
    setWriteNotice("");
    if (!activeUserId) {
      return;
    }
    const creditsNumber = Number.parseInt(credits, 10);
    if (!Number.isFinite(creditsNumber) || creditsNumber <= 0) {
      setError("请输入大于 0 的加款积分");
      return;
    }
    if (!sourceRef.trim()) {
      setError("请填写来源单号");
      return;
    }
    if (!reason.trim()) {
      setError("请填写加款原因");
      return;
    }
    const input: AdjustmentWriteInput = {
      sourceDocumentType: "CS_TICKET",
      sourceDocumentRef: sourceRef.trim(),
      credits: creditsNumber,
    };
    const key = adjustKey ?? crypto.randomUUID();
    setAdjustKey(key);
    try {
      const result = await createCustomerAdjustment(
        activeUserId,
        input,
        reason.trim(),
        key,
      );
      setWriteNotice(
        `调账成功（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 积分`,
      );
      setCredits("");
      setSourceRef("");
      setReason("");
      setAdjustKey(null);
      // Refresh the session view (the wallet changed, the session row may
      // surface a new lease).
      void load(activeUserId);
    } catch (cause) {
      if (cause instanceof AdminAdjustmentError) {
        setError(`调账失败：${cause.message}`);
      } else if (cause instanceof Error && cause.message) {
        setError(`调账失败：${cause.message}`);
      } else {
        setError("调账失败：未知错误");
      }
    }
  }

  return (
    <section className="admin-panel" aria-label="客户会话">
      <header>
        <h2>客户会话</h2>
      </header>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {writeNotice ? (
        <p className="wallet-notice" role="status">
          {writeNotice}
        </p>
      ) : null}

      {userId ? null : (
        <form className="admin-form" onSubmit={handleQuery}>
          <label>
            客户 ID
            <input
              placeholder="例如：customer_u"
              value={queryUserId}
              onChange={(e) => setQueryUserId(e.target.value)}
            />
          </label>
          <button type="submit" className="btn-primary" disabled={loading}>
            {loading ? "查询中…" : "查询会话"}
          </button>
        </form>
      )}

      {items.length === 0 && !loading ? (
        <p className="wallet-notice">该客户当前没有活动会话。</p>
      ) : (
        <ul className="session-list" aria-label="会话列表">
          {items.map((item) => (
            <li key={item.session_id} className="session-item">
              <div className="session-meta">
                <strong>会话 {item.session_id}</strong>
                <span>客户 {item.username}</span>
                <span>设备 {item.device_name ?? item.device_id}</span>
                <span>槽位 #{item.slot_no}</span>
                <span>状态 {item.device_status}</span>
                <span>
                  租约到期{" "}
                  {item.lease_until
                    ? new Date(item.lease_until).toLocaleString()
                    : "—"}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {total > items.length ? (
        <p className="wallet-notice">
          共 {total} 条，当前显示 {items.length} 条。
        </p>
      ) : null}

      {activeUserId && !readOnly ? (
        <form className="admin-form" onSubmit={submitAdjustment}>
          <h3>后台加款（T23 / BILL-02）</h3>
          <p className="wallet-notice">目标客户：{activeUserId}</p>
          <label>
            加款积分
            <input
              type="number"
              min={1}
              step={1}
              placeholder="例如：100"
              value={credits}
              onChange={(e) => setCredits(e.target.value)}
            />
          </label>
          <label>
            来源单号
            <input
              placeholder="例如：manual-20260825-001"
              value={sourceRef}
              onChange={(e) => setSourceRef(e.target.value)}
            />
          </label>
          <label>
            加款原因
            <input
              placeholder="必填，例如：客户电话反馈补发"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <button type="submit" className="btn-primary">
            执行后台加款
          </button>
        </form>
      ) : null}
    </section>
  );
}
