import { useEffect, useState } from "react";
import { exportBillingReportCsv, exportSummaryReportCsv } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";

/**
 * ADM-13 报表中心：把散落在各页的导出收进一个入口。
 *
 * 老板看数不再逐页找导出按钮：选定月份（默认本月）一键出经营/资金月报与
 * 账务明细；客户清单、调账、审计三类底稿附在下方说明各自入口。
 */
function monthBounds(month: string): { start: string; end: string } {
  const [year, mon] = month.split("-").map(Number);
  const first = new Date(Date.UTC(year, mon - 1, 1));
  const last = new Date(Date.UTC(year, mon, 0));
  return {
    start: first.toISOString().slice(0, 10),
    end: last.toISOString().slice(0, 10),
  };
}

export function ReportsPage({ readOnly = false }: { readOnly?: boolean }) {
  const [month, setMonth] = useState(() =>
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
    })
      .format(new Date())
      .slice(0, 7),
  );
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const range = monthBounds(month);
  const items: Array<{
    key: string;
    title: string;
    hint: string;
    run: () => Promise<{ filename: string }>;
  }> = [
    {
      key: "business",
      title: "经营月报",
      hint: "收入、成本、毛利与业务模块构成（与经营看板同口径）",
      run: () =>
        exportSummaryReportCsv({
          kind: "business",
          start_date: range.start,
          end_date: range.end,
        }),
    },
    {
      key: "funds",
      title: "资金月报",
      hint: "充值实收、赠送、退款与预收余额（与资金中心同口径）",
      run: () =>
        exportSummaryReportCsv({
          kind: "funds",
          start_date: range.start,
          end_date: range.end,
        }),
    },
    {
      key: "billing",
      title: "账务明细",
      hint: "按客户×业务的积分流水与成本明细（最长 90 天）",
      run: () =>
        exportBillingReportCsv({
          start_date: range.start,
          end_date: range.end,
        }),
    },
  ];

  async function download(item: (typeof items)[number]) {
    if (readOnly || busy) return;
    setBusy(item.key);
    setError("");
    try {
      const result = await item.run();
      setNotice(
        `已导出 ${result.filename}；范围 ${range.start} 至 ${range.end}。`,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="admin-panel admin-reports-page" aria-label="报表中心">
      <header>
        <h2>报表中心</h2>
        <p className="admin-hint">
          统一出口：选定月份后一键导出经营月报、资金月报与账务明细；月份为
          北京时间自然月。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <form
        className="admin-toolbar"
        onSubmit={(event) => event.preventDefault()}
      >
        <label>
          报表月份
          <input
            type="month"
            value={month}
            onChange={(event) => setMonth(event.target.value)}
          />
        </label>
      </form>
      <div className="admin-viral-tiles">
        {items.map((item) => (
          <div className="admin-viral-tile" key={item.key}>
            <dt>{item.title}</dt>
            <dd>
              <button
                type="button"
                disabled={readOnly || busy !== "" || !month}
                onClick={() => void download(item)}
              >
                {busy === item.key ? "正在导出…" : "导出 CSV"}
              </button>
              <small>{item.hint}</small>
            </dd>
          </div>
        ))}
      </div>
      <p className="admin-hint">
        底稿类导出保留在各自页面：客户清单（客户列表）、调账记录（资金中心 →
        人工调整）、审计日志（管理操作日志）——它们带各自的筛选参数，从原页
        导出才能带上当前筛选。
      </p>
    </section>
  );
}
