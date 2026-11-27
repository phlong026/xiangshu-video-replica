import { useCallback, useEffect, useRef, useState } from "react";
import { adminRead } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { formatFen } from "./ui/vocabulary";

type OverviewMetrics = {
  recharge_fen: number;
  paying_customers: number;
  new_paying_customers: number;
  revenue_fen: number;
  cost_fen: number;
  unknown_cost_count: number;
  pending_count: number;
  unknown_revenue_count?: number;
  legacy_cost_count?: number;
  legacy_settlement_count?: number;
  gross_fen?: number | null;
  margin_pct?: number | null;
  prepaid_credits: number;
  prepaid_fen: number | null;
};

type BusinessOverview = {
  start: string;
  end: string;
  prev_start: string;
  prev_end: string;
  metrics: OverviewMetrics;
  prev: OverviewMetrics;
  monthly?: {
    month: string;
    revenue_fen: number | null;
    cost_fen: number | null;
    known_revenue_fen?: number | null;
    known_cost_fen?: number | null;
    margin_pct?: number | null;
  }[];
  daily: {
    day: string;
    revenue_fen: number | null;
    cost_fen: number | null;
    known_revenue_fen?: number | null;
    known_cost_fen?: number | null;
    margin_pct?: number | null;
    unknown_cost_count?: number;
    unknown_revenue_count?: number;
    legacy_cost_count?: number;
    legacy_settlement_count?: number;
    pending_count?: number;
  }[];
  modules: {
    service: string;
    label: string;
    revenue_fen: number;
    cost_fen: number;
    unknown_cost_count: number;
    pending_count: number;
    unknown_revenue_count?: number;
    legacy_cost_count?: number;
    legacy_settlement_count?: number;
  }[];
  top_customers: {
    user_id: string;
    username: string;
    display_name: string;
    revenue_fen: number;
  }[];
};

type TrendDay = BusinessOverview["daily"][number];

function dayMargin(day: TrendDay) {
  if (
    day.revenue_fen == null ||
    day.cost_fen == null ||
    day.revenue_fen <= 0 ||
    (day.unknown_cost_count ?? 0) +
      (day.unknown_revenue_count ?? 0) +
      (day.legacy_cost_count ?? 0) +
      (day.legacy_settlement_count ?? 0) +
      (day.pending_count ?? 0) >
      0
  )
    return null;
  return (
    day.margin_pct ?? ((day.revenue_fen - day.cost_fen) / day.revenue_fen) * 100
  );
}

function dayDescription(day: TrendDay) {
  const rate = dayMargin(day);
  const margin =
    rate != null
      ? `${Math.round(rate)}%`
      : day.revenue_fen === 0 && day.cost_fen != null
        ? "无收入，无法计算"
        : "待核对";
  return `${day.day}：已知确认收入 ${formatFen(day.known_revenue_fen ?? day.revenue_fen ?? 0)}，已知成本 ${formatFen(day.known_cost_fen ?? day.cost_fen ?? 0)}，毛利率 ${margin}`;
}

const pad = (value: number) => String(value).padStart(2, "0");

/** 上海挂钟「今天」（en-CA 即 YYYY-MM-DD），全站筛选同口径。 */
function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

function isCalendarDay(iso: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return false;
  const date = new Date(iso);
  return (
    Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === iso
  );
}

function shiftDays(iso: string, days: number): string {
  const from = new Date(`${iso}T00:00:00Z`);
  from.setUTCDate(from.getUTCDate() + days);
  return from.toISOString().slice(0, 10);
}

function weekStart(iso: string): string {
  const from = new Date(`${iso}T00:00:00Z`);
  const weekday = from.getUTCDay() || 7; // 周一为一周开始
  return shiftDays(iso, 1 - weekday);
}

function prevMonthRange(iso: string): [string, string] {
  const year = Number(iso.slice(0, 4));
  const month = Number(iso.slice(5, 7));
  const pm = month === 1 ? 12 : month - 1;
  const py = month === 1 ? year - 1 : year;
  const lastDay = new Date(Date.UTC(py, pm, 0)).getUTCDate();
  return [`${py}-${pad(pm)}-01`, `${py}-${pad(pm)}-${pad(lastDay)}`];
}

