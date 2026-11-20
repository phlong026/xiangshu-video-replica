import { useCallback, useEffect, useState } from "react";
import {
  type AdminDiagnosisReport,
  type AdminDiagnosisReportPage,
  listAdminAccountDiagnosisReports,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * 账号诊断监控（只读）：报告状态分布 + 明细检索。
 *
 * 诊断在客户请求事务内同步执行，没有常驻任务；这里回答两个运维问题——
 * 「上游失败多不多」「谁在用/有没有报错」，用 status 过滤 + sec_uid 精查。
 */

const STATUS_LABELS: Record<string, string> = {
  PENDING: "排队",
  RUNNING: "执行中",
  SUCCEEDED: "成功",
  FAILED: "失败",
};

type StatusFilter = AdminDiagnosisReport["status"] | "all";
const STATUS_FILTERS: Array<{ id: StatusFilter; label: string }> = [
  { id: "all", label: "全部" },
  { id: "SUCCEEDED", label: "成功" },
  { id: "FAILED", label: "失败" },
  { id: "RUNNING", label: "执行中" },
  { id: "PENDING", label: "排队" },
];

function formatTime(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 19).replace("T", " ");
}

export function AdminAccountDiagnosisPage({ readOnly }: { readOnly: boolean }) {
  const [page, setPage] = useState<AdminDiagnosisReportPage | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [secUserId, setSecUserId] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setPage(
        await listAdminAccountDiagnosisReports({
          status: statusFilter === "all" ? undefined : statusFilter,
          secUserId: secUserId.trim() || undefined,
          limit: 30,
          offset: 0,
        }),
      );
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "读取失败，请稍后重试。",
      );
    } finally {
      setLoading(false);
    }
  }, [statusFilter, secUserId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const summary = page?.statusCounts ?? {};

  return (
    <section className="admin-panel" aria-label="账号诊断监控">
      <PageBanner tone="notice">
        账号诊断在客户请求内同步执行；本页只读展示报告状态与失败原因，
        不提供重放/删除操作。
      </PageBanner>

      <fieldset className="admin-diagnosis-filter">
        <legend className="admin-diagnosis-filter-legend">状态筛选</legend>
        {STATUS_FILTERS.map((filter) => (
          <button
            key={filter.id}
            type="button"
            className={
              statusFilter === filter.id
                ? "admin-diagnosis-chip is-active"
                : "admin-diagnosis-chip"
            }
            onClick={() => setStatusFilter(filter.id)}
          >
            {filter.label}
            {summary[filter.id] !== undefined ? ` (${summary[filter.id]})` : ""}
          </button>
        ))}
        <input
          aria-label="按账号 sec_user_id 精查"
          placeholder="sec_user_id 精查（可选）"
          value={secUserId}
          onChange={(event) => setSecUserId(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void refresh();
          }}
        />
        <button
          type="button"
          className="admin-diagnosis-chip"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "读取中…" : "刷新"}
        </button>
        {readOnly ? (
          <span className="admin-diagnosis-readonly">只读模式</span>
        ) : null}
      </fieldset>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      <DataTable
        ariaLabel="账号诊断报告列表"
        headers={
          <tr>
            <th>时间</th>
            <th>状态</th>
            <th>评分</th>
            <th>账号</th>
            <th>口径</th>
            <th>免费</th>
            <th>失败原因</th>
          </tr>
        }
      >
        {(page?.items ?? []).map((item) => (
          <tr key={item.id}>
            <td>{formatTime(item.created_at)}</td>
            <td>
              <StatusBadge
                tone={
                  item.status === "SUCCEEDED"
                    ? "good"
                    : item.status === "FAILED"
                      ? "danger"
                      : "neutral"
                }
              >
                {STATUS_LABELS[item.status] ?? item.status}
              </StatusBadge>
            </td>
            <td>{item.score !== null ? `${item.score} 分` : "—"}</td>
            <td>
              {item.unique_id ? `@${item.unique_id}` : item.sec_user_id || "—"}
            </td>
            <td>{item.data_level ?? "—"}</td>
            <td>{item.is_free ? "免费" : "积分"}</td>
            <td>
              {item.error_code
                ? `${item.error_code}${item.error_message ? `：${item.error_message}` : ""}`
                : "—"}
            </td>
          </tr>
        ))}
        {!loading && (page?.items ?? []).length === 0 ? (
          <tr>
            <td colSpan={7}>暂无诊断记录</td>
          </tr>
        ) : null}
      </DataTable>
      <p className="admin-diagnosis-total">
        共 {page?.total ?? 0} 条（本页 {(page?.items ?? []).length} 条）
      </p>
    </section>
  );
}
