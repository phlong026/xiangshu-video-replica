import { useCallback, useEffect, useState } from "react";

import { type AdjustmentListItem, listAdminAdjustments } from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import {
  ADJUSTMENT_SOURCE_LABELS,
  formatDateTime,
  formatFen,
  labelFrom,
} from "./ui/vocabulary";

/**
 * T33 — adjustment history for a specific customer.
 *
 * The page shows all admin adjustments (recharge orders) for a target user,
 * including source document type, reason, amount, credits, and timestamps.
 * All roles (admin/auditor) see the same read-only view (ADM-02).
 *
 * This page is typically accessed from the customer detail view; the userId
 * prop is passed by the parent component.
 */
export function AdjustmentsPage({ userId }: { userId: string }) {
  const [adjustments, setAdjustments] = useState<AdjustmentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);

  const loadAdjustments = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listAdminAdjustments(userId, {
        limit: pageSize,
        offset,
      });
      setAdjustments(response.items);
      setTotal(response.total);
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? `加载失败：${err.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }, [userId, offset, pageSize]);

  useEffect(() => {
    loadAdjustments();
  }, [loadAdjustments]);

  return (
    <div className="adjustments-page">
      <header>
        <h1>调账历史</h1>
        <p className="user-info">用户 ID: {userId}</p>
      </header>

      {loading && <div className="loading">加载中...</div>}

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      {!loading && !error && adjustments.length === 0 && (
        <div className="empty-state">暂无调账记录</div>
      )}

      {!loading && !error && adjustments.length > 0 && (
        <>
          <DataTable
            ariaLabel="调账历史列表"
            headers={
              <>
                <th>来源单类型</th>
                <th>来源单编号</th>
                <th>原因</th>
                <th>金额</th>
                <th>条数</th>
                <th>时间</th>
              </>
            }
          >
            {adjustments.map((adj) => (
              <tr key={adj.adjustment_id}>
                <td>
                  <StatusBadge tone="info">
                    {labelFrom(
                      ADJUSTMENT_SOURCE_LABELS,
                      adj.source_document_type,
                    )}
                  </StatusBadge>
                </td>
                <td>
                  <code>{adj.source_document_ref}</code>
                </td>
                <td>{adj.reason}</td>
                <td className="amount">{formatFen(adj.amount_fen)}</td>
                <td>{adj.credits}</td>
                <td>{formatDateTime(adj.created_at)}</td>
              </tr>
            ))}
          </DataTable>

          <Pagination
            limit={pageSize}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        </>
      )}
    </div>
  );
}
