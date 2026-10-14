import { useCallback, useEffect, useState } from "react";

import { type DashboardSummary, getDashboardSummary } from "../api.admin";
import clapperboardIcon from "../assets/icons/clapperboard.svg";
import gaugeIcon from "../assets/icons/gauge.svg";
import usersIcon from "../assets/icons/users-round.svg";
import walletIcon from "../assets/icons/wallet.svg";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { CustomerPicker, type PickedCustomer } from "./ui/CustomerPicker";
import { PageBanner } from "./ui/PageBanner";
import { useAutoRefresh } from "./ui/useAutoRefresh";
import { formatDateTime } from "./ui/vocabulary";
import "./economics.css";

function fenToYuan(fen: number): string {
  return (fen / 100).toFixed(2);
}

function KpiCard({
  label,
  value,
  sub,
  tone,
  icon,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "warn" | "danger";
  icon: string;
}) {
  return (
    <section
      className={`admin-summary-card${tone === "warn" ? " is-warning" : ""}${tone === "danger" ? " is-danger" : ""}`}
      aria-label={label}
    >
      <img src={icon} alt={`${label}图标`} />
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {sub ? <small>{sub}</small> : null}
      </div>
    </section>
  );
}

function TodoRow({
  label,
  count,
  tone,
  hint,
  onOpen,
  openLabel = "去处理",
}: {
  label: string;
  /** null = 数据源暂不可用：显示「—」而不是用 0 冒充「没有待办」。 */
  count: number | null;
  tone: "warn" | "danger" | "info";
  hint?: string;
  onOpen?: () => void;
  openLabel?: string;
}) {
  return (
    <li>
      <span>{label}</span>
      <span className={`status-badge status-badge--${tone}`}>
        {count ?? "—"}
      </span>
      {hint ? <small>{hint}</small> : null}
      {onOpen ? (
        <button type="button" onClick={onOpen}>
          {openLabel}
        </button>
      ) : null}
    </li>
  );
}

// 待办按业务域分组，运营一眼看出哪条线上有事。
const TODO_GROUPS = ["资金", "生成", "客户", "内容", "系统"] as const;
type TodoGroup = (typeof TODO_GROUPS)[number];

/**
 * W15 — 总览仪表盘：KPI、近 7 日生成趋势、待办事项与快捷操作。
 * 数据来自 GET /api/control/dashboard/summary（AdminReader）。
 */
