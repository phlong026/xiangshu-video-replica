import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type AuditLogItem, listAuditLog } from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime } from "./ui/vocabulary";

/**
 * T34 / ADM-02 — audit event log view.
 *
 * Lists the aggregated audit events (currently ADMIN_ADJUSTMENT rows from
 * the 039 ledger) with pagination and an actor filter. Both admin and
 * auditor roles read the same view (read-only).
 *
 * 筛选只在提交时生效：输入框改动不触发请求（整改清单 评估登记 5 的
 * "输入即加载 + 点击再发一次"重复请求问题在此收口）。
 */
export function AuditEventsPage() {
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [actorDraft, setActorDraft] = useState("");
  const [targetDraft, setTargetDraft] = useState("");
  const [typeDraft, setTypeDraft] = useState("");
  const [fromDraft, setFromDraft] = useState("");
  const [toDraft, setToDraft] = useState("");
  const [filters, setFilters] = useState<{
    actor: string;
    target: string;
    eventType: string;
    from: string;
    to: string;
  }>({ actor: "", target: "", eventType: "", from: "", to: "" });

  const loadLog = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listAuditLog({
        actorUserId: filters.actor || undefined,
        targetUserId: filters.target || undefined,
        eventType: filters.eventType || undefined,
        createdFrom: filters.from || undefined,
        createdTo: filters.to || undefined,
        limit: pageSize,
        offset,
      });
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }, [filters, offset, pageSize]);

  useEffect(() => {
    void loadLog();
  }, [loadLog]);

  function handleFilter(event: FormEvent) {
    event.preventDefault();
    // offset 归零与筛选词提交合入同一批次，effect 只会跑一次。
    setOffset(0);
    setFilters({
      actor: actorDraft.trim(),
      target: targetDraft.trim(),
      eventType: typeDraft.trim().toUpperCase(),
      from: fromDraft.trim(),
      to: toDraft.trim(),
    });
  }

  return (
    <section aria-label="审计事件" className="admin-panel">
      <header>
        <h2>审计事件</h2>
      </header>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      <form className="admin-form" onSubmit={handleFilter}>
        <label>
          事件类型
          <input
            placeholder="例如：ADMIN_ADJUSTMENT（留空显示全部）"
            value={typeDraft}
            onChange={(event) => setTypeDraft(event.target.value)}
          />
        </label>
        <label>
          操作人 ID
          <input
            placeholder="例如：admin_u"
            value={actorDraft}
            onChange={(event) => setActorDraft(event.target.value)}
          />
        </label>
        <label>
          目标客户 ID
          <input
            placeholder="例如：customer_u"
            value={targetDraft}
            onChange={(event) => setTargetDraft(event.target.value)}
          />
        </label>
        <label>
          起始时间
          <input
            placeholder="例如：2026-09-01"
            value={fromDraft}
            onChange={(event) => setFromDraft(event.target.value)}
          />
        </label>
        <label>
          截止时间
          <input
            placeholder="例如：2026-09-30"
            value={toDraft}
            onChange={(event) => setToDraft(event.target.value)}
          />
        </label>
        <button disabled={loading} type="submit">
          {loading ? "加载中…" : "筛选"}
        </button>
      </form>

      {items.length === 0 && !loading ? (
        <PageBanner tone="notice">暂无审计事件。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="审计事件列表"
          headers={
            <>
              <th>时间</th>
              <th>类型</th>
              <th>操作人</th>
              <th>目标客户</th>
              <th>来源单</th>
              <th>原因</th>
              <th>request id</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.event_id}>
              <td>{formatDateTime(item.created_at)}</td>
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
        </DataTable>
      )}

      <Pagination
        disabled={loading}
        limit={pageSize}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />
    </section>
  );
}
