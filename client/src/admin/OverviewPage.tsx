import { useCallback, useEffect, useState } from "react";

import { type DashboardSummary, getDashboardSummary } from "../api.admin";
import clapperboardIcon from "../assets/icons/clapperboard.svg";
import gaugeIcon from "../assets/icons/gauge.svg";
import shieldIcon from "../assets/icons/shield-check.svg";
import usersIcon from "../assets/icons/users-round.svg";
import walletIcon from "../assets/icons/wallet.svg";
import { PageBanner } from "./ui/PageBanner";
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
  onOpen,
}: {
  label: string;
  count: number;
  tone: "warn" | "danger" | "info";
  onOpen?: () => void;
}) {
  return (
    <li>
      <span>{label}</span>
      <span className={`status-badge status-badge--${tone}`}>{count}</span>
      {onOpen ? (
        <button type="button" onClick={onOpen}>
          去处理
        </button>
      ) : null}
    </li>
  );
}

/**
 * W15 — 总览仪表盘：KPI、近 7 日生成趋势、待办事项与快捷操作。
 * 数据来自 GET /api/control/dashboard/summary（AdminReader）。
 */
export function OverviewPage({
  readOnly = false,
  onNavigate,
}: {
  readOnly?: boolean;
  onNavigate?: (tab: string) => void;
}) {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setError("");
      setSummary(await getDashboardSummary());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取仪表盘失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return <PageBanner tone="error">{error}</PageBanner>;
  }
  if (!summary) {
    return (
      <section className="admin-panel" aria-label="总览仪表盘">
        <p>正在加载仪表盘…</p>
      </section>
    );
  }

  const { today, trend, todos, device_slots } = summary;
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
  const todoItems: Array<{
    key: string;
    label: string;
    count: number;
    tone: "warn" | "danger" | "info";
    tab: string;
  }> = [
    {
      key: "pairings",
      label: "待批准配对",
      count: todos.pending_pairings,
      tone: "warn",
      tab: "codes",
    },
    {
      key: "failed",
      label: "失败任务待处理",
      count: todos.failed_tasks_7d,
      tone: "danger",
      tab: "failedGenerationRecords",
    },
    {
      key: "recon",
      label: "对账不一致",
      count: todos.reconciliation_problems,
      tone: "warn",
      tab: "funds",
    },
    {
      key: "rates",
      label: "费率未配置科目",
      count: todos.unconfigured_rates ?? 0,
      tone: "danger",
      tab: "rates",
    },
    {
      key: "expiring",
      label: "即将过期激活码",
      count: todos.expiring_codes_7d,
      tone: "info",
      tab: "codes",
    },
  ];

  return (
    <div className="dashboard-page">
      <div className="dashboard-kpis">
        <KpiCard
          icon={clapperboardIcon}
          label="今日生成"
          value={String(today.generation_count)}
          sub={`输出 ${(today.output_seconds ?? 0).toLocaleString()} 秒`}
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
            (todos.unknown_cost_records ?? 0) > 0
              ? `${todos.unknown_cost_records} 项用量待核对`
              : "实际用量口径"
          }
          tone={(todos.unknown_cost_records ?? 0) > 0 ? "warn" : undefined}
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
          icon={usersIcon}
          label="在线设备"
          value={String(today.online_devices)}
          sub={`活跃客户 ${today.active_customers}`}
        />
        <KpiCard
          icon={walletIcon}
          label="今日充值"
          value={`¥${fenToYuan(today.recharge_fen)}`}
          sub={`${today.recharge_orders ?? 0} 单`}
        />
        <KpiCard
          icon={shieldIcon}
          label="对账异常"
          value={String(todos.reconciliation_problems)}
          sub="资金账务核对"
          tone={todos.reconciliation_problems ? "warn" : undefined}
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
                    <div
                      className="dashboard-trend__bar dashboard-trend__bar--cost"
                      title={`成本 ¥${fenToYuan(point.cost_fen ?? 0)}`}
                      style={{
                        height: `${Math.round(((point.cost_fen ?? 0) / maxCost) * 100)}%`,
                      }}
                    >
                      <b>{fenToYuan(point.cost_fen ?? 0)}</b>
                    </div>
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
          <ul className="dashboard-todos">
            {todoItems.map((item) => (
              <TodoRow
                key={item.key}
                count={item.count}
                label={item.label}
                tone={item.tone}
                onOpen={
                  !readOnly && item.count > 0
                    ? () => onNavigate?.(item.tab)
                    : undefined
                }
              />
            ))}
          </ul>
        </section>
      </div>

      <div className="dashboard-bottom-grid">
        <section className="admin-panel" aria-label="设备槽位占用">
          <h2>设备槽位占用</h2>
          <strong className="slot-count">
            {device_slots.bound} / {device_slots.total}
          </strong>
          <div className="slot-meter">
            <i
              style={{
                width: `${device_slots.total ? Math.min(100, (device_slots.bound / device_slots.total) * 100) : 0}%`,
              }}
            />
          </div>
          <p className="admin-hint">已绑定 / 总槽位</p>
        </section>
        {!readOnly ? (
          <section className="admin-panel" aria-label="快捷操作">
            <h2>快捷操作</h2>
            <div className="dashboard-quick-actions">
              <button type="button" onClick={() => onNavigate?.("issueCodes")}>
                快速发码
              </button>
              <button
                type="button"
                onClick={() => onNavigate?.("customerAdjustments")}
              >
                后台加款
              </button>
              <button
                type="button"
                onClick={() => onNavigate?.("customerAdjustments")}
              >
                发放免费秒数
              </button>
              <button type="button" onClick={() => onNavigate?.("costDetails")}>
                成本核对
              </button>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}

function yuanOrUnknown(
  costFen: number | undefined,
  unknownCount: number | undefined,
): string {
  if (costFen === undefined || (unknownCount ?? 0) > 0) return "用量待核对";
  return `¥${fenToYuan(costFen)}`;
}
