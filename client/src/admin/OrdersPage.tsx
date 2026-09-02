import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type ControlRechargeOrder,
  type ControlReconciliation,
  downloadControlRechargeOrdersCsv,
  downloadControlWalletTransactionsCsv,
  getControlRechargeOrders,
  getControlReconciliation,
  type RechargeOrderStatus,
  syncControlRechargeOrder,
} from "../api";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { OrderStatusBadge } from "./ui/StatusBadge";
import { useAutoRefresh } from "./ui/useAutoRefresh";
import { formatFen } from "./ui/vocabulary";

const PAGE_SIZE = 20;

const STATUS_FILTERS = [
  { value: "", label: "全部状态" },
  { value: "PENDING", label: "待支付" },
  { value: "PAID", label: "已支付" },
  { value: "FAILED", label: "失败" },
  { value: "CLOSED", label: "已关闭" },
] as const;

/**
 * 充值订单页（从 AdminApp 内联表格抽出）：补上状态筛选与分页（服务端
 * 均已支持）；手动查单改为说明性确认——它会向 ZPay 查单并可能入账。
 */
export function OrdersPage({ readOnly = false }: { readOnly?: boolean }) {
  const [orders, setOrders] = useState<ControlRechargeOrder[]>([]);
  const [orderTotal, setOrderTotal] = useState(0);
  const [orderOffset, setOrderOffset] = useState(0);
  const [statusDraft, setStatusDraft] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [reconciliation, setReconciliation] =
    useState<ControlReconciliation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pendingSyncOrderNo, setPendingSyncOrderNo] = useState<string | null>(
    null,
  );
  const [syncing, setSyncing] = useState(false);
  const { autoRefresh, toggleAutoRefresh } = useAutoRefresh(() => {
    void loadOrdersRef.current?.();
  });

  const loadOrders = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [orderPage, nextReconciliation] = await Promise.all([
        getControlRechargeOrders({
          status: (statusFilter || undefined) as
            | RechargeOrderStatus
            | undefined,
          limit: PAGE_SIZE,
          offset: orderOffset,
        }),
        getControlReconciliation(),
      ]);
      setOrders(orderPage.items);
      setOrderTotal(orderPage.total);
      setReconciliation(nextReconciliation);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：读取充值订单失败。",
      );
    } finally {
      setLoading(false);
    }
  }, [orderOffset, statusFilter]);

  const loadOrdersRef = useRef<(() => void) | null>(null);
  loadOrdersRef.current = () => void loadOrders();

  useEffect(() => {
    void loadOrders();
  }, [loadOrders]);

  function handleFilterSubmit(event: FormEvent) {
    event.preventDefault();
    setOrderOffset(0);
    setStatusFilter(statusDraft);
  }

  async function confirmSync(reason: string) {
    if (!pendingSyncOrderNo || syncing) {
      return;
    }
    setSyncing(true);
    setError("");
    setNotice("");
    try {
      await syncControlRechargeOrder(pendingSyncOrderNo, reason);
      setNotice(`订单 ${pendingSyncOrderNo} 状态已同步。`);
      setPendingSyncOrderNo(null);
      await loadOrders();
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "同步订单失败。",
      );
    } finally {
      setSyncing(false);
    }
  }

  async function exportRechargeOrders() {
    setError("");
    try {
      await downloadControlRechargeOrdersCsv();
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "导出充值订单失败。",
      );
    }
  }

  async function exportWalletTransactions() {
    setError("");
    try {
      await downloadControlWalletTransactionsCsv();
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "导出账务流水失败。",
      );
    }
  }

  return (
    <section aria-label="充值订单" className="admin-panel">
      <div className="admin-actions">
        <button
          aria-pressed={autoRefresh}
          type="button"
          onClick={toggleAutoRefresh}
        >
          {autoRefresh ? "自动刷新：开（30 秒）" : "自动刷新：关"}
        </button>
        <button type="button" onClick={() => void exportRechargeOrders()}>
          导出充值订单 CSV
        </button>
        <button type="button" onClick={() => void exportWalletTransactions()}>
          导出账务流水 CSV
        </button>
      </div>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {reconciliation ? (
        <div className="admin-metrics">
          <span>钱包数 {reconciliation.wallet_count}</span>
          <span>待支付订单 {reconciliation.pending_order_count}</span>
          <span>钱包不一致 {reconciliation.wallet_mismatch_count}</span>
          <span>
            已支付未入账 {reconciliation.paid_order_without_charge_count}
          </span>
          <span>
            入账但订单未支付 {reconciliation.charge_without_paid_order_count}
          </span>
        </div>
      ) : null}

      <form className="admin-form" onSubmit={handleFilterSubmit}>
        <label>
          订单状态
          <select
            value={statusDraft}
            onChange={(event) => setStatusDraft(event.target.value)}
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <button disabled={loading} type="submit">
          筛选
        </button>
      </form>

      {!loading && orders.length === 0 && !error ? (
        <PageBanner tone="notice">暂无充值订单。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="充值订单列表"
          headers={
            <>
              <th>订单号</th>
              <th>账号</th>
              <th>金额</th>
              <th>条数</th>
              <th>状态</th>
              <th>操作</th>
            </>
          }
        >
          {orders.map((order) => (
            <tr key={order.id}>
              <td>
                <code>{order.order_no}</code>
              </td>
              <td>{order.username}</td>
              <td>{formatFen(order.amount_fen)}</td>
              <td>{order.credits}</td>
              <td>
                <OrderStatusBadge status={order.status} />
              </td>
              <td>
                {order.status === "PENDING" && !readOnly ? (
                  <button
                    type="button"
                    onClick={() => setPendingSyncOrderNo(order.order_no)}
                  >
                    查单同步
                  </button>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <Pagination
        disabled={loading}
        limit={PAGE_SIZE}
        offset={orderOffset}
        total={orderTotal}
        onPageChange={setOrderOffset}
      />

      <ConfirmDialog
        busy={syncing}
        confirmLabel="确认查单"
        description="将立即向 ZPay 查询该订单的最新支付状态；若已支付，会当场完成入账。原因将写入审计日志。"
        level="reason"
        open={pendingSyncOrderNo !== null}
        title={`查单同步 ${pendingSyncOrderNo ?? ""}`}
        onClose={() => setPendingSyncOrderNo(null)}
        onConfirm={(reason: string) => void confirmSync(reason)}
      />
    </section>
  );
}