type Preset = "today" | "week" | "month" | "lastMonth" | "custom";
const maxRangeDays = 366;

function presetRange(preset: Preset, current: { start: string; end: string }) {
  const today = shanghaiToday();
  switch (preset) {
    case "today":
      return { start: today, end: today };
    case "week":
      return { start: weekStart(today), end: today };
    case "month":
      return { start: `${today.slice(0, 7)}-01`, end: today };
    case "lastMonth": {
      const [start, end] = prevMonthRange(today);
      return { start, end };
    }
    case "custom":
      return current;
  }
}

/** 环比：上一等长区间为 0 时无从算百分比，按方向文案如实表达。 */
function delta(cur: number, prev: number): string {
  if (cur === prev) return "持平";
  if (prev === 0) return "上一区间为 0";
  const pct = Math.round(((cur - prev) / prev) * 100);
  return `${pct > 0 ? "+" : ""}${pct}%`;
}

function incompleteEvidence(metrics: OverviewMetrics) {
  return {
    income:
      (metrics.unknown_revenue_count ?? 0) +
      (metrics.legacy_settlement_count ?? 0),
    cost: metrics.unknown_cost_count + (metrics.legacy_cost_count ?? 0),
    pending: metrics.pending_count,
  };
}

/** 经营看板（方案 P1）：给老板的一屏结论——8 张指标卡、按日趋势、业务构成、Top 客户。
 *  毛利与客单价由前端按卡片口径计算；确认收入/成本沿用服务端已知金额口径，
 *  缺证据的请求只进「待核对」条数，绝不冒充零成本或零收入。 */
