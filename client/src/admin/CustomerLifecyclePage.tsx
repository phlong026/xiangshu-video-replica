import { useCallback, useEffect, useState } from "react";
import {
  type CustomerLifecycleItem,
  type CustomerLifecycleResponse,
  type CustomerRetentionResponse,
  listCustomerLifecycle,
  readCustomerRetention,
} from "../api.admin";
import { BatchGrantDialog } from "./BatchGrantDialog";
import { type GrantBatch, readGrantBatch } from "./batchAdjustments";
import { CustomerLink } from "./CustomerLink";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { useCan } from "./ui/permissions";
import { RuleNote } from "./ui/RuleNote";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime, formatFen } from "./ui/vocabulary";

/**
 * M3 客户运营 v2（ADM-10）：生命周期分层 + 健康分 + 批量赠送。
 *
 * 目标读者是运营：先看分布（谁在沉睡），再按分层下钻，勾选目标客户批量
 * 赠送激活积分。批量赠送统一走 BatchGrantDialog（与客户列表页同一实现）：
 * 逐户调用既有调账端点、逐户独立幂等键与操作号，失败不阻断其余客户并可只重试
 * 失败部分；整批进度落盘，刷新或崩溃后按原标识继续，不会重复发放。
 */
const PAGE_SIZE = 20;
const STAGE_LABELS: Record<string, string> = {
  new: "新客",
  active: "活跃",
  dormant: "沉睡",
  churned: "流失",
};

const STAGE_FILTERS = ["", "new", "active", "dormant", "churned"] as const;

