import { useCallback, useEffect, useRef, useState } from "react";
import {
  AdminActivationError,
  listRefunds,
  markRefundExecution,
  type RefundListItem,
} from "../api.admin";
import { CustomerLink } from "./CustomerLink";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { FilterBar } from "./ui/FilterBar";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * M1 退款管理最小版（ADM-08）：双事实对齐清单。
 *
 * 账本侧（REFUND_APPROVAL 反向调账冲积分）在客户详情发起；本页收口
 * 资金侧：支付后台人工退付完成后回到这里登记执行结果（渠道 + 第三方
 * 退款单号），未登记的行就是每天要清的「已冲积分、待退付」队列。
 */
const PAGE_SIZE = 20;
const SOURCE_REVIEW_MESSAGE = "未核对原充值来源，需人工核对";

function hasVerifiedSource(item: RefundListItem): boolean {
  return (
    item.source_verified === true &&
    Boolean(item.original_recharge_order_no?.trim())
  );
}

type ExecutedFilter = "" | "pending" | "done";

const EXECUTED_OPTIONS: Array<{ value: ExecutedFilter; label: string }> = [
  { value: "", label: "全部" },
  { value: "pending", label: "待退付" },
  { value: "done", label: "已退付" },
];

export function RefundsPage({
  readOnly = false,
  onCustomer,
}: {
  readOnly?: boolean;
  onCustomer?: (id: string) => void;
}) {
  const [items, setItems] = useState<RefundListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  // 草稿与已提交筛选分开（T22）：点「查询」才发请求。
  const defaultFilters: { username: string; executed: ExecutedFilter } = {
    username: "",
    executed: "pending",
  };
  const [draft, setDraft] = useState(defaultFilters);
  const [filters, setFilters] = useState(defaultFilters);
  const [pendingMark, setPendingMark] = useState<RefundListItem | null>(null);
  const [channel, setChannel] = useState<
    "wechat_manual" | "offline_transfer" | "other"
  >("wechat_manual");
  const [providerRef, setProviderRef] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listRefunds({
        targetUsername: filters.username || undefined,
        executed: filters.executed,
        limit: PAGE_SIZE,
        offset,
      });
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取退款清单失败");
    } finally {
      setLoading(false);
    }
  }, [filters, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (readOnly) setPendingMark(null);
  }, [readOnly]);

  async function confirmMark(reason: string) {
    if (!pendingMark || inFlight.current || readOnly) return;
    const current = items.find(
      (item) => item.adjustment_id === pendingMark.adjustment_id,
    );
    if (
      !hasVerifiedSource(pendingMark) ||
      !current ||
      !hasVerifiedSource(current) ||
      current.executed ||
      current.original_recharge_order_no !==
        pendingMark.original_recharge_order_no
    ) {
      setError(SOURCE_REVIEW_MESSAGE);
      setPendingMark(null);
      return;
    }
    inFlight.current = true;
    setSaving(true);
    setError("");
    try {
      await markRefundExecution({
        adjustmentId: pendingMark.adjustment_id,
        channel,
        providerRef: providerRef.trim(),
        note: note.trim(),
        reason,
      });
      setNotice(
        `已登记 ${pendingMark.target_username} 的退付执行（${providerRef.trim()}）。`,
      );
      setPendingMark(null);
      setProviderRef("");
      setNote("");
      await load();
    } catch (cause) {
      if (
        cause instanceof AdminActivationError &&
        cause.code === "REFUND_SOURCE_REVIEW_REQUIRED"
      ) {
        setItems((rows) =>
          rows.map((item) =>
            item.adjustment_id === pendingMark.adjustment_id
              ? { ...item, source_verified: false }
              : item,
          ),
        );
        setPendingMark(null);
      }
      setError(cause instanceof Error ? cause.message : "登记退付执行失败");
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }

  return (
    <section className="admin-panel admin-refunds-page" aria-label="退款管理">
      <header>
        <h3>退款管理</h3>
        <p className="admin-hint">
          账本侧扣减在客户详情发起，只能扣回对应原已付款充值的未用积分。
          退付在支付后台人工办理后回此登记。未核对原充值来源的记录需人工核对，
          不能登记新的退付；历史已执行记录保留原退付事实。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <FilterBar
        ariaLabel="退款筛选"
        busy={loading}
        onReset={() => {
          setOffset(0);
          setDraft(defaultFilters);
          setFilters(defaultFilters);
        }}
        onSubmit={() => {
          setOffset(0);
          setFilters(draft);
        }}
      >
        <label>
          客户
          <input
            value={draft.username}
            onChange={(event) =>
              setDraft({ ...draft, username: event.target.value })
            }
            placeholder="用户名或公司名"
          />
        </label>
        <label>
          退付状态
          <select
            value={draft.executed}
            onChange={(event) =>
              setDraft({
                ...draft,
                executed: event.target.value as ExecutedFilter,
              })
            }
          >
            {EXECUTED_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </FilterBar>
      {loading ? (
        <p role="status">正在读取退款清单…</p>
      ) : items.length === 0 ? (
        <PageBanner tone="notice">暂无符合条件的退款记录。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="退款双事实清单"
          headers={
            <>
              <th>客户</th>
              <th>冲减积分</th>
              <th>原充值来源</th>
              <th>调账时间 / 经办</th>
              <th>退付状态</th>
              <th>退付渠道 / 单号 / 时间</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.adjustment_id}>
              <td>
                <CustomerLink
                  userId={item.target_user_id}
                  username={item.target_username}
                  company={item.target_display_name || undefined}
                  onCustomer={onCustomer}
                />
              </td>
              <td>-{item.credits_deducted} 积分</td>
              <td>
                {hasVerifiedSource(item) ? (
                  <>
                    <code>{item.original_recharge_order_no}</code>
                    <div>已核对原充值来源</div>
                  </>
                ) : (
                  <StatusBadge tone="warn">{SOURCE_REVIEW_MESSAGE}</StatusBadge>
                )}
              </td>
              <td>
                {formatDateTime(item.created_at)} · {item.admin_username}
              </td>
              <td>
                <StatusBadge tone={item.executed ? "good" : "warn"}>
                  {item.executed ? "已退付" : "待退付"}
                </StatusBadge>
              </td>
              <td>
                {item.executed
                  ? `${item.channel_label} · ${item.provider_ref} · ${formatDateTime(item.executed_at)}（${item.executed_by}）`
                  : "—"}
              </td>
              <td>
                {item.executed || readOnly ? (
                  "—"
                ) : (
                  <button
                    type="button"
                    disabled={saving || !hasVerifiedSource(item)}
                    title={
                      hasVerifiedSource(item)
                        ? undefined
                        : SOURCE_REVIEW_MESSAGE
                    }
                    onClick={() => {
                      if (
                        inFlight.current ||
                        !hasVerifiedSource(item) ||
                        readOnly
                      )
                        return;
                      setError("");
                      setChannel("wechat_manual");
                      setProviderRef("");
                      setNote("");
                      setPendingMark(item);
                    }}
                  >
                    登记退付
                  </button>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
      <Pagination
        total={total}
        limit={PAGE_SIZE}
        offset={offset}
        onPageChange={setOffset}
      />
      <ConfirmDialog
        open={pendingMark !== null && !readOnly}
        title="登记退付执行"
        confirmLabel="确认登记"
        description={
          pendingMark
            ? `${pendingMark.target_username} · 冲减 ${pendingMark.credits_deducted} 积分。原充值订单号：${pendingMark.original_recharge_order_no}。请填写支付后台实际退付的渠道与第三方退款单号；登记后不可重填，如需更正走调账更正流程。`
            : ""
        }
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !inFlight.current && setPendingMark(null)}
        onConfirm={(reason) => void confirmMark(reason)}
      >
        <label>
          退付渠道
          <select
            disabled={saving}
            value={channel}
            onChange={(event) =>
              setChannel(
                event.target.value as
                  | "wechat_manual"
                  | "offline_transfer"
                  | "other",
              )
            }
          >
            <option value="wechat_manual">微信商户原路退回</option>
            <option value="offline_transfer">线下转账退回</option>
            <option value="other">其他渠道</option>
          </select>
        </label>
        <label>
          第三方退款单号
          <input
            disabled={saving}
            value={providerRef}
            onChange={(event) => setProviderRef(event.target.value)}
            placeholder="支付后台的退款单号，必填"
            required
          />
        </label>
        <label>
          备注（可选）
          <input
            disabled={saving}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="如部分退回说明"
          />
        </label>
      </ConfirmDialog>
    </section>
  );
}
