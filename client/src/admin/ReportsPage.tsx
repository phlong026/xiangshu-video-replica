import { useCallback, useEffect, useState } from "react";
import {
  exportBillingReportCsv,
  exportSummaryReportCsv,
  listRecentExports,
  type RecentExport,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
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

function monthBounds(month: string): { start: string; end: string } {
  const [year, mon] = month.split("-").map(Number);
  return {
    start: isoDay(new Date(Date.UTC(year, mon - 1, 1))),
    end: isoDay(new Date(Date.UTC(year, mon, 0))),
  };
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

function shanghaiToday(): { month: string; year: number; quarter: number } {
  const month = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
  })
    .format(new Date())
    .slice(0, 7);
  const [year, mon] = month.split("-").map(Number);
  return { month, year, quarter: Math.ceil(mon / 3) };
}

function describeFilters(filters: Record<string, string>): string {
  const parts: string[] = [];
  if (filters.start || filters.end)
    parts.push(`${filters.start ?? "…"} 至 ${filters.end ?? "…"}`);
  for (const [key, value] of Object.entries(filters)) {
    if (key === "start" || key === "end" || key === "format") continue;
    parts.push(`${key}=${value}`);
  }
  return parts.join("；") || "全部";
}

export function ReportsPage({
  readOnly: _readOnly = false,
}: {
  readOnly?: boolean;
}) {
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

  const range =
    mode === "month"
      ? month
        ? monthBounds(month)
        : null
      : mode === "quarter"
        ? Number.isInteger(year) && year >= 2000
          ? quarterBounds(year, quarter)
          : null
        : customStart && customEnd
          ? { start: customStart, end: customEnd }
          : null;
  const days = range ? dayCount(range.start, range.end) : 0;
  const rangeProblem = !range
    ? "请先选定报表区间。"
    : days < 1
      ? "结束日期不能早于开始日期。"
      : days > SUMMARY_MAX_DAYS
        ? `区间最长 ${SUMMARY_MAX_DAYS} 天。`
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
      hint: "收入、成本、毛利与业务模块构成（与经营看板同口径）",
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
      hint: "充值实收、赠送、退款与预收余额（与资金中心同口径）",
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
      hint: `按客户×业务的积分流水与成本明细（最长 ${BILLING_MAX_DAYS} 天）`,
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
        `已导出 ${result.filename}；范围 ${range.start} 至 ${range.end}。`,
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
          统一出口：选定月份、季度或自定义区间后一键导出经营报表、资金报表与账务明细；
          区间为北京时间自然日。
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
                onChange={(event) => setCustomStart(event.target.value)}
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                min={customStart || undefined}
                value={customEnd}
                onChange={(event) => setCustomEnd(event.target.value)}
              />
            </label>
          </>
        ) : null}
      </form>
      <p className="admin-hint" role="status">
        {range && !rangeProblem
          ? `当前区间：${range.start} 至 ${range.end}（共 ${days} 天）`
          : rangeProblem}
      </p>
      <div className="admin-viral-tiles">
        {items.map((item) => (
          <div className="admin-viral-tile" key={item.key}>
            <dt>{item.title}</dt>
            <dd>
              <button
                type="button"
                disabled={
                  busy !== "" || Boolean(rangeProblem) || !!item.disabledReason
                }
                title={item.disabledReason || undefined}
                onClick={() => void download(item)}
              >
                {busy === item.key ? "正在导出…" : "导出 CSV"}
              </button>
              <small>{item.disabledReason || item.hint}</small>
            </dd>
          </div>
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
        底稿类导出保留在各自页面：客户清单（客户列表）、调账记录（资金中心 →
        人工调整）、审计日志（管理操作日志）——它们带各自的筛选参数，从原页
        导出才能带上当前筛选。
      </p>
    </section>
  );
}