export function BusinessDashboard({
  readOnly = false,
  range: controlledRange,
  onRangeChange,
  onCustomer,
}: {
  readOnly?: boolean;
  range?: { start: string; end: string };
  onRangeChange?: (range: { start: string; end: string }) => void;
  onCustomer?: (userId: string) => void;
}) {
  const [preset, setPreset] = useState<Preset>(
    controlledRange &&
      (controlledRange.start !== `${shanghaiToday().slice(0, 7)}-01` ||
        controlledRange.end !== shanghaiToday())
      ? "custom"
      : "month",
  );
  const [custom, setCustom] = useState(() => ({
    start: controlledRange?.start ?? `${shanghaiToday().slice(0, 7)}-01`,
    end: controlledRange?.end ?? shanghaiToday(),
  }));
  const [appliedRange, setAppliedRange] = useState(custom);
  const range = controlledRange ?? appliedRange;
  const rangeError =
    !isCalendarDay(custom.start) || !isCalendarDay(custom.end)
      ? "请选择有效的开始和结束日期。"
      : custom.end < custom.start
        ? "结束日期不能早于开始日期。"
        : custom.end > shanghaiToday()
          ? "结束日期不能晚于今天。"
          : custom.end > shiftDays(custom.start, maxRangeDays - 1)
            ? `单次最多查询 ${maxRangeDays} 天。`
            : "";
  const [overview, setOverview] = useState<BusinessOverview>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");

  const requestSequence = useRef(0);
  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setOverview(undefined);
    setError("");
    if (!isCalendarDay(range.start) || !isCalendarDay(range.end)) {
      setBusy(false);
      return;
    }
    setBusy(true);
    try {
      const query = new URLSearchParams({ start: range.start, end: range.end });
      const result = await adminRead<BusinessOverview>(
        `/api/control/business/overview?${query}`,
        "读取经营看板失败",
      );
      if (sequence === requestSequence.current) setOverview(result);
    } catch (cause) {
      if (sequence !== requestSequence.current) return;
      setOverview(undefined);
      setError(cause instanceof Error ? cause.message : "读取经营看板失败");
    } finally {
      if (sequence === requestSequence.current) setBusy(false);
    }
  }, [range.start, range.end]);

  useEffect(() => {
    void load();
    return () => {
      requestSequence.current += 1;
    };
  }, [load]);

  const metrics = overview?.metrics;
  const prev = overview?.prev;
  const avgOrder =
    metrics && metrics.paying_customers > 0
      ? metrics.recharge_fen / metrics.paying_customers
      : null;
  const avgOrderPrev =
    prev && prev.paying_customers > 0
      ? prev.recharge_fen / prev.paying_customers
      : null;

  const cards: {
    label: string;
    value: string;
    sub?: string;
    delta?: string;
    warn?: boolean;
    basis?: string;
  }[] = [];
  if (metrics && prev) {
    const current = incompleteEvidence(metrics);
    const previous = incompleteEvidence(prev);
    const incomplete =
      current.income > 0 || current.cost > 0 || current.pending > 0;
    const prevIncomplete =
      previous.income > 0 || previous.cost > 0 || previous.pending > 0;
    const gross =
      metrics.gross_fen === undefined
        ? metrics.revenue_fen - metrics.cost_fen
        : metrics.gross_fen;
    const grossPrev =
      prev.gross_fen === undefined
        ? prev.revenue_fen - prev.cost_fen
        : prev.gross_fen;
    const pending = current.pending > 0 ? `${current.pending} 笔待结算` : "";
    cards.push(
      {
        label: "充值实收",
        value: formatFen(metrics.recharge_fen),
        delta: delta(metrics.recharge_fen, prev.recharge_fen),
      },
      {
        label: "已确认收入",
        value: formatFen(metrics.revenue_fen),
        sub:
          [current.income > 0 && `${current.income} 笔收入待核对`, pending]
            .filter(Boolean)
            .join(" · ") || undefined,
        delta:
          current.income ||
          previous.income ||
          current.pending ||
          previous.pending
            ? undefined
            : delta(metrics.revenue_fen, prev.revenue_fen),
      },
      {
        label: "已确认成本",
        value: formatFen(metrics.cost_fen),
        sub:
          [current.cost > 0 && `${current.cost} 笔成本待核对`, pending]
            .filter(Boolean)
            .join(" · ") || undefined,
        delta:
          current.cost || previous.cost || current.pending || previous.pending
            ? undefined
            : delta(metrics.cost_fen, prev.cost_fen),
      },
      {
        label: "毛利 / 毛利率",
        value: incomplete || gross === null ? "待核对" : formatFen(gross),
        sub:
          incomplete || gross === null
            ? "账务核对后显示毛利"
            : metrics.revenue_fen > 0
              ? `毛利率 ${Math.round(metrics.margin_pct ?? (gross / metrics.revenue_fen) * 100)}%`
              : "无收入，无法计算毛利率",
        delta:
          incomplete || prevIncomplete || gross === null || grossPrev === null
            ? undefined
            : delta(gross, grossPrev),
        warn: incomplete || gross === null,
      },
      {
        label: "付费客户数",
        value: `${metrics.paying_customers}`,
        delta: delta(metrics.paying_customers, prev.paying_customers),
      },
      {
        label: "新增付费客户",
        value: `${metrics.new_paying_customers}`,
        delta: delta(metrics.new_paying_customers, prev.new_paying_customers),
      },
      {
        label: "付费客户平均充值",
        value: avgOrder === null ? "暂无付费客户" : formatFen(avgOrder),
        delta:
          avgOrder === null || avgOrderPrev === null
            ? undefined
            : delta(avgOrder, avgOrderPrev),
      },
      {
        label: "当前客户积分余额",
        value: `${metrics.prepaid_credits} 积分`,
        sub:
          metrics.prepaid_fen === null
            ? "含可用和冻结积分"
            : `含可用和冻结积分 · 约 ${formatFen(metrics.prepaid_fen)}`,
      },
    );
  }

  const bases = [
    "所选日期内实际收到的充值款，含线下收款。",
    "客户已使用的积分中，能确认对应充值款的金额。赠送积分的收入为零。",
    "已经确认的服务费用，包括重试产生的费用。待核对费用会单独提示。",
    "毛利等于已确认收入减去成本。毛利率是毛利占收入的比例。",
    "所选日期内实际充值过的客户数，每位客户只计一次。",
    "首次实际付款充值的日期落在所选日期内的客户数。",
    "所选日期内收到的充值款除以付费客户数。",
    "切换日期也显示此刻的积分余额。金额按当前比例折算，仅供参考。",
  ];
  cards.forEach((card, index) => {
    card.basis = bases[index];
  });
  const daily = overview?.daily ?? [];
  const trendRef = useRef<SVGSVGElement>(null);
  const [trendWidth, setTrendWidth] = useState(600);
  const [trendMode, setTrendMode] = useState<"amount" | "margin">("amount");
  const [trendDetail, setTrendDetail] = useState("");
  const selectedTrendDay = daily.find((day) => day.day === trendDetail);
  useEffect(() => {
    if (daily.length === 0) return;
    const element = trendRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) =>
      setTrendWidth(Math.max(300, entries[0].contentRect.width)),
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [daily.length]);
  const maxDaily = Math.max(
    1,
    ...daily.map((day) =>
      Math.max(
        day.known_revenue_fen ?? day.revenue_fen ?? 0,
        day.known_cost_fen ?? day.cost_fen ?? 0,
      ),
    ),
  );

  async function copyCustomerId(userId: string) {
    try {
      await navigator.clipboard.writeText(userId);
      setCopied(userId);
    } catch {
      setCopied("");
    }
  }

  function applyRange(next: typeof range) {
    if (next.start === range.start && next.end === range.end) return;
    setTrendDetail("");
    setAppliedRange(next);
    onRangeChange?.(next);
  }

  return (
    <section className="admin-panel business-dashboard" aria-label="经营看板">
      <form
        className="billing-economics-filters"
        aria-label="经营看板时间选择"
        onSubmit={(event) => {
          event.preventDefault();
          if (preset !== "custom" || rangeError) return;
          if (custom.start === range.start && custom.end === range.end) {
            void load();
          } else {
            applyRange(custom);
          }
        }}
      >
        <label>
          时间
          <select
            value={preset}
            onChange={(event) => {
              const next = event.target.value as Preset;
              setPreset(next);
              if (next === "custom") setCustom(range);
              else applyRange(presetRange(next, range));
            }}
          >
            <option value="today">今日</option>
            <option value="week">本周</option>
            <option value="month">本月</option>
            <option value="lastMonth">上月</option>
            <option value="custom">自定义</option>
          </select>
        </label>
        {preset === "custom" && (
          <>
            <label>
              开始日期
              <input
                type="date"
                value={custom.start}
                onChange={(event) =>
                  setCustom({ ...custom, start: event.target.value })
                }
                required
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                value={custom.end}
                min={custom.start}
                max={shanghaiToday()}
                onChange={(event) =>
                  setCustom({ ...custom, end: event.target.value })
                }
                required
              />
            </label>
            <button type="submit" disabled={busy || !!rangeError}>
              查询
            </button>
          </>
        )}
      </form>
      {preset === "custom" && rangeError && (
        <p role="status" className="admin-hint">
          {rangeError}
        </p>
      )}
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {busy && <p role="status">正在计算…</p>}
      {metrics && prev && (
        <>
          <p className="admin-hint">
            所选日期 {overview?.start} 至 {overview?.end} · 对比日期{" "}
            {overview?.prev_start} 至 {overview?.prev_end}
            （紧邻上一段，天数相同，北京时间）。{" "}
            <a href={`#admin/costReview?${new URLSearchParams(range)}`}>
              查看本区间账务明细
            </a>
          </p>
          <section
            className="economics-kpis economics-kpis--four business-kpis"
            aria-label="经营指标卡"
          >
            {cards.map((card, index) => (
              <article
                key={card.label}
                className={card.warn ? "business-card--warn" : undefined}
              >
                <span>
                  {card.label}{" "}
                  <button
                    type="button"
                    className="metric-help"
                    aria-label={`了解${card.label}怎么算`}
                    aria-describedby={`business-basis-${index}`}
                  >
                    ⓘ
                    <span role="tooltip" id={`business-basis-${index}`}>
                      {card.basis}
                    </span>
                  </button>
                </span>
                <strong>{card.value}</strong>
                {card.sub && <small>{card.sub}</small>}
                {card.delta && <small>环比 {card.delta}</small>}
              </article>
            ))}
          </section>
          <section className="business-trend-panel" aria-label="经营趋势">
            <div className="business-trend-heading">
              <h3>按日经营趋势</h3>
              <fieldset aria-label="趋势指标">
                <button
                  type="button"
                  aria-pressed={trendMode === "amount"}
                  onClick={() => {
                    setTrendMode("amount");
                    setTrendDetail("");
                  }}
                >
                  收入与成本
                </button>
                <button
                  type="button"
                  aria-pressed={trendMode === "margin"}
                  onClick={() => {
                    setTrendMode("margin");
                    setTrendDetail("");
                  }}
                >
                  毛利率
                </button>
              </fieldset>
            </div>
            {daily.length === 0 ? (
              <p>这个区间没有消耗记录。</p>
            ) : (
              <svg
                className="business-trend"
                ref={trendRef}
                viewBox={`0 0 ${trendWidth} 130`}
                role="img"
                aria-label={
                  trendMode === "amount"
                    ? "收入与成本按日趋势图"
                    : "毛利率按日趋势图"
                }
                preserveAspectRatio="none"
              >
                {(() => {
                  const width = trendWidth;
                  const step = (width - 60) / daily.length;
                  const rates = daily.map(dayMargin);
                  const plottedRates = rates.map((rate) =>
                    rate !== null && rate >= -100 && rate <= 100 ? rate : null,
                  );
                  const y = (rate: number) => 100 - ((rate + 100) / 200) * 85;
                  const path = plottedRates
                    .map((rate, i) =>
                      rate === null
                        ? ""
                        : `${i === 0 || plottedRates[i - 1] === null ? "M" : "L"}${30 + (i + 0.5) * step},${y(rate)}`,
                    )
                    .join(" ");
                  const marginMode = trendMode === "margin";
                  return (
                    <>
                      <text x={0} y={12} fontSize={10}>
                        {marginMode ? "100%" : formatFen(maxDaily)}
                      </text>
                      <text x={0} y={105} fontSize={10}>
                        {marginMode ? "−100%" : "¥0"}
                      </text>
                      {daily.map((day, i) => {
                        const revenue =
                          day.known_revenue_fen ?? day.revenue_fen ?? 0;
                        const cost = day.known_cost_fen ?? day.cost_fen ?? 0;
                        const x = 30 + i * step;
                        const rate = rates[i];
                        const detail = dayDescription(day);
                        return (
                          <g key={day.day} aria-label={detail}>
                            <title>{detail}</title>
                            {!marginMode && (
                              <>
                                <rect
                                  x={x + step * 0.12}
                                  y={100 - (revenue / maxDaily) * 85}
                                  width={step * 0.28}
                                  height={(revenue / maxDaily) * 85}
                                  className="business-trend__revenue"
                                />
                                <rect
                                  x={x + step * 0.43}
                                  y={100 - (cost / maxDaily) * 85}
                                  width={step * 0.28}
                                  height={(cost / maxDaily) * 85}
                                  className="business-trend__cost"
                                />
                              </>
                            )}
                            {marginMode &&
                              rate !== null &&
                              rate !== undefined && (
                                <circle
                                  cx={x + step * 0.5}
                                  cy={y(Math.max(-100, Math.min(100, rate)))}
                                  r={plottedRates[i] === null ? 4 : 2.5}
                                  className={
                                    plottedRates[i] === null
                                      ? "business-trend__outlier"
                                      : "business-trend__margin"
                                  }
                                />
                              )}
                            <text x={x} y={123} fontSize={10}>
                              {i === 0 || i === daily.length - 1
                                ? day.day.slice(5)
                                : ""}
                            </text>
                          </g>
                        );
                      })}
                      {marginMode && (
                        <path
                          d={path}
                          className="business-trend__margin-line"
                        />
                      )}
                    </>
                  );
                })()}
              </svg>
            )}
            <p className="admin-hint">
              {trendMode === "amount"
                ? "金柱为已确认收入，灰柱为已确认成本。"
                : "空心点表示毛利率超出图表范围。可选日期看具体数值。账务待核对或收入为零的日期留空。"}
            </p>
            {trendMode === "margin" && daily.length > 0 && (
              <label>
                查看某日
                <select
                  value={trendDetail}
                  onChange={(event) => setTrendDetail(event.target.value)}
                >
                  <option value="">选择日期</option>
                  {daily.map((day) => (
                    <option key={day.day} value={day.day}>
                      {day.day}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {selectedTrendDay && (
              <p role="status" className="admin-hint">
                {dayDescription(selectedTrendDay)}
              </p>
            )}
          </section>
          {(overview?.monthly?.length ?? 0) > 0 && (
            <section aria-label="近6月按月汇总">
              <h3>近 6 月按月汇总</h3>
              <div className="admin-table-scroll">
                <table
                  className="admin-data-table"
                  aria-label="近6月收入成本与毛利率"
                >
                  <thead>
                    <tr>
                      <th>月份</th>
                      <th>收入</th>
                      <th>成本</th>
                      <th>毛利率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview?.monthly?.map((row) => (
                      <tr key={row.month}>
                        <td>{row.month.slice(0, 7)}</td>
                        <td>
                          {row.revenue_fen == null
                            ? row.known_revenue_fen != null
                              ? `${formatFen(row.known_revenue_fen)}（已确认部分）`
                              : "待核对"
                            : formatFen(row.revenue_fen)}
                        </td>
                        <td>
                          {row.cost_fen == null
                            ? row.known_cost_fen != null
                              ? `${formatFen(row.known_cost_fen)}（已确认部分）`
                              : "待核对"
                            : formatFen(row.cost_fen)}
                        </td>
                        <td>
                          {row.margin_pct == null
                            ? row.revenue_fen == null || row.cost_fen == null
                              ? "待核对"
                              : "无收入基数"
                            : `${row.margin_pct.toFixed(1)}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="admin-hint">
                北京时间最近六个自然月。仅显示有账务记录的月份。
              </p>
            </section>
          )}
          <h3>业务构成</h3>
          <div className="admin-table-scroll">
            <table
              className="admin-data-table"
              aria-label="业务模块收入成本构成"
            >
              <thead>
                <tr>
                  <th>业务模块</th>
                  <th>确认收入</th>
                  <th>成本</th>
                  <th>毛利</th>
                </tr>
              </thead>
              <tbody>
                {overview?.modules.map((module) => (
                  <tr key={module.service}>
                    <td>{module.label}</td>
                    <td>{formatFen(module.revenue_fen)}</td>
                    <td>
                      {formatFen(module.cost_fen)}
                      {module.unknown_cost_count > 0 && (
                        <>（另有 {module.unknown_cost_count} 项待核对）</>
                      )}
                    </td>
                    <td>
                      {module.unknown_cost_count +
                        module.pending_count +
                        (module.unknown_revenue_count ?? 0) +
                        (overview.metrics.legacy_cost_count ?? 0) +
                        (overview.metrics.legacy_settlement_count ?? 0) >
                      0
                        ? "待核对"
                        : formatFen(module.revenue_fen - module.cost_fen)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>确认收入前 10 位客户</h3>
          <div className="admin-table-scroll">
            <table
              className="admin-data-table"
              aria-label="确认收入前 10 位客户"
            >
              <thead>
                <tr>
                  <th>客户</th>
                  <th>确认收入</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {(overview?.top_customers.length ?? 0) === 0 && (
                  <tr>
                    <td colSpan={3}>这个区间没有客户消耗记录。</td>
                  </tr>
                )}
                {overview?.top_customers.map((customer) => (
                  <tr key={customer.user_id}>
                    <td>
                      <button
                        type="button"
                        className="table-link-button"
                        onClick={() => {
                          if (onCustomer) onCustomer(customer.user_id);
                          else
                            window.location.hash = `admin/customersMgmt?userId=${encodeURIComponent(customer.user_id)}`;
                        }}
                      >
                        {customer.display_name || customer.username}
                      </button>
                      <small>（{customer.username}）</small>
                    </td>
                    <td>{formatFen(customer.revenue_fen)}</td>
                    <td>
                      {/* 编号只用于复制：找人靠名称，不靠记 ID。 */}
                      {!readOnly && (
                        <button
                          type="button"
                          onClick={() => void copyCustomerId(customer.user_id)}
                        >
                          {copied === customer.user_id ? "已复制" : "复制编号"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {!metrics && !busy && !error && <p>这个区间没有经营数据。</p>}
    </section>
  );
}
