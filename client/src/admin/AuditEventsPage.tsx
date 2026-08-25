import { type FormEvent, useCallback, useEffect, useState } from "react";

import { AdminAuditError, type AuditLogItem, listAuditLog } from "../api.admin";

/**
 * T34 / ADM-02 — audit event log view.
 *
 * Lists the aggregated audit events (currently ADMIN_ADJUSTMENT rows from
 * the 039 ledger) with pagination and an actor filter. Both admin and
 * auditor roles read the same view (read-only).
 */
export function AuditEventsPage() {
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [actorFilter, setActorFilter] = useState("");

  const loadLog = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listAuditLog({
        actorUserId: actorFilter.trim() || undefined,
        limit: pageSize,
        offset,
      });
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      if (cause instanceof AdminAuditError) {
        setError(`加载失败：${cause.message}`);
      } else if (cause instanceof Error && cause.message) {
        setError(`加载失败：${cause.message}`);
      } else {
        setError("加载失败：未知错误");
      }
    } finally {
      setLoading(false);
    }
  }, [actorFilter, offset, pageSize]);

  useEffect(() => {
    loadLog();
  }, [loadLog]);

  function handleFilter(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
    void loadLog();
  }

  const handleNextPage = () => {
    if (offset + pageSize < total) {
      setOffset(offset + pageSize);
    }
  };

  const handlePrevPage = () => {
    if (offset > 0) {
      setOffset(Math.max(0, offset - pageSize));
    }
  };

  const totalPages = Math.ceil(total / pageSize);

  return (
    <section className="admin-panel" aria-label="审计事件">
      <header>
        <h2>审计事件</h2>
      </header>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <form className="admin-form" onSubmit={handleFilter}>
        <label>
          操作人 ID
          <input
            placeholder="例如：admin_u（留空显示全部）"
            value={actorFilter}
            onChange={(e) => setActorFilter(e.target.value)}
          />
        </label>
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "加载中…" : "筛选"}
        </button>
      </form>

      {items.length === 0 && !loading ? (
        <p className="wallet-notice">暂无审计事件。</p>
      ) : (
        <table className="admin-table" aria-label="审计事件列表">
          <thead>
            <tr>
              <th>时间</th>
              <th>类型</th>
              <th>操作人</th>
              <th>目标客户</th>
              <th>来源单</th>
              <th>原因</th>
              <th>request id</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.event_id}>
                <td>
                  {item.created_at
                    ? new Date(item.created_at).toLocaleString()
                    : "—"}
                </td>
                <td>{item.event_type}</td>
                <td>{item.actor_username || item.actor_user_id}</td>
                <td>{item.target_user_id}</td>
                <td>
                  {item.source_document_type} / {item.source_document_ref}
                </td>
                <td>{item.reason}</td>
                <td>{item.request_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {totalPages > 1 ? (
        <nav className="pagination" aria-label="审计分页">
          <button
            type="button"
            className="btn-secondary"
            onClick={handlePrevPage}
            disabled={offset === 0}
          >
            ← 上一页
          </button>
          <span>
            第 {Math.floor(offset / pageSize) + 1} / {totalPages} 页（共 {total}{" "}
            条）
          </span>
          <button
            type="button"
            className="btn-secondary"
            onClick={handleNextPage}
            disabled={offset + pageSize >= total}
          >
            下一页 →
          </button>
        </nav>
      ) : null}
    </section>
  );
}
