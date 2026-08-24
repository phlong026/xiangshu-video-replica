import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  type ActivationCodeListItem,
  AdminActivationError,
  adminActivationErrorMessage,
  createIdempotencyKey,
  listActivationCodes,
  resumeActivationCode,
  revokeActivationCode,
  suspendActivationCode,
} from "../api.admin";

type CodeAction = "suspend" | "resume" | "revoke";

const ACTION_LABELS: Record<CodeAction, string> = {
  suspend: "暂停",
  resume: "恢复",
  revoke: "作废",
};

const STATUS_LABELS: Record<string, string> = {
  GENERATED: "已生成",
  ISSUED: "已发放",
  ACTIVE: "已激活",
  SUSPENDED: "已暂停",
  REVOKED: "已作废",
};

/**
 * T32 — activation code listing with the suspend / resume / revoke writes.
 *
 * The list always shows masked codes only; each transition demands a reason
 * plus an explicit confirmation and reports the audit `request_id`, matching
 * the T12 write contract. Auditors (and refreshed read-only sessions) get the
 * same list without any write control.
 */
export function ActivationCodesPage({
  readOnly = false,
  onSessionExpired,
}: {
  readOnly?: boolean;
  onSessionExpired?: () => void;
}) {
  const [items, setItems] = useState<ActivationCodeListItem[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // The inputs are draft values; queries only fire when the operator submits
  // the filter form (applied filters), so typing does not spam the API.
  const [filterBatchId, setFilterBatchId] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [appliedFilters, setAppliedFilters] = useState({
    batch: "",
    status: "",
  });
  const [pending, setPending] = useState<{
    codeId: string;
    action: CodeAction;
  } | null>(null);
  const [actionReason, setActionReason] = useState("");
  const [actionConfirmed, setActionConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  // One idempotency key per logical transition: ambiguous failures (timeout /
  // network) keep the key so a retry replays the T12 server snapshot; a new
  // openAction or a definitive outcome releases it.
  const [actionKey, setActionKey] = useState<string | null>(null);

  const handleFailure = useCallback(
    (cause: unknown, fallback: string) => {
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
        onSessionExpired?.();
        return;
      }
      setError(adminActivationErrorMessage(cause, fallback));
    },
    [onSessionExpired],
  );

  useEffect(() => {
    // Stale-response guard: only the newest applied filters may touch the DOM.
    let cancelled = false;
    void (async () => {
      setError("");
      setNotice("");
      try {
        const page = await listActivationCodes({
          batch_id: appliedFilters.batch || undefined,
          status: appliedFilters.status || undefined,
        });
        if (!cancelled) {
          setItems(page.items);
        }
      } catch (cause) {
        if (cancelled) {
          return;
        }
        if (cause instanceof AdminActivationError && cause.status === 401) {
          setError("会话已失效，请重新登录");
          onSessionExpired?.();
          return;
        }
        setError(adminActivationErrorMessage(cause, "读取激活码列表失败"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [appliedFilters, onSessionExpired]);

  function openAction(codeId: string, action: CodeAction) {
    setPending({ codeId, action });
    setActionReason("");
    setActionConfirmed(false);
    setActionKey(null);
    setError("");
    setNotice("");
  }

  async function submitAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pending) {
      return;
    }
    setError("");
    if (!actionReason.trim()) {
      setError("请填写操作原因");
      return;
    }
    if (!actionConfirmed) {
      setError("请先勾选确认操作");
      return;
    }
    setBusy(true);
    const { codeId, action } = pending;
    const key = actionKey ?? createIdempotencyKey();
    setActionKey(key);
    try {
      const result =
        action === "suspend"
          ? await suspendActivationCode(codeId, actionReason.trim(), key)
          : action === "resume"
            ? await resumeActivationCode(codeId, actionReason.trim(), key)
            : await revokeActivationCode(codeId, actionReason.trim(), key);
      setItems((current) =>
        current.map((item) =>
          item.code_id === result.code_id
            ? { ...item, status: result.status }
            : item,
        ),
      );
      setNotice(
        `已${ACTION_LABELS[action]}（request id: ${result.request_id}）`,
      );
      setPending(null);
      setActionReason("");
      setActionConfirmed(false);
      setActionKey(null);
    } catch (cause) {
      handleFailure(cause, "操作激活码失败");
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        setActionKey(null);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-panel" aria-label="激活码列表">
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
      {notice ? (
        <p className="wallet-notice" role="status">
          {notice}
        </p>
      ) : null}

      <form
        className="admin-filters"
        onSubmit={(event) => {
          event.preventDefault();
          setAppliedFilters({
            batch: filterBatchId.trim(),
            status: filterStatus,
          });
        }}
      >
        <label>
          批次 ID
          <input
            value={filterBatchId}
            onChange={(event) => setFilterBatchId(event.target.value)}
          />
        </label>
        <label>
          状态
          <select
            value={filterStatus}
            onChange={(event) => setFilterStatus(event.target.value)}
          >
            <option value="">全部</option>
            <option value="GENERATED">已生成</option>
            <option value="ISSUED">已发放</option>
            <option value="ACTIVE">已激活</option>
            <option value="SUSPENDED">已暂停</option>
            <option value="REVOKED">已作废</option>
          </select>
        </label>
        <button type="submit">查询</button>
      </form>

      <div className="table-scroll">
        <table className="internal-table">
          <thead>
            <tr>
              <th>码 ID</th>
              <th>掩码码</th>
              <th>状态</th>
              <th>绑定用户</th>
              <th>发放时间</th>
              {readOnly ? null : <th>操作</th>}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.code_id}>
                <td>{item.code_id}</td>
                <td>{item.masked_code}</td>
                <td>{statusLabel(item.status)}</td>
                <td>{item.bound_user_id ?? "—"}</td>
                <td>{item.issued_at ?? "—"}</td>
                {readOnly ? null : (
                  <td>
                    <button
                      type="button"
                      onClick={() => openAction(item.code_id, "suspend")}
                    >
                      暂停
                    </button>
                    <button
                      type="button"
                      onClick={() => openAction(item.code_id, "resume")}
                    >
                      恢复
                    </button>
                    <button
                      type="button"
                      onClick={() => openAction(item.code_id, "revoke")}
                    >
                      作废
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="admin-hint">
        仅显示前 50 条，请用批次 ID 与状态过滤缩小范围。
      </p>

      {pending ? (
        <form className="admin-form" onSubmit={submitAction}>
          <h2>
            {ACTION_LABELS[pending.action]} {pending.codeId}
          </h2>
          <label>
            操作原因
            <input
              placeholder="例如：风控暂停 / 复核通过 / 风控作废"
              value={actionReason}
              onChange={(event) => setActionReason(event.target.value)}
            />
          </label>
          <label>
            <input
              checked={actionConfirmed}
              type="checkbox"
              onChange={(event) => setActionConfirmed(event.target.checked)}
            />
            我已确认操作
          </label>
          <button disabled={busy} type="submit">
            确认{ACTION_LABELS[pending.action]}
          </button>
        </form>
      ) : null}
    </section>
  );
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}
