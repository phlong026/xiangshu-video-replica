import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  AdminCustomerError,
  type CustomerListItem,
  listCustomers,
} from "../api.admin";
import { AdjustmentsPage } from "./AdjustmentsPage";

/**
 * T33 — customer list with pagination and filtering.
 *
 * The page shows all registered customers with their activation codes,
 * usernames, and current status. Admin users can navigate through
 * pages and filter by username; auditors see the same read-only view.
 *
 * Each row links to the adjustment history for that customer (ADM-02).
 */
export function CustomersPage() {
  const [customers, setCustomers] = useState<CustomerListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [usernameFilter, setUsernameFilter] = useState("");
  const [detailUserId, setDetailUserId] = useState<string | null>(null);

  const loadCustomers = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listCustomers({
        page,
        page_size: pageSize,
        username_filter: usernameFilter || undefined,
      });
      setCustomers(response.customers);
      setTotal(response.total);
    } catch (err) {
      if (err instanceof AdminCustomerError) {
        setError(`加载失败：${err.message}`);
      } else {
        setError("加载失败：未知错误");
      }
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, usernameFilter]);

  useEffect(() => {
    loadCustomers();
  }, [loadCustomers]);

  const handleFilterSubmit = (e: FormEvent) => {
    e.preventDefault();
    setPage(1); // Reset to first page when filtering
    loadCustomers();
  };

  const handleNextPage = () => {
    if (page < Math.ceil(total / pageSize)) {
      setPage(page + 1);
    }
  };

  const handlePrevPage = () => {
    if (page > 1) {
      setPage(page - 1);
    }
  };

  const totalPages = Math.ceil(total / pageSize);

  if (detailUserId !== null) {
    return (
      <div className="customers-page">
        <header>
          <h1>调账历史</h1>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setDetailUserId(null)}
          >
            ← 返回客户列表
          </button>
        </header>
        <AdjustmentsPage userId={detailUserId} />
      </div>
    );
  }

  const statusLabels: Record<string, string> = {
    active: "活跃",
    suspended: "已暂停",
    revoked: "已作废",
  };

  return (
    <div className="customers-page">
      <header>
        <h1>客户管理</h1>
      </header>

      <form onSubmit={handleFilterSubmit} className="filter-form">
        <input
          type="text"
          placeholder="按用户名筛选"
          value={usernameFilter}
          onChange={(e) => setUsernameFilter(e.target.value)}
        />
        <button type="submit">筛选</button>
      </form>

      {loading && <div className="loading">加载中...</div>}

      {error && <div className="error">{error}</div>}

      {!loading && !error && customers.length === 0 && (
        <div className="empty-state">暂无客户数据</div>
      )}

      {!loading && !error && customers.length > 0 && (
        <>
          <table className="customers-table">
            <thead>
              <tr>
                <th>用户名</th>
                <th>激活码</th>
                <th>注册时间</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {customers.map((customer) => (
                <tr key={customer.user_id}>
                  <td>{customer.username}</td>
                  <td>
                    <code>{customer.activation_code}</code>
                  </td>
                  <td>
                    {new Date(customer.created_at).toLocaleString("zh-CN")}
                  </td>
                  <td>
                    <span className={`status-badge status-${customer.status}`}>
                      {statusLabels[customer.status] || customer.status}
                    </span>
                  </td>
                  <td>
                    <button
                      type="button"
                      onClick={() => setDetailUserId(customer.user_id)}
                    >
                      调账历史
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="pagination">
            <button
              type="button"
              onClick={handlePrevPage}
              disabled={page === 1}
            >
              上一页
            </button>
            <span>
              第 {page} 页 / 共 {totalPages} 页（共 {total} 位客户）
            </span>
            <button
              type="button"
              onClick={handleNextPage}
              disabled={page === totalPages}
            >
              下一页
            </button>
          </div>
        </>
      )}
    </div>
  );
}
