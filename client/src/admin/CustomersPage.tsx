import {
  type FormEvent,
  Fragment,
  useCallback,
  useEffect,
  useState,
} from "react";

import { downloadCustomersCsv } from "../api";
import {
  type CustomerListItem,
  type CustomerUnitPrice,
  createCustomerAdjustment,
  fetchCustomerUnitPrice,
  listCustomers,
  updateCustomerUnitPrice,
} from "../api.admin";
import { AdjustmentsPage } from "./AdjustmentsPage";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { CustomerStatusBadge } from "./ui/StatusBadge";
import { formatDateTime, formatFen, formatYuanFromFen } from "./ui/vocabulary";

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
  /** C1：从客户详情一键进入该客户的会话视图（免手输 UUID）。 */
  onOpenSessions?: (userId: string) => void;
  readOnly?: boolean;
}

export function CustomersPage({
  embedded = false,
  onOpenDevices,
  onOpenSessions,
  readOnly = false,
}: CustomersPageProps = {}) {
  const [customers, setCustomers] = useState<CustomerListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
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
        limit: pageSize,
        offset,
        username_filter: usernameFilter || undefined,
      });
      setCustomers(response.items);
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
  }, [offset, pageSize, usernameFilter]);

  useEffect(() => {
    loadCustomers();
  }, [loadCustomers]);

  const handleFilterSubmit = (e: FormEvent) => {
    e.preventDefault();
    setOffset(0);
    setExpandedUserId(null);
    setUsernameFilter(usernameDraft.trim());
  };

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

  const exportList = () => {
    void downloadCustomersCsv({
      status: statusFilter === "all" ? undefined : statusFilter,
      username: usernameFilter || undefined,
    });
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

      <form className="admin-toolbar" onSubmit={handleFilterSubmit}>
        <label className="admin-toolbar__field">
          <span>用户名筛选</span>
          <input
            placeholder="按用户名筛选"
            type="text"
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
            <option value="revoked">已撤销</option>
          </select>
        </label>
        <button className="admin-toolbar__secondary" type="submit">
          筛选
        </button>
        <button type="button" onClick={exportList}>
          导出列表 CSV
        </button>
      </form>

      <section aria-label="需要处理" className="admin-attention-strip">
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

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

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
            <table
              aria-label="客户列表"
              className="customers-table admin-data-table"
            >
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>激活码</th>
                  <th>注册时间</th>
                  <th>状态</th>
                  <th>生成（成功 / 总数）</th>
                  <th>失败 / 处理中 / 待处理</th>
                  <th>已结算消耗（条）</th>
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
                          {formatDateTime(customer.created_at)}
                        </td>
                        <td data-label="状态">
                          <CustomerStatusBadge status={customer.status} />
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
                                      {formatDateTime(customer.created_at)}
                                    </dd>
                                  </div>
                                  <div>
                                    <dt>当前状态</dt>
                                    <dd>
                                      <CustomerStatusBadge
                                        status={customer.status}
                                      />
                                    </dd>
                                  </div>
                                </dl>
                              </section>
                              <CustomerPriceEditor
                                readOnly={readOnly}
                                userId={customer.user_id}
                              />
                              <FreeCreditsSection
                                readOnly={readOnly}
                                userId={customer.user_id}
                              />
                              <div className="customer-detail-actions">
                                {onOpenSessions ? (
                                  <button
                                    className="btn-secondary"
                                    type="button"
                                    onClick={() =>
                                      onOpenSessions(customer.user_id)
                                    }
                                  >
                                    查看会话
                                  </button>
                                ) : null}
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

          <Pagination
            limit={pageSize}
            noun="位"
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
          {total > pageSize ? null : (
            <p className="admin-hint">共 {total} 位客户。</p>
          )}
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
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // null = dialog closed; number = save that price; "reset" = restore default.
  const [pendingWrite, setPendingWrite] = useState<
    { kind: "save"; unitPriceFen: number } | { kind: "reset" } | null
  >(null);
  const [dialogError, setDialogError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void fetchCustomerUnitPrice(userId)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setPricing(result);
        setPriceYuan(formatYuanFromFen(result.unit_price_fen));
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

  function requestSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const unitPriceFen = yuanInputToFen(priceYuan);
    if (unitPriceFen === null) {
      setError("客户售价必须是大于 0、最多两位小数的金额");
      return;
    }
    setError("");
    setDialogError("");
    setPendingWrite({ kind: "save", unitPriceFen });
  }

  function requestReset() {
    setError("");
    setDialogError("");
    setPendingWrite({ kind: "reset" });
  }

  async function persistPrice(reason: string) {
    if (!pendingWrite || saving) {
      return;
    }
    const unitPriceFen =
      pendingWrite.kind === "save" ? pendingWrite.unitPriceFen : null;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await updateCustomerUnitPrice(
        userId,
        unitPriceFen,
        reason,
      );
      setPricing(updated);
      setPriceYuan(formatYuanFromFen(updated.unit_price_fen));
      setNotice(
        unitPriceFen === null ? "已恢复全局默认售价" : "客户售价已保存",
      );
      setPendingWrite(null);
    } catch (cause) {
      setDialogError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "保存客户售价失败",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="客户售价" className="customer-detail-section">
      <h3>客户售价</h3>
      {loading ? <p className="admin-hint">正在读取客户售价…</p> : null}
      {pricing ? (
        <p className="admin-hint">
          当前 {formatFen(pricing.unit_price_fen)} / 条 · 全局默认{" "}
          {formatFen(pricing.default_unit_price_fen)} / 条
          {pricing.custom_unit_price_fen === null
            ? "（使用默认）"
            : "（独立定价）"}
        </p>
      ) : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!loading && pricing && !readOnly ? (
        <form className="admin-form" onSubmit={requestSave}>
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
          <div className="admin-actions">
            <button disabled={saving} type="submit">
              {saving ? "正在保存" : "保存客户售价"}
            </button>
            <button
              className="btn-secondary"
              disabled={saving || pricing.custom_unit_price_fen === null}
              type="button"
              onClick={requestReset}
            >
              恢复全局默认
            </button>
          </div>
        </form>
      ) : null}
      {!loading && pricing && readOnly ? (
        <p className="admin-hint">审计员仅可查看定价，不能修改。</p>
      ) : null}

      <ConfirmDialog
        busy={saving}
        confirmLabel={
          pendingWrite?.kind === "reset" ? "确认恢复默认" : "确认保存"
        }
        description={
          pendingWrite?.kind === "reset"
            ? "将清除该客户的独立定价，恢复为全局默认售价。原因将写入审计日志。"
            : `将把该客户售价改为 ${pendingWrite ? formatFen(pendingWrite.unitPriceFen) : ""} / 条（仅影响该客户之后的充值换算）。原因将写入审计日志。`
        }
        error={dialogError}
        level="reason"
        open={pendingWrite !== null}
        title={
          pendingWrite?.kind === "reset" ? "恢复全局默认售价" : "修改客户售价"
        }
        onClose={() => {
          setPendingWrite(null);
          setDialogError("");
        }}
        onConfirm={(reason: string) => void persistPrice(reason)}
      />
    </section>
  );
}

