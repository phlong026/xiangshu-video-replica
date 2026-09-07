import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AdminGenerationRecord,
  getAdminGenerationRecords,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { TabBar } from "./ui/TabBar";
import {
  formatDateTime,
  GENERATION_RECORD_TYPE_LABELS,
  GENERATION_STATUS_LABELS,
  labelFrom,
} from "./ui/vocabulary";

const PAGE_SIZE = 50;

export function GenerationRecordsPage({
  initialStatus = "",
}: {
  initialStatus?: string;
}) {
  const [items, setItems] = useState<AdminGenerationRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [username, setUsername] = useState("");
  const [status, setStatus] = useState(initialStatus);
  const [recordType, setRecordType] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [filters, setFilters] = useState({
    username: "",
    status: initialStatus,
    recordType: "",
    createdFrom: "",
    createdTo: "",
  });
  const requestIdRef = useRef(0);

  const loadRecords = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    try {
      setLoading(true);
      setError("");
      setItems([]);
      const response = await getAdminGenerationRecords({
        limit: PAGE_SIZE,
        offset,
        username: filters.username || undefined,
        status: filters.status || undefined,
        recordType: filters.recordType || undefined,
        createdFrom: filters.createdFrom || undefined,
        createdTo: filters.createdTo || undefined,
      });
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
  }, [filters, offset]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords]);

  return (
    <section
      aria-label="生成记录"
      className="admin-panel admin-generation-records"
    >
      <TabBar
        active="records"
        ariaLabel="生成记录页签"
        items={[{ id: "records", label: "生成记录" }]}
        onChange={() => {}}
      />
      <div className="admin-actions">
        <button type="button" onClick={() => void loadRecords()}>
          {loading ? "刷新中…" : "刷新记录"}
        </button>
        <span>共 {total} 条</span>
      </div>

      <p className="admin-hint">
        记录视频、口播、图片和 AI
        评分调用。上游未返回精确成本时会明确标注，不以零成本代替。
      </p>
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setFilters({ username, status, recordType, createdFrom, createdTo });
        }}
      >
        <label>
          账号
          <input
            aria-label="生成账号"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          类型
          <select
            aria-label="生成类型"
            value={recordType}
            onChange={(event) => setRecordType(event.target.value)}
          >
            <option value="">全部类型</option>
            <option value="VIDEO">视频</option>
            <option value="ORAL_VIDEO">口播视频</option>
            <option value="FIRST_FRAME_IMAGE">首帧图片</option>
            <option value="CHARACTER_SHEET_IMAGE">人物表</option>
            <option value="CHARACTER_VIEW_IMAGE">人物视图</option>
            <option value="SOURCE_FRAME_PROCESS">素材处理</option>
            <option value="SOURCE_FRAME_AI_SCORE">AI 评分</option>
          </select>
        </label>
        <label>
          状态
          <select
            aria-label="生成状态"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="">全部状态</option>
            {Object.entries(GENERATION_STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          起始时间
          <input
            aria-label="生成起始时间"
            type="date"
            value={createdFrom}
            onChange={(event) => setCreatedFrom(event.target.value)}
          />
        </label>
        <label>
          截止时间
          <input
            aria-label="生成截止时间"
            type="date"
            value={createdTo}
            onChange={(event) => setCreatedTo(event.target.value)}
          />
        </label>
        <button type="submit">查询</button>
      </form>

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
              <th>耗时</th>
              <th>扣减额度</th>
              <th>上游成本（元）</th>
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
              <td>
                <StatusBadge
                  tone={
                    item.status === "SUCCEEDED"
                      ? "good"
                      : item.status === "FAILED"
                        ? "danger"
                        : "warn"
                  }
                >
                  {labelFrom(GENERATION_STATUS_LABELS, item.status)}
                </StatusBadge>
              </td>
              <td>{formatDuration(item.created_at, item.completed_at)}</td>
              <td>
                {item.charged_credits > 0
                  ? `${item.charged_credits} 秒`
                  : "0 秒"}
              </td>
              <td>{formatProviderCost(item)}</td>
              <td>
                <details>
                  <summary>
                    <span>查看详情</span>
                    <small>{formatResult(item)}</small>
                  </summary>
                  <dl>
                    <dt>记录编号</dt>
                    <dd>{item.record_id}</dd>
                    <dt>结果引用</dt>
                    <dd>{item.result_reference ?? "—"}</dd>
                    <dt>供应商任务凭证</dt>
                    <dd>{item.provider_reference ?? "—"}</dd>
                    <dt>错误码</dt>
                    <dd>{item.error_code ?? "—"}</dd>
                    <dt>错误说明</dt>
                    <dd>{item.error_message ?? "—"}</dd>
                    <dt>记录数据</dt>
                    <dd>
                      {item.record_data_status === "CORRUPTED"
                        ? "记录数据损坏"
                        : "正常"}
                    </dd>
                  </dl>
                </details>
              </td>
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

function formatDuration(createdAt: string, completedAt: string | null): string {
  if (!completedAt) return "进行中";
  const seconds = Math.max(
    0,
    Math.round((Date.parse(completedAt) - Date.parse(createdAt)) / 1000),
  );
  if (!Number.isFinite(seconds)) return "—";
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}
