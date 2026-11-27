import { useCallback, useEffect, useState } from "react";
import {
  exportBillingReportCsv,
  exportSummaryReportCsv,
  listRecentExports,
  type RecentExport,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { useCan } from "./ui/permissions";
import { formatDateTime } from "./ui/vocabulary";

/**
 * ADM-13 报表中心：把散落在各页的导出收进一个入口。
 *
 * 老板看数不再逐页找导出按钮：选定区间（月 / 季度 / 自定义，默认本月）一键出经营 / 资金
 * 报表与账务明细，并在下方列出最近 10 次导出（谁、何时、什么范围）；客户清单、调账、
 * 审计三类底稿附在下方说明各自入口。区间一律按北京时间自然日。
 */
type RangeMode = "month" | "quarter" | "custom";

/** 账务明细服务端上限（一个自然季度最长 92 天）。 */
const BILLING_MAX_DAYS = 92;
/** 汇总报表服务端上限。 */
const SUMMARY_MAX_DAYS = 366;

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** 非法或半成品的月份一律返回 null。
 *
 * `type="month"` 允许用户输入到一半（空串、"2026-"、"abc"）：直接进 `Date.UTC`
 * 会得到 Invalid Date，`toISOString()` 抛 RangeError 并卸载整棵管理端树
 * （main 侧修过同一处事故）。这里先做格式校验并回读一致性双保险。 */
function monthBounds(month: string): { start: string; end: string } | null {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) return null;
  const [year, mon] = month.split("-").map(Number);
  const start = isoDay(new Date(Date.UTC(year, mon - 1, 1)));
  const end = isoDay(new Date(Date.UTC(year, mon, 0)));
  if (!start.startsWith(month)) return null;
  return { start, end };
}

function quarterBounds(
  year: number,
  quarter: number,
): { start: string; end: string } {
  const firstMonth = (quarter - 1) * 3;
  return {
    start: isoDay(new Date(Date.UTC(year, firstMonth, 1))),
    end: isoDay(new Date(Date.UTC(year, firstMonth + 3, 0))),
  };
}

function dayCount(start: string, end: string): number {
  return (Date.parse(end) - Date.parse(start)) / 86_400_000 + 1;
}

function shanghaiToday() {
  const date = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const month = date.slice(0, 7);
  const [year, mon] = month.split("-").map(Number);
  return { date, month, year, quarter: Math.ceil(mon / 3) };
}

function describeFilters(filters: Record<string, string>): string {
  const parts: string[] = [];
  if (filters.start || filters.end)
    parts.push(`${filters.start ?? "…"} 至 ${filters.end ?? "…"}`);
  for (const [key, value] of Object.entries(filters)) {
    if (!value || key === "start" || key === "end" || key === "format")
      continue;
    if (key === "subject_id_count") {
      parts.push(`按 ${value} 个编号筛选`);
      continue;
    }
    if (key === "attention") {
      const labels: Record<string, string> = {
        pending: "待结算",
        unknown_cost: "成本待核对",
        unknown_revenue: "收入待核对",
      };
      if (value) parts.push(`只看${labels[value] ?? value}`);
      continue;
    }
    parts.push(`${key}=${value}`);
  }
  return parts.join(" · ") || "全部";
}

