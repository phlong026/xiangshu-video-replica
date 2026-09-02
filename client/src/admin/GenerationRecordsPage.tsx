import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AdminGenerationRecord,
  getAdminGenerationRecords,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import {
  formatDateTime,
  GENERATION_RECORD_TYPE_LABELS,
  labelFrom,
} from "./ui/vocabulary";

const PAGE_SIZE = 50;

export function GenerationRecordsPage() {
  const [items, setItems] = useState<AdminGenerationRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestIdRef = useRef(0);

  const loadRecords = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    try {
      setLoading(true);
      setError("");
      setItems([]);
      const response = await getAdminGenerationRecords(PAGE_SIZE, offset);
      if (requestId !== requestIdRef.current) {
        return;
      }
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      if (requestId !== requestIdRef.current) {
        return;
      }
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, [offset]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords]);

  return (
    <section aria-label="生成记录" className="admin-panel">
      <div className="admin-actions">
        <button type="button" onClick={() => void loadRecords()}>
          {loading ? "刷新中…" : "刷新记录"}
        </button>
        <span>共 {total} 条</span>
      </div>

      <p className="admin-hint">
        记录视频、图片和 AI
        评分调用。上游未返回精确成本时会明确标注，不以零成本代替。
      </p>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      {!loading && items.length === 0 ? (
        <PageBanner tone="notice">暂无生成记录。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="用户生成记录列表"
          headers={
            <>
              <th>时间</th>
              <th>用户</th>
              <th>项目</th>
              <th>生成类型</th>
              <th>服务 / 模型</th>
              <th>状态</th>
              <th>扣减条数</th>
              <th>上游成本</th>
              <th>结果 / 错误</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={`${item.record_type}-${item.record_id}`}>
              <td>{formatDateTime(item.created_at)}</td>
              <td>{item.username}</td>
              <td>{item.project_name ?? "—"}</td>
              <td>
                {labelFrom(GENERATION_RECORD_TYPE_LABELS, item.record_type)}
              </td>
              <td>{formatProvider(item)}</td>
              <td>{item.status}</td>
              <td>{item.charged_credits}</td>
              <td>{formatProviderCost(item)}</td>
              <td>{formatResult(item)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <Pagination
        disabled={loading}
        limit={PAGE_SIZE}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />
    </section>
  );
}

function formatProvider(item: AdminGenerationRecord): string {
  return [item.provider, item.model].filter(Boolean).join(" / ") || "本地处理";
}

function formatProviderCost(item: AdminGenerationRecord): string {
  if (item.provider_cost_status === "NOT_APPLICABLE") {
    return "无付费调用";
  }
  if (
    item.provider_cost_status === "UNAVAILABLE" ||
    item.provider_cost === null
  ) {
    return "上游未回传";
  }
  if (item.provider_cost_status === "ESTIMATED") {
    return `估算 ${item.provider_cost}`;
  }
  return String(item.provider_cost);
}

function formatResult(item: AdminGenerationRecord): string {
  if (item.record_data_status === "CORRUPTED") {
    return "记录数据损坏";
  }
  return item.error_code ?? item.result_reference ?? "—";
}