/**
 * 免费条数发放（FREE_GRANT，054）：为激活码对应的账号发放免费生成条数。
 * 走 T23 审计调账闭环——账面金额为 0、钱包照增、来源单与原因必填。
 */
function FreeCreditsSection({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  const [credits, setCredits] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [notice, setNotice] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);

  function requestGrant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const creditsNumber = Number.parseInt(credits, 10);
    if (!Number.isFinite(creditsNumber) || creditsNumber <= 0) {
      setDialogError("免费条数必须是大于 0 的整数");
      setDialogOpen(true);
      return;
    }
    if (!sourceRef.trim()) {
      setDialogError("请填写来源单号（运营审批或活动编号）");
      setDialogOpen(true);
      return;
    }
    setDialogError("");
    setDialogOpen(true);
  }

  async function submitGrant(reason: string) {
    if (submitting) {
      return;
    }
    const creditsNumber = Number.parseInt(credits, 10);
    const key = idempotencyKey ?? crypto.randomUUID();
    setIdempotencyKey(key);
    setSubmitting(true);
    try {
      const result = await createCustomerAdjustment(
        userId,
        {
          sourceDocumentType: "FREE_GRANT",
          sourceDocumentRef: sourceRef.trim(),
          credits: creditsNumber,
        },
        reason,
        key,
      );
      setNotice(
        `已发放 ${creditsNumber} 条免费条数（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 条`,
      );
      setCredits("");
      setSourceRef("");
      setIdempotencyKey(null);
      setDialogOpen(false);
      setDialogError("");
    } catch (cause) {
      setDialogError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "发放免费条数失败",
      );
      if (cause instanceof Error && cause.name === "AdminActivationError") {
        // 明确失败释放幂等键；超时等模糊失败保留键以便重试重放。
        setIdempotencyKey(null);
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (readOnly) {
    return (
      <section aria-label="免费条数" className="customer-detail-section">
        <h3>免费条数</h3>
        <p className="admin-hint">审计员仅可查看，不能发放免费条数。</p>
      </section>
    );
  }

  return (
    <section aria-label="免费条数" className="customer-detail-section">
      <h3>免费条数</h3>
      <p className="admin-hint">
        发放的免费条数直接进入该账号钱包，生成视频时与充值条数同等冻结与结算；
        账面金额记 0，来源单号与原因写入审计。
      </p>
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <form className="admin-form" onSubmit={requestGrant}>
        <label>
          发放条数
          <input
            min={1}
            placeholder="例如：10"
            step={1}
            type="number"
            value={credits}
            onChange={(event) => setCredits(event.target.value)}
          />
        </label>
        <label>
          来源单号
          <input
            placeholder="必填，例如：PROMO-2026-09-001"
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
          />
        </label>
        <button type="submit">发放免费条数</button>
      </form>

      <ConfirmDialog
        busy={submitting}
        confirmLabel="确认发放"
        description="免费条数会立即进入客户钱包并可立即用于生成视频。原因将写入审计日志。"
        error={dialogError}
        level="reasonAndAck"
        open={dialogOpen}
        title="发放免费条数"
        onClose={() => {
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={(reason: string) => void submitGrant(reason)}
      />
    </section>
  );
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