export function ReportsPage({
  readOnly: _readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const canExport = useCan()("export");
  const today = shanghaiToday();
  const [mode, setMode] = useState<RangeMode>("month");
  const [month, setMonth] = useState(today.month);
  const [year, setYear] = useState(today.year);
  const [quarter, setQuarter] = useState(today.quarter);
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [recent, setRecent] = useState<RecentExport[] | null>(null);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const loadRecent = useCallback(async () => {
    try {
      setRecent((await listRecentExports()).items);
    } catch {
      // 导出记录是辅助信息：读不到不影响导出本身。
      setRecent([]);
    }
  }, []);
  useEffect(() => {
    void loadRecent();
  }, [loadRecent]);

  const selectedRange =
    mode === "month"
      ? month
        ? monthBounds(month)
        : null
      : mode === "quarter"
        ? Number.isInteger(year) && year >= 2000 && year <= 2100
          ? quarterBounds(year, quarter)
          : null
        : customStart && customEnd
          ? { start: customStart, end: customEnd }
          : null;
  const throughToday =
    !!selectedRange &&
    mode !== "custom" &&
    selectedRange.start <= today.date &&
    selectedRange.end > today.date;
  const range =
    throughToday && selectedRange
      ? { ...selectedRange, end: today.date }
      : selectedRange;
  const days = range ? dayCount(range.start, range.end) : 0;
  const rangeProblem = !range
    ? "请先选定报表区间。"
    : days < 1
      ? "结束日期不能早于开始日期。"
      : days > SUMMARY_MAX_DAYS
        ? `区间最长 ${SUMMARY_MAX_DAYS} 天。`
        : range.end > today.date
          ? "报表区间不能晚于今天。"
          : "";
  const periodWord =
    mode === "month" ? "月报" : mode === "quarter" ? "季报" : "报表";

  const items: Array<{
    key: string;
    title: string;
    hint: string;
    disabledReason: string;
    run: (r: { start: string; end: string }) => Promise<{ filename: string }>;
  }> = [
    {
      key: "business",
      title: `经营${periodWord}`,
      hint: "查看收入、成本、毛利和各项业务表现，数字与经营看板一致。",
      disabledReason: "",
      run: (r) =>
        exportSummaryReportCsv({
          kind: "business",
          start_date: r.start,
          end_date: r.end,
        }),
    },
    {
      key: "funds",
      title: `资金${periodWord}`,
      hint: "查看实收充值款、赠送积分、退款和当前积分余额。",
      disabledReason: "",
      run: (r) =>
        exportSummaryReportCsv({
          kind: "funds",
          start_date: r.start,
          end_date: r.end,
        }),
    },
    {
      key: "billing",
      title: "账务明细",
      hint: `查看每笔业务的积分变动和成本，最多选 ${BILLING_MAX_DAYS} 天。`,
      disabledReason:
        days > BILLING_MAX_DAYS
          ? `账务明细区间最长 ${BILLING_MAX_DAYS} 天，请缩短区间。`
          : "",
      run: (r) =>
        exportBillingReportCsv({ start_date: r.start, end_date: r.end }),
    },
  ];

  async function download(item: (typeof items)[number]) {
    if (busy || !range || rangeProblem) return;
    setBusy(item.key);
    setError("");
    try {
      const result = await item.run(range);
      setNotice(
        `已导出 ${result.filename}（${range.start} 至 ${range.end}）。`,
      );
      void loadRecent();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="admin-panel admin-reports-page" aria-label="报表中心">
      <header>
        <p className="admin-hint">
          选择日期后，下载所需报表。日期按北京时间计算。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <form
        className="admin-toolbar"
        onSubmit={(event) => event.preventDefault()}
      >
        <label>
          区间类型
          <select
            value={mode}
            onChange={(event) => setMode(event.target.value as RangeMode)}
          >
            <option value="month">月</option>
            <option value="quarter">季度</option>
            <option value="custom">自定义区间</option>
          </select>
        </label>
        {mode === "month" ? (
          <label>
            报表月份
            <input
              type="month"
              value={month}
              max={today.month}
              onChange={(event) => setMonth(event.target.value)}
            />
          </label>
        ) : null}
        {mode === "quarter" ? (
          <>
            <label>
              年份
              <input
                type="number"
                min={2000}
                max={2100}
                value={year}
                onChange={(event) => setYear(Number(event.target.value))}
              />
            </label>
            <label>
              季度
              <select
                value={quarter}
                onChange={(event) => setQuarter(Number(event.target.value))}
              >
                {[1, 2, 3, 4].map((q) => (
                  <option key={q} value={q}>
                    第 {q} 季度
                  </option>
                ))}
              </select>
            </label>
          </>
        ) : null}
        {mode === "custom" ? (
          <>
            <label>
              开始日期
              <input
                type="date"
                value={customStart}
                max={today.date}
                onChange={(event) => setCustomStart(event.target.value)}
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                min={customStart || undefined}
                max={today.date}
                value={customEnd}
                onChange={(event) => setCustomEnd(event.target.value)}
              />
            </label>
          </>
        ) : null}
      </form>
      <p className="admin-hint" role="status">
        {range && !rangeProblem
          ? `当前区间：${range.start} 至 ${range.end}（共 ${days} 天）${throughToday ? " · 本期截至今天" : ""}`
          : rangeProblem}
      </p>
      {canExport ? null : (
        <PageBanner tone="notice">
          当前角色没有导出权限，仅可查看导出记录。
        </PageBanner>
      )}
      <div className="admin-viral-tiles" hidden={!canExport}>
        {items.map((item) => (
          <article className="admin-viral-tile" key={item.key}>
            <h3>{item.title}</h3>
            <div className="admin-report-actions">
              <button
                type="button"
                aria-label={`导出${item.title} CSV`}
                disabled={
                  busy !== "" || Boolean(rangeProblem) || !!item.disabledReason
                }
                title={item.disabledReason || undefined}
                onClick={() => void download(item)}
              >
                {busy === item.key ? "正在导出…" : "导出 CSV"}
              </button>
              <small>{item.disabledReason || item.hint}</small>
            </div>
          </article>
        ))}
      </div>
      <section aria-label="最近导出记录">
        <h3>最近 10 次导出</h3>
        {recent === null ? (
          <p role="status">正在读取导出记录…</p>
        ) : recent.length === 0 ? (
          <p className="admin-hint">暂无导出记录。</p>
        ) : (
          <ul>
            {recent.map((item) => (
              <li key={`${item.at}:${item.kind}:${item.actor}`}>
                {formatDateTime(item.at)} · {item.actor} · {item.label} ·{" "}
                {describeFilters(item.filters)}
              </li>
            ))}
          </ul>
        )}
      </section>
      <p className="admin-hint">
        客户清单、人工调整记录和管理操作日志可在各自页面下载。下载时会沿用该页的筛选条件。
      </p>
    </section>
  );
}