export function CustomerLifecyclePage({
  readOnly = false,
  onCustomer,
}: {
  readOnly?: boolean;
  onCustomer?: (id: string) => void;
}) {
  // 批量赠送是资金操作：需要 funds.write（页面本身的 readOnly 只管客户类写点）。
  const can = useCan();
  const canGrant = !readOnly && can("funds.write");
  const [data, setData] = useState<CustomerLifecycleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [stage, setStage] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [grantOpen, setGrantOpen] = useState(false);
  // 只在发放对话框开关时读一次恢复记录：有未完成批次就给出「继续」入口；
  // 记录损坏时不在这里重复报错，留给对话框给出可处置的提示。
  const [pendingBatch, setPendingBatch] = useState<GrantBatch | null>(null);
  // 只读取共享恢复记录用于「继续未完成批次」入口；记录损坏时交给对话框给可处置的
  // 报错，这里不重复报一次。挂载与每次关闭对话框后各读一次。
  const refreshPendingBatch = useCallback(() => {
    try {
      setPendingBatch(readGrantBatch());
    } catch {
      setPendingBatch(null);
    }
  }, []);
  useEffect(() => {
    refreshPendingBatch();
  }, [refreshPendingBatch]);
  const [retention, setRetention] = useState<CustomerRetentionResponse | null>(
    null,
  );

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listCustomerLifecycle({
        stage: stage || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setData(response);
      setSelected(new Set());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取客户运营数据失败");
    } finally {
      setLoading(false);
    }
  }, [stage, offset]);

  useEffect(() => {
    void load();
  }, [load]);
  // ADM-23 留存矩阵独立加载：失败不打扰主清单，但必须可见可重试（T32）
  // ——静默置 null 会让「无数据」与「查询失败」不可分辨。
  const [retentionLoading, setRetentionLoading] = useState(false);
  const [retentionError, setRetentionError] = useState("");
  const loadRetention = useCallback(async () => {
    setRetentionLoading(true);
    setRetentionError("");
    try {
      setRetention(await readCustomerRetention());
    } catch (cause) {
      setRetention(null);
      setRetentionError(
        cause instanceof Error ? cause.message : "留存查询失败",
      );
    } finally {
      setRetentionLoading(false);
    }
  }, []);
  useEffect(() => {
    void loadRetention();
  }, [loadRetention]);

  const items = data?.items ?? [];
  const stageCounts = new Map(
    (data?.distribution ?? []).map((row) => [row.stage, row.count]),
  );
  const allCount = [...stageCounts.values()].reduce((sum, n) => sum + n, 0);
  const allSelected =
    items.length > 0 && items.every((item) => selected.has(item.user_id));

  function toggleAll() {
    setSelected(
      allSelected ? new Set() : new Set(items.map((item) => item.user_id)),
    );
  }

  function toggle(user_id: string) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(user_id)) next.delete(user_id);
      else next.add(user_id);
      return next;
    });
  }

  return (
    <section className="admin-panel admin-lifecycle-page" aria-label="客户运营">
      <header>
        <p className="admin-hint">
          生命周期分层与健康分定位需要激活的客户；勾选后可批量赠送积分
          （逐户调账、逐户留痕；中断后可继续未完成部分）。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {pendingBatch ? (
        <PageBanner tone="notice">
          有未完成的发放批次 {pendingBatch.sourceDocumentRef}：已完成{" "}
          {pendingBatch.targets.filter((target) => target.done).length}/
          {pendingBatch.targets.length}，每户 {pendingBatch.credits} 积分。
          继续发放沿用原幂等键与操作号，已成功的客户不会重复发放。
          {!readOnly ? (
            <button type="button" onClick={() => setGrantOpen(true)}>
              继续未完成批次
            </button>
          ) : null}
        </PageBanner>
      ) : null}
      {data ? (
        <>
          {/* 分层卡片：分布由服务端按全量统计（不随所选分层收窄），选中哪层
              都能直接点别的层；aria-pressed 标出当前分层。 */}
          <section className="admin-stat-tiles" aria-label="分层分布">
            {(["", ...STAGE_FILTERS.slice(1)] as string[]).map((value) => (
              <button
                key={value || "all"}
                type="button"
                className="admin-stat-tile"
                aria-pressed={stage === value}
                onClick={() => {
                  setStage(value);
                  setOffset(0);
                }}
              >
                <span>{value ? STAGE_LABELS[value] : "全部分层"}</span>
                <strong>
                  {value ? (stageCounts.get(value) ?? 0) : allCount} 人
                </strong>
              </button>
            ))}
          </section>
          <RuleNote>
            <p className="admin-hint">{data.lifecycleRule}</p>
            <p className="admin-hint">{data.healthRule}</p>
          </RuleNote>
        </>
      ) : null}
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
        }}
      >
        <label>
          分层
          <select
            value={stage}
            onChange={(event) => {
              setStage(event.target.value);
              // T30：分层变了，旧页码指向的窗口已不存在，必须回到第一页。
              setOffset(0);
            }}
          >
            {STAGE_FILTERS.map((value) => (
              <option key={value} value={value}>
                {value ? STAGE_LABELS[value] : "全部分层"}
              </option>
            ))}
          </select>
        </label>
        {canGrant && selected.size > 0 ? (
          <button type="button" onClick={() => setGrantOpen(true)}>
            批量赠送（已选 {selected.size}）
          </button>
        ) : null}
      </form>
      {loading ? (
        <p role="status">正在读取客户运营数据…</p>
      ) : items.length === 0 ? (
        <PageBanner tone="notice">该分层暂无客户。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="客户生命周期清单"
          headers={
            <>
              {!canGrant ? null : (
                <th>
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label="全选本页客户"
                  />
                </th>
              )}
              <th>客户</th>
              <th>分层</th>
              <th>健康分</th>
              <th>最近活跃</th>
              <th>余额 / 30 天任务</th>
            </>
          }
        >
          {items.map((item: CustomerLifecycleItem) => (
            <tr key={item.user_id}>
              {!canGrant ? null : (
                <td>
                  <input
                    type="checkbox"
                    checked={selected.has(item.user_id)}
                    onChange={() => toggle(item.user_id)}
                    aria-label={`选择 ${item.display_name || item.username}`}
                  />
                </td>
              )}
              <td>
                <CustomerLink
                  userId={item.user_id}
                  username={item.username}
                  company={item.display_name || undefined}
                  onCustomer={onCustomer}
                />
              </td>
              <td>
                <StatusBadge
                  tone={
                    item.stage === "active"
                      ? "good"
                      : item.stage === "new"
                        ? "info"
                        : "warn"
                  }
                >
                  {STAGE_LABELS[item.stage] ?? item.stage}
                </StatusBadge>
              </td>
              <td>
                <strong
                  className={
                    item.health_score < (data?.healthAlertScore ?? 40)
                      ? "is-danger-text"
                      : undefined
                  }
                >
                  {item.health_score}
                </strong>
              </td>
              <td>{formatDateTime(item.last_active_at)}</td>
              <td>
                {item.available_credits} 积分 · 成功 {item.succeeded_30d} / 失败{" "}
                {item.failed_30d} · 累计充值{" "}
                {formatFen(item.recharge_total_fen)}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
      {data ? (
        // 服务端返回该分层真实总数；此前用「本页条数 + offset」冒充总数，
        // 满页时「下一页」被禁用，第 2 页之后的客户永远看不到。
        <Pagination
          // T30：后端已下发真实 total，不再用「本页行数+offset」伪造
          // （满一页时恒为 20 条、无法翻页）。
          total={data.total ?? data.items.length + offset}
          limit={PAGE_SIZE}
          offset={offset}
          onPageChange={setOffset}
        />
      ) : null}
      {retentionError ? (
        <div role="alert" className="uc-error">
          {retentionError}
          <button
            type="button"
            disabled={retentionLoading}
            onClick={() => void loadRetention()}
          >
            {retentionLoading ? "重试中…" : "重试"}
          </button>
        </div>
      ) : null}
      {retention?.cohorts?.length ? (
        <section aria-label="客户队列留存">
          <h3>客户队列留存（近 {retention.months} 月）</h3>
          <DataTable
            ariaLabel="客户队列留存矩阵"
            headers={
              <>
                <th>队列（激活月）</th>
                <th>客户数</th>
                <th>各月活跃（月: 人数）</th>
              </>
            }
          >
            {retention.cohorts.map((row) => (
              <tr key={row.cohort}>
                <td>{row.cohort}</td>
                <td>{row.size}</td>
                <td>
                  {Object.entries(row.activeByMonth).length === 0
                    ? "近月无活跃"
                    : Object.entries(row.activeByMonth)
                        .map(([month, count]) => `${month}: ${count}`)
                        .join(" · ")}
                </td>
              </tr>
            ))}
          </DataTable>
          <p className="admin-hint">{retention.rule}</p>
        </section>
      ) : null}
      <BatchGrantDialog
        open={grantOpen && !readOnly}
        targets={(data?.items ?? [])
          .filter((item) => selected.has(item.user_id))
          .map((item) => ({ user_id: item.user_id, username: item.username }))}
        onClose={() => {
          setGrantOpen(false);
          refreshPendingBatch();
        }}
        onFinished={(ids) => {
          setSelected(new Set());
          setNotice(`已向 ${ids.length} 名客户赠送积分，列表已刷新。`);
          void load();
        }}
      />
    </section>
  );
}
