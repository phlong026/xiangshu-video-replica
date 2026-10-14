import { useCallback, useEffect, useState } from "react";
import {
  listPublishStats,
  type PublishStatsItem,
  syncPublishStats,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { FilterBar } from "./ui/FilterBar";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * ADM-18 发布效果回流（默认关闭）：客户发布成片后的公开互动数据。
 *
 * 总开关在「系统设置 → 功能开关」（T28）；打开前需确认对客户发布内容的采集授权口径（改进方案 §7 待拍板）；
 * 同步按条触发（抖音），费用记平台成本。列表即"爆款赋能效果"数据面。
 */
const PAGE_SIZE = 20;

export function PublishStatsPage({ readOnly = false }: { readOnly?: boolean }) {
  const [items, setItems] = useState<PublishStatsItem[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [rule, setRule] = useState("");
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  // 草稿与已提交筛选分开（T22/T23）：点「查询」才发请求。
  const [draftUsername, setDraftUsername] = useState("");
  const [username, setUsername] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [syncingId, setSyncingId] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listPublishStats({
        username: username || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setItems(response.items);
      setEnabled(response.enabled);
      setRule(response.rule);
      setTotal(response.total);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取发布成效失败");
    } finally {
      setLoading(false);
    }
  }, [username, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  async function sync(item: PublishStatsItem) {
    if (readOnly || saving || !enabled) return;
    setSaving(true);
    setSyncingId(item.id);
    setError("");
    try {
      const result = await syncPublishStats({
        id: item.id,
        reason: "同步发布效果",
      });
      setNotice(
        `已同步 ${item.username} 的《${item.title || item.platform_item_id}》：点赞 ${result.stats?.likes ?? "—"}。`,
      );
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "同步失败");
    } finally {
      setSaving(false);
      setSyncingId("");
    }
  }

  return (
    <section className="admin-panel admin-publish-stats" aria-label="发布成效">
      <header>
        <p className="admin-hint">{rule}</p>
        <a href="#admin/systemSettings?sub=features">
          回流开关在「系统设置 → 功能开关」
        </a>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <p>
        <StatusBadge tone={enabled ? "good" : "warn"}>
          {enabled ? "回流已开启" : "回流未开启（默认）"}
        </StatusBadge>
      </p>
      <FilterBar
        ariaLabel="发布成效筛选"
        busy={loading}
        onReset={() => {
          setOffset(0);
          setDraftUsername("");
          setUsername("");
        }}
        onSubmit={() => {
          setOffset(0);
          setUsername(draftUsername.trim());
        }}
      >
        <label>
          客户
          <input
            value={draftUsername}
            onChange={(event) => setDraftUsername(event.target.value)}
            placeholder="用户名"
          />
        </label>
      </FilterBar>
      {loading ? (
        <p role="status">正在读取发布成效…</p>
      ) : items.length === 0 ? (
        <PageBanner tone="notice">暂无已发布记录。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="发布成效清单"
          headers={
            <>
              <th>客户 / 平台</th>
              <th>标题 / 短链</th>
              <th>互动数据</th>
              <th>同步时间</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                {item.display_name || item.username} ·{" "}
                {item.platform === "douyin" ? "抖音" : "视频号"}
              </td>
              <td>
                {item.title || "（无标题）"}
                {item.short_url ? (
                  <>
                    {" · "}
                    <a href={item.short_url} target="_blank" rel="noreferrer">
                      打开
                    </a>
                  </>
                ) : null}
              </td>
              <td>
                {item.stats
                  ? `赞 ${item.stats.likes ?? "—"} · 评 ${item.stats.comments ?? "—"} · 藏 ${
                      item.stats.collects ?? "—"
                    } · 转发 ${item.stats.shares ?? "—"}`
                  : "未同步"}
              </td>
              <td>
                {item.stats_synced_at
                  ? formatDateTime(item.stats_synced_at)
                  : "—"}
              </td>
              <td>
                {!readOnly && item.platform === "douyin" ? (
                  <button
                    type="button"
                    disabled={saving || !enabled}
                    onClick={() => void sync(item)}
                  >
                    {syncingId === item.id ? "同步中…" : "同步"}
                  </button>
                ) : (
                  "—"
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
    </section>
  );
}
