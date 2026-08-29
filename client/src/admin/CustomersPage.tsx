import {
  type FormEvent,
  Fragment,
  useCallback,
  useEffect,
  useState,
} from "react";

import {
  AdminCustomerError,
  type CustomerListItem,
  type CustomerUnitPrice,
  fetchCustomerUnitPrice,
  listCustomers,
  updateCustomerUnitPrice,
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
interface CustomersPageProps {
  embedded?: boolean;
  onOpenDevices?: () => void;
  readOnly?: boolean;
}

export function CustomersPage({
  embedded = false,
  onOpenDevices,
  readOnly = false,
}: CustomersPageProps = {}) {
  const [customers, setCustomers] = useState<CustomerListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [usernameDraft, setUsernameDraft] = useState("");
  const [usernameFilter, setUsernameFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [detailUserId, setDetailUserId] = useState<string | null>(null);
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);

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
    setPage(1);
    setExpandedUserId(null);
    setUsernameFilter(usernameDraft.trim());
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

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

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
    ACTIVE: "活跃",
    suspended: "已暂停",
    SUSPENDED: "已暂停",
    revoked: "已作废",
    REVOKED: "已作废",
  };

  const failedGenerations = customers.reduce(
    (sum, customer) => sum + (customer.generation_failed ?? 0),
    0,
  );
  const attentionGenerations = customers.reduce(
    (sum, customer) => sum + (customer.generation_attention ?? 0),
    0,
  );
  const inProgressGenerations = customers.reduce(
    (sum, customer) => sum + (customer.generation_in_progress ?? 0),
    0,
  );
  const visibleCustomers = customers.filter(
    (customer) =>
      statusFilter === "all" || customer.status.toLowerCase() === statusFilter,
  );

  const exportVisibleCustomers = () => {
    const rows = [
      [
        "用户名",
        "激活码",
        "注册时间",
        "状态",
        "成功生成",
        "生成总数",
        "失败生成",
        "处理中",
        "待关注",
        "已结算消耗",
      ],
      ...visibleCustomers.map((customer) => [
        customer.username,
        customer.activation_code,
        new Date(customer.created_at).toLocaleString("zh-CN"),
        statusLabels[customer.status] || customer.status,
        customer.generation_succeeded ?? 0,
        customer.generation_total ?? 0,
        customer.generation_failed ?? 0,
        customer.generation_in_progress ?? 0,
        customer.generation_attention ?? 0,
        customer.credits_spent ?? 0,
      ]),
    ];
    const csv = rows.map((row) => row.map(escapeCsvCell).join(",")).join("\n");
    const downloadUrl = URL.createObjectURL(
      new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    anchor.href = downloadUrl;
    anchor.download = `customers-${new Date().toISOString().slice(0, 10)}.csv`;
    anchor.click();
    URL.revokeObjectURL(downloadUrl);
  };

  return (
    <div className="customers-page">
      {!embedded ? (
        <header className="admin-page-header">
          <h1>客户管理</h1>
          <p>
            聚焦客户状态、生成表现和消耗情况，支持在同一页快速查看运营详情。
          </p>
        </header>
      ) : null}

      <form onSubmit={handleFilterSubmit} className="admin-toolbar">
        <label className="admin-toolbar__field">
          <span>用户名筛选</span>
          <input
            type="text"
            placeholder="按用户名筛选"
            value={usernameDraft}
            onChange={(e) => setUsernameDraft(e.target.value)}
          />
        </label>
        <label className="admin-toolbar__field admin-toolbar__field--select">
          <span>客户状态</span>
          <select
            aria-label="客户状态"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="all">全部状态</option>
            <option value="active">活跃</option>
            <option value="suspended">已暂停</option>
            <option value="revoked">已作废</option>
          </select>
        </label>
        <button className="admin-toolbar__secondary" type="submit">
          筛选
        </button>
        <button type="button" onClick={exportVisibleCustomers}>
          导出列表
        </button>
      </form>

      <section className="admin-attention-strip" aria-label="需要处理">
        <strong>需要处理</strong>
        <span>
          失败生成 <b>{failedGenerations}</b>
        </span>
        <span>
          待关注生成 <b>{attentionGenerations}</b>
        </span>
        <span>
          处理中 <b>{inProgressGenerations}</b>
        </span>
        <small>数据范围：当前筛选页</small>
      </section>

      {loading && <div className="loading">加载中...</div>}

      {error && <div className="error">{error}</div>}

      {!loading && !error && customers.length === 0 && (
        <div className="empty-state">暂无客户数据</div>
      )}

      {!loading &&
      !error &&
      customers.length > 0 &&
      visibleCustomers.length === 0 ? (
        <div className="empty-state">当前状态下暂无客户</div>
      ) : null}

      {!loading && !error && visibleCustomers.length > 0 && (
        <>
          <div className="table-scroll admin-table-card">
            <table className="customers-table admin-data-table">
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>激活码</th>
                  <th>注册时间</th>
                  <th>状态</th>
                  <th>生成（成功 / 总数）</th>
                  <th>失败 / 处理中 / 待处理</th>
                  <th>已结算消耗</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visibleCustomers.map((customer) => {
                  const isExpanded = expandedUserId === customer.user_id;
                  const detailId = `customer-detail-${customer.user_id}`;
                  return (
                    <Fragment key={customer.user_id}>
                      <tr className={isExpanded ? "is-expanded" : undefined}>
                        <td data-label="用户名">{customer.username}</td>
                        <td data-label="激活码">
                          <code>{customer.activation_code}</code>
                        </td>
                        <td data-label="注册时间">
                          {new Date(customer.created_at).toLocaleString(
                            "zh-CN",
                          )}
                        </td>
                        <td data-label="状态">
                          <span
                            className={`status-badge status-${customer.status}`}
                          >
                            {statusLabels[customer.status] || customer.status}
                          </span>
                        </td>
                        <td data-label="生成（成功 / 总数）">
                          {customer.generation_succeeded ?? 0} /{" "}
                          {customer.generation_total ?? 0}
                        </td>
                        <td data-label="失败 / 处理中 / 待处理">
                          {customer.generation_failed ?? 0} /{" "}
                          {customer.generation_in_progress ?? 0} /{" "}
                          {customer.generation_attention ?? 0}
                        </td>
                        <td
                          aria-label={`${customer.username} 已结算消耗`}
                          data-label="已结算消耗"
                        >
                          {customer.credits_spent ?? 0}
                        </td>
                        <td data-label="操作">
                          <button
                            aria-controls={detailId}
                            aria-expanded={isExpanded}
                            type="button"
                            onClick={() =>
                              setExpandedUserId((current) =>
                                current === customer.user_id
                                  ? null
                                  : customer.user_id,
                              )
                            }
                          >
                            {isExpanded ? "收起详情" : "展开详情"}
                          </button>
                        </td>
                      </tr>
                      {isExpanded ? (
                        <tr className="admin-detail-row">
                          <td colSpan={8}>
                            <section
                              className="customer-detail-panel"
                              id={detailId}
                            >
                              <div className="customer-detail-panel__header">
                                <div>
                                  <h2>{customer.username} 运营详情</h2>
                                  <p>
                                    查看当前客户的使用表现、消耗节奏与激活信息。
                                  </p>
                                </div>
                                <button
                                  aria-expanded="true"
                                  type="button"
                                  onClick={() => setExpandedUserId(null)}
                                >
                                  收起详情
                                </button>
                              </div>
                              <section className="customer-detail-section">
                                <h3>生成与消耗</h3>
                                <div className="customer-detail-grid">
                                  <article className="customer-detail-metric">
                                    <span>累计生成</span>
                                    <strong>
                                      {customer.generation_total ?? 0} 条
                                    </strong>
                                  </article>
                                  <article className="customer-detail-metric">
                                    <span>成功产出</span>
                                    <strong>
                                      {customer.generation_succeeded ?? 0} 条
                                    </strong>
                                  </article>
                                  <article className="customer-detail-metric">
                                    <span>异常关注</span>
                                    <strong>
                                      {customer.generation_attention ?? 0} 条
                                    </strong>
                                  </article>
                                  <article className="customer-detail-metric">
                                    <span>已结算消耗</span>
                                    <strong>
                                      {customer.credits_spent ?? 0} 条
                                    </strong>
                                  </article>
                                </div>
                              </section>
                              <section className="customer-detail-section">
                                <h3>授权与账户</h3>
                                <dl className="customer-detail-meta">
                                  <div>
                                    <dt>激活码</dt>
                                    <dd>
                                      <code>{customer.activation_code}</code>
                                    </dd>
                                  </div>
                                  <div>
                                    <dt>注册时间</dt>
                                    <dd>
                                      {new Date(
                                        customer.created_at,
                                      ).toLocaleString("zh-CN")}
                                    </dd>
                                  </div>
                                  <div>
                                    <dt>当前状态</dt>
                                    <dd>
                                      {statusLabels[customer.status] ||
                                        customer.status}
                                    </dd>
                                  </div>
                                </dl>
                              </section>
                              <CustomerPriceEditor
                                readOnly={readOnly}
                                userId={customer.user_id}
                              />
                              <div className="customer-detail-actions">
                                {onOpenDevices ? (
                                  <button
                                    className="btn-secondary"
                                    type="button"
                                    onClick={onOpenDevices}
                                  >
                                    查看设备
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  onClick={() =>
                                    setDetailUserId(customer.user_id)
                                  }
                                >
                                  调账历史
                                </button>
                              </div>
                            </section>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

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

function CustomerPriceEditor({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  const [pricing, setPricing] = useState<CustomerUnitPrice | null>(null);
  const [priceYuan, setPriceYuan] = useState("");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    void fetchCustomerUnitPrice(userId)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setPricing(result);
        setPriceYuan(formatFenAsYuanInput(result.unit_price_fen));
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message.trim()
              ? cause.message
              : "读取客户单价失败",
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  async function savePrice(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const unitPriceFen = yuanInputToFen(priceYuan);
    if (unitPriceFen === null) {
      setError("客户售价必须是大于 0、最多两位小数的金额");
      return;
    }
    if (!reason.trim()) {
      setError("请填写修改客户售价的原因");
      return;
    }
    await persistPrice(unitPriceFen);
  }

  async function resetPrice() {
    if (!reason.trim()) {
      setError("请先填写恢复默认售价的原因");
      return;
    }
    await persistPrice(null);
  }

  async function persistPrice(unitPriceFen: number | null) {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await updateCustomerUnitPrice(
        userId,
        unitPriceFen,
        reason.trim(),
      );
      setPricing(updated);
      setPriceYuan(formatFenAsYuanInput(updated.unit_price_fen));
      setReason("");
      setNotice(
        unitPriceFen === null ? "已恢复全局默认售价" : "客户售价已保存",
      );
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "保存客户售价失败",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="customer-detail-section" aria-label="客户售价">
      <h3>客户售价</h3>
      {loading ? <p className="admin-hint">正在读取客户售价…</p> : null}
      {pricing ? (
        <p className="admin-hint">
          当前 {formatFenAsYuan(pricing.unit_price_fen)} 元/条 · 全局默认{" "}
          {formatFenAsYuan(pricing.default_unit_price_fen)} 元/条
          {pricing.custom_unit_price_fen === null
            ? "（使用默认）"
            : "（独立定价）"}
        </p>
      ) : null}
      {error ? <p role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {!loading && pricing && !readOnly ? (
        <form className="admin-form" onSubmit={savePrice}>
          <label>
            售价（元/条）
            <input
              inputMode="decimal"
              min="0.01"
              step="0.01"
              type="number"
              value={priceYuan}
              onChange={(event) => setPriceYuan(event.target.value)}
            />
          </label>
          <label>
            修改原因
            <input
              placeholder="必填，将写入审计日志"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="admin-actions">
            <button disabled={saving} type="submit">
              {saving ? "正在保存" : "保存客户售价"}
            </button>
            <button
              className="btn-secondary"
              disabled={saving || pricing.custom_unit_price_fen === null}
              type="button"
              onClick={() => void resetPrice()}
            >
              恢复全局默认
            </button>
          </div>
        </form>
      ) : null}
      {!loading && pricing && readOnly ? (
        <p className="admin-hint">审计员仅可查看定价，不能修改。</p>
      ) : null}
    </section>
  );
}

function formatFenAsYuan(value: number): string {
  return (value / 100).toFixed(2);
}

function formatFenAsYuanInput(value: number): string {
  return String(value / 100);
}

function yuanInputToFen(value: string): number | null {
  const normalized = value.trim();
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(normalized)) {
    return null;
  }
  const [yuan, cents = ""] = normalized.split(".");
  const result = Number(yuan) * 100 + Number(cents.padEnd(2, "0"));
  return Number.isSafeInteger(result) && result > 0 ? result : null;
}

function escapeCsvCell(value: string | number): string {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}
