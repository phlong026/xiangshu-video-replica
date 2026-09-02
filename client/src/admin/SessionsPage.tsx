import { type FormEvent, useState } from "react";

import {
  type AdjustmentWriteInput,
  AdminActivationError,
  type CustomerSessionListItem,
  createCustomerAdjustment,
  listCustomerSessions,
  listLiveSessions,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import {
  ADJUSTMENT_SOURCE_LABELS,
  formatDateTime,
  labelFrom,
} from "./ui/vocabulary";

/** 加款/免费发放可选的来源单类型（与 054 服务端枚举一致）。 */
const SOURCE_DOCUMENT_OPTIONS = [
  "CS_TICKET",
  "FREE_GRANT",
  "COMPENSATION_APPROVAL",
  "REFUND_APPROVAL",
  "LEDGER_CORRECTION",
] as const;

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
  const [sourceType, setSourceType] = useState<string>("CS_TICKET");
  const [sourceRef, setSourceRef] = useState("");
  const [writeNotice, setWriteNotice] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [writeError, setWriteError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // A11：全部在线会话总览模式（无需先知道客户 ID）。
  const [liveMode, setLiveMode] = useState(false);
  const [liveItems, setLiveItems] = useState<CustomerSessionListItem[]>([]);
  const [liveTotal, setLiveTotal] = useState(0);
  const [liveOffset, setLiveOffset] = useState(0);

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
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }

  function handleQuery(e: FormEvent) {
    e.preventDefault();
    setWriteNotice("");
    setLiveMode(false);
    void load(queryUserId);
  }

  async function loadLive(offsetToLoad: number) {
    setLoading(true);
    setError("");
    try {
      const response = await listLiveSessions({
        limit: 50,
        offset: offsetToLoad,
      });
      setLiveItems(response.items);
      setLiveTotal(response.total);
      setLiveOffset(offsetToLoad);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }

  function openLiveMode() {
    setLiveMode(true);
    setLiveOffset(0);
    void loadLive(0);
  }

  function focusCustomer(userId: string) {
    setQueryUserId(userId);
    setLiveMode(false);
    void load(userId);
  }

  function handleAdjustSubmit(e: FormEvent) {
    e.preventDefault();
    setWriteNotice("");
    setWriteError("");
    if (!activeUserId) {
      return;
    }
    const creditsNumber = Number.parseInt(credits, 10);
    if (!Number.isFinite(creditsNumber) || creditsNumber <= 0) {
      setError("请输入大于 0 的加款条数");
      return;
    }
    if (!sourceRef.trim()) {
      setError("请填写来源单号");
      return;
    }
    // 原因与"我已知晓"在确认对话框里收集——直接改钱包余额属高危操作。
    setConfirmOpen(true);
  }

  async function submitAdjustment(reason: string) {
    if (!activeUserId || submitting) {
      return;
    }
    const creditsNumber = Number.parseInt(credits, 10);
    const input: AdjustmentWriteInput = {
      sourceDocumentType: sourceType,
      sourceDocumentRef: sourceRef.trim(),
      credits: creditsNumber,
    };
    const key = adjustKey ?? crypto.randomUUID();
    setAdjustKey(key);
    setSubmitting(true);
    try {
      const result = await createCustomerAdjustment(
        activeUserId,
        input,
        reason,
        key,
      );
      setWriteNotice(
        `调账成功（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 条`,
      );
      setCredits("");
      setSourceRef("");
      setAdjustKey(null);
      setConfirmOpen(false);
      // Refresh the session view (the wallet changed, the session row may
      // surface a new lease).
      void load(activeUserId);
    } catch (cause) {
      setWriteError(
        cause instanceof Error && cause.message
          ? cause.message
          : "调账失败：未知错误",
      );
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        // A definitive failure releases the key; an ambiguous one (timeout /
        // network) keeps it so the retry replays instead of double-charging.
        setAdjustKey(null);
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section aria-label="客户会话" className="admin-panel">
      <header>
        <h2>客户会话</h2>
      </header>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {writeNotice ? (
        <PageBanner tone="notice">{writeNotice}</PageBanner>
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
          <button disabled={loading} type="submit">
            {loading ? "查询中…" : "查询会话"}
          </button>
          <button type="button" onClick={() => void openLiveMode()}>
            {liveMode ? "刷新在线会话" : "查看全部在线会话"}
          </button>
        </form>
      )}

      {liveMode ? (
        <>
          <ul aria-label="在线会话列表" className="session-list">
            {liveItems.map((item) => (
              <li className="session-item" key={item.session_id}>
                <div className="session-meta">
                  <strong>会话 {item.session_id}</strong>
                  <span>客户 {item.username}</span>
                  <span>设备 {item.device_name ?? item.device_id}</span>
                  <span>租约到期 {formatDateTime(item.lease_until)}</span>
                  <button
                    type="button"
                    onClick={() => focusCustomer(item.user_id)}
                  >
                    查看此客户
                  </button>
                </div>
              </li>
            ))}
          </ul>
          <Pagination
            limit={50}
            offset={liveOffset}
            total={liveTotal}
            onPageChange={(next) => void loadLive(next)}
          />
        </>
      ) : null}

      {items.length === 0 && !loading ? (
        <PageBanner tone="notice">该客户当前没有活动会话。</PageBanner>
      ) : (
        <ul aria-label="会话列表" className="session-list">
          {items.map((item) => (
            <li className="session-item" key={item.session_id}>
              <div className="session-meta">
                <strong>会话 {item.session_id}</strong>
                <span>客户 {item.username}</span>
                <span>设备 {item.device_name ?? item.device_id}</span>
                <span>槽位 #{item.slot_no}</span>
                <span>状态 {item.device_status}</span>
                <span>租约到期 {formatDateTime(item.lease_until)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {total > items.length ? (
        <PageBanner tone="notice">
          共 {total} 条，当前显示 {items.length} 条。
        </PageBanner>
      ) : null}

      {activeUserId && !readOnly ? (
        <form className="admin-form" onSubmit={handleAdjustSubmit}>
          <h3>后台加款</h3>
          <PageBanner tone="notice">目标客户：{activeUserId}</PageBanner>
          <label>
            加款条数
            <input
              min={1}
              placeholder="例如：100"
              step={1}
              type="number"
              value={credits}
              onChange={(e) => setCredits(e.target.value)}
            />
          </label>
          <label>
            来源单类型
            <select
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value)}
            >
              {SOURCE_DOCUMENT_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {labelFrom(ADJUSTMENT_SOURCE_LABELS, option)}
                </option>
              ))}
            </select>
          </label>
          <label>
            来源单号
            <input
              placeholder="必填，例如：manual-20260902-001"
              value={sourceRef}
              onChange={(e) => setSourceRef(e.target.value)}
            />
          </label>
          <button type="submit">执行后台加款</button>
        </form>
      ) : null}

      <ConfirmDialog
        busy={submitting}
        confirmLabel="确认加款"
        description={
          <>
            即将直接为客户 {activeUserId ?? ""} 的钱包增加 {credits || "0"}{" "}
            条。该操作立即生效并写入审计，请确认来源单与金额无误。
          </>
        }
        error={writeError}
        level="reasonAndAck"
        open={confirmOpen}
        title="确认后台加款"
        onClose={() => {
          setConfirmOpen(false);
          setWriteError("");
        }}
        onConfirm={(reason: string) => void submitAdjustment(reason)}
      />
    </section>
  );
}
