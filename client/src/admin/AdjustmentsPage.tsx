import { useCallback, useEffect, useState } from "react";

import {
  type AdjustmentListItem,
  AdminAdjustmentError,
  listAdminAdjustments,
} from "../api.admin";

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
      if (err instanceof AdminAdjustmentError) {
        setError(`加载失败：${err.message}`);
      } else {
        setError("加载失败：未知错误");
      }
    } finally {
      setLoading(false);
    }
  }, [userId, offset, pageSize]);

  useEffect(() => {
    loadAdjustments();
  }, [loadAdjustments]);

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
  const currentPage = Math.floor(offset / pageSize) + 1;

  const sourceDocumentLabels: Record<string, string> = {
    CS_TICKET: "客服工单",
    REFUND_APPROVAL: "退款审批",
    COMPENSATION_APPROVAL: "补偿审批",
    LEDGER_CORRECTION: "账本更正",
  };

  const formatAmount = (amountFen: number): string => {
    const yuan = amountFen / 100;
    return `¥${yuan.toFixed(2)}`;
  };

  return (
    <div className="adjustments-page">
      <header>
        <h1>调账历史</h1>
        <p className="user-info">用户 ID: {userId}</p>
      </header>

      {loading && <div className="loading">加载中...</div>}

      {error && <div className="error">{error}</div>}

      {!loading && !error && adjustments.length === 0 && (
        <div className="empty-state">暂无调账记录</div>
      )}

      {!loading && !error && adjustments.length > 0 && (
        <>
          <table className="adjustments-table">
            <thead>
              <tr>
                <th>来源单类型</th>
                <th>来源单编号</th>
                <th>原因</th>
                <th>金额</th>
                <th>积分</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {adjustments.map((adj) => (
                <tr key={adj.adjustment_id}>
                  <td>
                    <span
                      className={`source-badge source-${adj.source_document_type}`}
                    >
                      {sourceDocumentLabels[adj.source_document_type] ||
                        adj.source_document_type}
                    </span>
                  </td>
                  <td>
                    <code>{adj.source_document_ref}</code>
                  </td>
                  <td>{adj.reason}</td>
                  <td className="amount">{formatAmount(adj.amount_fen)}</td>
                  <td>{adj.credits}</td>
                  <td>{new Date(adj.created_at).toLocaleString("zh-CN")}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="pagination">
            <button
              type="button"
              onClick={handlePrevPage}
              disabled={offset === 0}
            >
              上一页
            </button>
            <span>
              第 {currentPage} 页 / 共 {totalPages} 页（共 {total} 条记录）
            </span>
            <button
              type="button"
              onClick={handleNextPage}
              disabled={offset + pageSize >= total}
            >
              下一页
            </button>
          </div>
        </>
      )}
    </div>
  );
}