export function OverviewPage({
  readOnly = false,
  onNavigate,
}: {
  readOnly?: boolean;
  /** target 用于带上下文跳转（如快捷操作选定的客户 userId）。 */
  onNavigate?: (tab: string, target?: Record<string, string>) => void;
}) {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState("");
  // 快捷操作先选客户（T13）：选定后直达该客户详情里的对应表单，
  // 不再落到客户列表让运营再搜一次。
  const [quickAction, setQuickAction] = useState<{
    destination: string;
    label: string;
  } | null>(null);
  const [quickCustomer, setQuickCustomer] = useState<PickedCustomer | null>(
    null,
  );
  const [quickError, setQuickError] = useState("");
  function startQuickAction(destination: string, label: string) {
    setQuickCustomer(null);
    setQuickError("");
    setQuickAction({ destination, label });
  }

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setError("");
      setSummary(await getDashboardSummary());
      setUpdatedAt(new Date().toISOString());
    } catch (cause) {
      // 刷新失败只局部提示：已有数据继续展示并标明时间，不再整页换成报错。
      setError(cause instanceof Error ? cause.message : "读取仪表盘失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  // 总览是值班常开页：默认每 60 秒刷新一次（页面隐藏时暂停），可手动关闭。
  const { autoRefresh, toggleAutoRefresh } = useAutoRefresh(
    () => void load(),
    60_000,
    true,
  );

  const toolbar = (
    <div className="dashboard-toolbar">
      <span role="status">
        {updatedAt
          ? `数据更新于 ${formatDateTime(updatedAt)}`
          : loading
            ? "正在加载仪表盘…"
            : ""}
      </span>
      <button type="button" disabled={loading} onClick={() => void load()}>
        {loading ? "刷新中…" : "刷新"}
      </button>
      <button
        type="button"
        aria-pressed={autoRefresh}
        onClick={toggleAutoRefresh}
      >
        {autoRefresh ? "自动刷新：开（60 秒）" : "自动刷新：关"}
      </button>
    </div>
  );
  const errorBanner = error ? (
    <PageBanner tone="error">
      {summary ? `刷新失败，下方仍是上次成功读取的数据：${error}` : error}{" "}
      <button type="button" disabled={loading} onClick={() => void load()}>
        重试
      </button>
    </PageBanner>
  ) : null;

  if (!summary) {
    return (
      <section className="admin-panel" aria-label="总览仪表盘">
        {toolbar}
        {errorBanner}
      </section>
    );
  }

  const { today, trend, todos } = summary;
  const maxTrend = Math.max(
    1,
    ...trend.map((point) => point.succeeded + point.failed),
  );
  const maxCost = Math.max(1, ...trend.map((point) => point.cost_fen ?? 0));
  const successPoints = trend.map((point, index) => ({
    x: ((index + 0.5) / trend.length) * 100,
    y: 94 - (point.succeeded / maxTrend) * 82,
  }));
  const successPath = successPoints
    .map((point, index) => {
      if (index === 0) return `M ${point.x} ${point.y}`;
      const previous = successPoints[index - 1];
      const middleX = (previous.x + point.x) / 2;
      // 水平切线让相邻段平滑连接，控制点不越过数据区间，避免虚构峰谷。
      return `C ${middleX} ${previous.y}, ${middleX} ${point.y}, ${point.x} ${point.y}`;
    })
    .join(" ");
  // 拆解失败中最集中的那条上游原因，直接摆在待办行上供定位。
  const topAnalysisReason =
    todos.analysis_failure_reasons?.[0]?.reason ??
    todos.analysis_failure_reasons?.[0]?.error_code ??
    undefined;
  const todoItems: Array<{
    key: string;
    group: TodoGroup;
    label: string;
    count: number | null;
    tone: "warn" | "danger" | "info";
    tab: string;
    hint?: string;
  }> = [
    {
      key: "failed",
      group: "生成",
      label: "失败任务待处理",
      count: todos.failed_tasks_7d,
      tone: "danger",
      tab: "failedGenerationRecords",
    },
    {
      // 拆解是付费上游调用：2026-09-20 那种「拆解全挂、总览全绿」不能再出现，
      // 单列一行并把最集中的上游原因直接摆出来。
      key: "analysis",
      group: "生成",
      label: "拆解失败待排查",
      count: todos.analysis_failures_7d ?? 0,
      tone: "danger",
      tab: "analysisFailures",
      hint: topAnalysisReason,
    },
    {
      key: "recon",
      group: "资金",
      label: "对账不一致",
      count: todos.reconciliation_problems,
      tone: "warn",
      // 落到资金中心的对账异常清单，条数与该清单一致（方案 P0-5）。
      tab: "recon",
    },
    {
      key: "rates",
      group: "系统",
      label: "成本待配置",
      count: todos.unconfigured_rates ?? 0,
      tone: "danger",
      tab: "rates",
    },
    {
      // 2026-09-18 匹配梳理 P3：/dashboard/summary 一直在返回这两个字段，
      // 总览却从未展示。它们直接决定"今天的数字能不能信"：
      // pending_count 是仍处 PENDING 的计费操作数（billing_reports.py:174），
      // unknown_revenue_count 是收入无法确定的操作数（同文件 :322）。
      key: "pendingOps",
      group: "资金",
      label: "今日操作待结算",
      count: today.pending_operations ?? 0,
      tone: "info",
      // 这两项是计费操作口径，清单在经营分析的成本明细里（方案 P0-5）。
      tab: "pendingOperations",
      hint: "当日金额尚未定稿",
    },
    {
      // P0-5 收口：unknown_cost_records 此前只喂给「今日成本」KpiCard 的
      // 待核对判定（yuanOrUnknown），没有任何入口能落到条数一致的清单。
      // 服务端 billing_reports.py 承诺「成本待核对」清单与计数同口径
      // （attention=unknown_cost），这里补上发出方。
      key: "unknownCost",
      group: "资金",
      label: "今日成本待核对",
      count: todos.unknown_cost_records ?? 0,
      tone: "warn",
      tab: "unknownCost",
      hint: "成本金额尚未确定",
    },
    {
      key: "unknownRevenue",
      group: "资金",
      label: "今日收入未确定",
      count: today.unknown_revenue_operations ?? 0,
      tone: "warn",
      tab: "unknownRevenue",
      hint: "毛利可能偏低",
    },
    // 全局待办（整改 T11）：每项计数与跳转后清单同口径，点进去条数一致。
    {
      key: "pendingOrders",
      group: "资金",
      label: "待支付充值订单",
      count: todos.pending_orders ?? 0,
      tone: "info",
      tab: "pendingOrders",
      hint: "含客户放弃支付的订单；久未到账可查单补单",
    },
    {
      key: "queueAttention",
      group: "生成",
      label: "队列需人工处理",
      count: todos.queue_attention ?? 0,
      tone: "danger",
      tab: "queueAttention",
      hint: "隔离区冻结了客户预扣积分",
    },
    {
      key: "openTickets",
      group: "客户",
      label: "工单未结案",
      count: todos.open_tickets ?? 0,
      tone: "warn",
      tab: "openTickets",
    },
    {
      key: "contentFailed",
      group: "内容",
      label: "内容准备失败",
      count: todos.content_prepare_failed ?? 0,
      tone: "warn",
      tab: "contentFailed",
    },
    {
      key: "activeAlerts",
      group: "系统",
      label: "告警命中",
      count: todos.active_alerts === undefined ? 0 : todos.active_alerts,
      tone: "danger",
      tab: "activeAlerts",
      hint: todos.active_alerts === null ? "告警聚合暂不可用" : undefined,
    },
  ];

  // 昨日数据取自同一份近 7 日趋势（最后一点是今天），用来给今日指标一个参照。
  const yesterday = trend.length >= 2 ? trend[trend.length - 2] : undefined;
  return (
    <div className="dashboard-page">
      {toolbar}
      {errorBanner}
      {(today.legacy_cost_records ?? 0) + (today.legacy_settlements ?? 0) >
        0 && (
        <p role="status">
          今日有 {today.legacy_cost_records ?? 0} 条历史成本、
          {today.legacy_settlements ?? 0} 条历史结算待核对。
        </p>
      )}
      <div className="dashboard-kpis">
        <h2 className="dashboard-kpi-group">今日</h2>
        <KpiCard
          icon={clapperboardIcon}
          label="今日生成"
          value={String(today.generation_count)}
          sub={`输出 ${(today.output_seconds ?? 0).toLocaleString()} 秒${
            yesterday ? ` · 昨日成功 ${yesterday.succeeded} 条` : ""
          }`}
        />
        <KpiCard
          icon={gaugeIcon}
          label="生成成功率"
          value={
            today.success_rate_pct == null ? "—" : `${today.success_rate_pct}%`
          }
          sub={`成功 ${today.succeeded}`}
        />
        <KpiCard
          icon={walletIcon}
          label="今日成本"
          value={yuanOrUnknown(today.cost_fen, todos.unknown_cost_records)}
          sub={
            today.cost_fen == null
              ? "结算或成本待核对"
              : yesterday?.cost_fen != null
                ? `已确认成本 · 昨日 ¥${fenToYuan(yesterday.cost_fen)}`
                : "已确认成本"
          }
          tone={today.cost_fen == null ? "warn" : undefined}
        />
        <KpiCard
          icon={gaugeIcon}
          label="今日毛利"
          value={
            today.gross_fen == null
              ? "待核对"
              : `¥${fenToYuan(today.gross_fen)}`
          }
          sub={
            today.margin_pct == null
              ? "利润率未知"
              : `利润率 ${today.margin_pct}%`
          }
        />
        <KpiCard
          icon={walletIcon}
          label="今日充值"
          value={`¥${fenToYuan(today.recharge_fen)}`}
          sub={`${today.recharge_orders ?? 0} 单`}
        />
        {/* 存量指标与当日指标分开：有效客户不是「今天」发生的数。对账异常
            只在待办里出现一次，不再与 KPI 卡重复。 */}
        <h2 className="dashboard-kpi-group">存量</h2>
        <KpiCard
          icon={usersIcon}
          label="有效客户"
          value={String(today.active_customers)}
          sub="已启用的客户账户"
        />
      </div>

      <div className="dashboard-main-grid">
        <section className="admin-panel" aria-label="近 7 日生成与成本">
          <div className="dashboard-chart-heading">
            <h2>近 7 日生成与成本</h2>
            <div className="dashboard-chart-legend">
              <span className="is-cost">成本（元）</span>
              <span className="is-success">成功生成数（条）</span>
            </div>
          </div>
          {trend.length === 0 ? (
            <p>暂无数据。</p>
          ) : (
            <div
              className="dashboard-combo"
              role="img"
              aria-label="成本柱状图与成功数趋势曲线"
            >
              <div
                className="dashboard-axis dashboard-axis--left"
                aria-hidden="true"
              >
                <span>¥{Math.round(maxCost / 100).toLocaleString()}</span>
                <span>¥0</span>
              </div>
              <div
                className="dashboard-axis dashboard-axis--right"
                aria-hidden="true"
              >
                <span>{maxTrend}</span>
                <span>0</span>
              </div>
              <svg
                viewBox="0 0 100 100"
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                <path
                  d={successPath}
                  fill="none"
                  stroke="#75a47c"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  vectorEffect="non-scaling-stroke"
                />
              </svg>
              {trend.map((point) => (
                <div
                  className="dashboard-trend__col"
                  key={point.day}
                  title={`${point.day}：成功 ${point.succeeded} / 失败 ${point.failed}`}
                >
                  <div className="dashboard-trend__bars">
                    {point.cost_fen == null ? (
                      <span title="成本待核对">待核对</span>
                    ) : (
                      <div
                        className="dashboard-trend__bar dashboard-trend__bar--cost"
                        title={`成本 ¥${fenToYuan(point.cost_fen ?? 0)}`}
                        style={{
                          height: `${Math.round(((point.cost_fen ?? 0) / maxCost) * 100)}%`,
                        }}
                      >
                        <b>{fenToYuan(point.cost_fen ?? 0)}</b>
                      </div>
                    )}
                  </div>
                  <span>
                    {point.day.slice(5)}
                    <small>{point.succeeded} 条</small>
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="admin-panel" aria-label="待办事项">
          <h2>待办事项</h2>
          {TODO_GROUPS.map((group) => (
            <section key={group} aria-label={`${group}待办`}>
              <h3 className="dashboard-todo-group">{group}</h3>
              <ul className="dashboard-todos">
                {todoItems
                  .filter((item) => item.group === group)
                  .map((item) => (
                    <TodoRow
                      key={item.key}
                      count={item.count}
                      hint={item.hint}
                      label={item.label}
                      tone={item.tone}
                      // 审计员同样需要落到清单去核对，只是按钮说「去查看」；
                      // 写操作由目标页自身按 readOnly 收敛。
                      onOpen={
                        (item.count ?? 0) > 0
                          ? () => onNavigate?.(item.tab)
                          : undefined
                      }
                      openLabel={readOnly ? "去查看" : "去处理"}
                    />
                  ))}
              </ul>
            </section>
          ))}
        </section>
      </div>

      <div className="dashboard-bottom-grid">
        {!readOnly ? (
          <section className="admin-panel" aria-label="快捷操作">
            <h2>快捷操作</h2>
            <div className="dashboard-quick-actions">
              {/* 收款开通计入收入、赠送不计收入，入口分开（方案 P0-1）。 */}
              <button
                type="button"
                onClick={() =>
                  startQuickAction("customerPackage", "开通套餐（已收款）")
                }
              >
                开通套餐（已收款）
              </button>
              <button
                type="button"
                onClick={() =>
                  startQuickAction("customerAdjustments", "赠送积分")
                }
              >
                赠送积分
              </button>
              <button
                type="button"
                onClick={() => startQuickAction("customerRefund", "退款扣减")}
              >
                退款扣减
              </button>
              <button type="button" onClick={() => onNavigate?.("costDetails")}>
                成本核对
              </button>
            </div>
          </section>
        ) : null}
      </div>
      <ConfirmDialog
        open={quickAction !== null}
        title={quickAction ? `${quickAction.label}：选择客户` : ""}
        description="选定客户后直接打开该客户详情里的对应表单。"
        confirmLabel="前往表单"
        level="standard"
        error={quickError}
        onClose={() => setQuickAction(null)}
        onConfirm={() => {
          if (!quickAction) return;
          if (!quickCustomer) {
            setQuickError("请先选择客户。");
            return;
          }
          onNavigate?.(quickAction.destination, {
            userId: quickCustomer.user_id,
          });
          setQuickAction(null);
        }}
      >
        <CustomerPicker
          value={quickCustomer}
          onChange={(customer) => {
            setQuickCustomer(customer);
            setQuickError("");
          }}
        />
      </ConfirmDialog>
    </div>
  );
}

function yuanOrUnknown(
  costFen: number | null | undefined,
  unknownCount: number | undefined,
): string {
  if (costFen == null || (unknownCount ?? 0) > 0) return "待核对";
  return `¥${fenToYuan(costFen)}`;
}
