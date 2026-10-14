import { useCallback, useEffect, useRef, useState } from "react";
import {
  type CustomerLifecycleItem,
  type CustomerLifecycleResponse,
  type CustomerRetentionResponse,
  createCustomerAdjustment,
  createIdempotencyKey,
  listCustomerLifecycle,
  readCustomerRetention,
} from "../api.admin";
import { apiBaseUrl } from "../apiBase";
import { CustomerLink } from "./CustomerLink";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { RuleNote } from "./ui/RuleNote";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime, formatFen } from "./ui/vocabulary";

/**
 * M3 客户运营 v2（ADM-10）：生命周期分层 + 健康分 + 批量赠送。
 *
 * 目标读者是运营：先看分布（谁在沉睡），再按分层下钻，勾选目标客户批量
 * 赠送激活积分。批量赠送逐户调用既有调账端点（幂等键逐户独立），失败
 * 中断并列出已成功名单，方便续发。
 */
const PAGE_SIZE = 20;
const MAX_GRANT_CREDITS = 2_147_483_647;

type GrantTarget = {
  userId: string;
  username: string;
  operationId: string;
  idempotencyKey: string;
  done: boolean;
};

type GrantBatch = {
  version: 1;
  id: string;
  apiBase: string;
  sourceDocumentRef: string;
  credits: number;
  reason: string;
  targets: GrantTarget[];
};

function batchStorageKey(base = apiBaseUrl()) {
  // 同一浏览器切换服务端时不能把旧环境的发放请求带到新环境。
  return `admin:customer-grant-batch:v1:${base}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNonBlank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function readBatch(): GrantBatch | null {
  const stored = localStorage.getItem(batchStorageKey());
  if (stored === null) return null;
  const value: unknown = JSON.parse(stored);
  if (
    !isRecord(value) ||
    value.version !== 1 ||
    !isNonBlank(value.id) ||
    value.apiBase !== apiBaseUrl() ||
    !isNonBlank(value.sourceDocumentRef) ||
    !isNonBlank(value.reason) ||
    typeof value.credits !== "number" ||
    !Number.isSafeInteger(value.credits) ||
    value.credits <= 0 ||
    value.credits > MAX_GRANT_CREDITS ||
    !Array.isArray(value.targets) ||
    value.targets.length === 0 ||
    value.targets.length > PAGE_SIZE ||
    !value.targets.every(
      (target: unknown) =>
        isRecord(target) &&
        isNonBlank(target.userId) &&
        isNonBlank(target.username) &&
        isNonBlank(target.operationId) &&
        target.operationId.length <= 128 &&
        isNonBlank(target.idempotencyKey) &&
        typeof target.done === "boolean",
    )
  ) {
    throw new Error("批次恢复记录格式无效");
  }
  const batch = value as GrantBatch;
  for (const field of ["userId", "operationId", "idempotencyKey"] as const) {
    if (
      new Set(batch.targets.map((target) => target[field])).size !==
      batch.targets.length
    ) {
      throw new Error("批次恢复记录包含重复目标或标识");
    }
  }
  return batch;
}

function persistBatch(batch: GrantBatch) {
  if (batch.apiBase !== apiBaseUrl()) {
    throw new Error("服务端地址已更改，已暂停原批次。请切回原服务端后继续。");
  }
  try {
    const key = batchStorageKey(batch.apiBase);
    const serialized = JSON.stringify(batch);
    localStorage.setItem(key, serialized);
    if (localStorage.getItem(key) !== serialized) throw new Error();
  } catch {
    // 首次发送前保存完整请求；收到成功响应后再保存进度。任何失败都停止后续发放。
    throw new Error(
      "无法保存批次恢复记录，已暂停发放。请恢复浏览器存储后继续原批次。",
    );
  }
}

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
  const [data, setData] = useState<CustomerLifecycleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [stage, setStage] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [grantOpen, setGrantOpen] = useState(false);
  const [grantCredits, setGrantCredits] = useState("100");
  const [grantRef, setGrantRef] = useState("");
  const [restored] = useState(() => {
    try {
      return { batch: readBatch(), error: "" };
    } catch {
      return {
        batch: null,
        error:
          "无法读取批次恢复记录，已暂停赠送。请先核对发放流水并恢复浏览器存储，勿直接清除记录后重发。",
      };
    }
  });
  const [batch, setBatch] = useState<GrantBatch | null>(restored.batch);
  const batchRef = useRef(batch);
  const savingRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [progress, setProgress] = useState("");
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

  async function confirmGrant(reason: string) {
    if (readOnly || savingRef.current || restored.error) return;
    let current = batchRef.current;
    if (!current) {
      const credits = Number(grantCredits);
      if (
        !Number.isSafeInteger(credits) ||
        credits <= 0 ||
        credits > MAX_GRANT_CREDITS
      ) {
        setError(`赠送积分必须是 1 至 ${MAX_GRANT_CREDITS} 的整数。`);
        return;
      }
      const targets =
        data?.items.filter((item) => selected.has(item.user_id)) ?? [];
      if (targets.length === 0 || !reason.trim()) return;
      try {
        const existing = readBatch();
        if (existing) {
          batchRef.current = existing;
          setBatch(existing);
          setError("发现已有批次恢复记录，请核对原批次后继续发放。");
          return;
        }
      } catch {
        setError("无法读取批次恢复记录，已暂停赠送。请恢复浏览器存储后重试。");
        return;
      }
      const id = createIdempotencyKey();
      current = {
        version: 1,
        id,
        apiBase: apiBaseUrl(),
        sourceDocumentRef: grantRef.trim() || `batch-${id}`,
        credits,
        reason: reason.trim(),
        targets: targets.map((item) => ({
          userId: item.user_id,
          username: item.username,
          operationId: createIdempotencyKey(),
          idempotencyKey: createIdempotencyKey(),
          done: false,
        })),
      };
      batchRef.current = current;
      setBatch(current);
    }
    savingRef.current = true;
    setSaving(true);
    setError("");
    setNotice("");
    setProgress("");
    try {
      persistBatch(current);
      for (const item of current.targets.filter((target) => !target.done)) {
        const completed = current.targets.filter(
          (target) => target.done,
        ).length;
        setProgress(
          `正在发放（${completed + 1}/${current.targets.length}）：${item.username}`,
        );
        await createCustomerAdjustment(
          item.userId,
          {
            sourceDocumentType: "FREE_GRANT",
            sourceDocumentRef: current.sourceDocumentRef,
            credits: current.credits,
            operationId: item.operationId,
          },
          current.reason,
          item.idempotencyKey,
        );
        current = {
          ...current,
          targets: current.targets.map((target) =>
            target.userId === item.userId ? { ...target, done: true } : target,
          ),
        };
        batchRef.current = current;
        setBatch(current);
        persistBatch(current);
      }
      localStorage.removeItem(batchStorageKey(current.apiBase));
      batchRef.current = null;
      setBatch(null);
      setNotice(
        `已向 ${current.targets.length} 名客户各赠送 ${current.credits} 积分。`,
      );
      setGrantOpen(false);
      setGrantRef("");
      await load();
    } catch (cause) {
      const done = current.targets.filter((target) => target.done);
      setError(
        `发放中断：${done.length}/${current.targets.length} 完成（${done.map((target) => target.username).join("、") || "无"}）。` +
          (cause instanceof Error ? cause.message : ""),
      );
    } finally {
      savingRef.current = false;
      setSaving(false);
      setProgress("");
    }
  }

  return (
    <section className="admin-panel admin-lifecycle-page" aria-label="客户运营">
      <header>
        <p className="admin-hint">
          生命周期分层与健康分定位需要激活的客户；勾选后可批量赠送积分
          （逐户调账、逐户留痕，失败会中断并列出已成功名单）。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {restored.error ? (
        <PageBanner tone="error">{restored.error}</PageBanner>
      ) : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {batch ? (
        <PageBanner tone="notice">
          原批次 {batch.sourceDocumentRef}：已完成{" "}
          {batch.targets.filter((target) => target.done).length}/
          {batch.targets.length}，每户 {batch.credits} 积分。
          目标客户、积分及原因已锁定，刷新后可继续；已成功客户不会重复发放。
          {!readOnly ? (
            <button
              type="button"
              disabled={saving}
              onClick={() => setGrantOpen(true)}
            >
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
        {!readOnly && !batch && !restored.error && selected.size > 0 ? (
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
              {readOnly ? null : (
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
              {readOnly ? null : (
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
      <ConfirmDialog
        open={grantOpen && !readOnly}
        title={batch ? "继续批量赠送积分" : "批量赠送积分"}
        confirmLabel={batch ? "继续发放未完成客户" : "确认发放"}
        description={
          batch
            ? `原批次共 ${batch.targets.length} 名客户，仅继续未完成客户：${
                batch.targets
                  .filter((target) => !target.done)
                  .map((target) => target.username)
                  .join("、") || "无（仅清理完成记录）"
              }。沿用原操作原因及发放凭证。`
            : `将向 ${selected.size} 名客户逐户赠送积分并逐户留痕；发放过程不可中途撤销。`
        }
        busy={saving}
        error={progress || error}
        level={batch ? "standard" : "reason"}
        onClose={() => !saving && setGrantOpen(false)}
        onConfirm={(reason) => void confirmGrant(reason)}
      >
        <label>
          每户赠送积分（正整数）
          <input
            type="number"
            min={1}
            max={MAX_GRANT_CREDITS}
            value={batch?.credits ?? grantCredits}
            disabled={!!batch || saving}
            onChange={(event) => setGrantCredits(event.target.value)}
          />
        </label>
        <label>
          发放单号（可追溯）
          <input
            value={batch?.sourceDocumentRef ?? grantRef}
            disabled={!!batch || saving}
            onChange={(event) => setGrantRef(event.target.value)}
            placeholder="如：十月激活活动"
          />
        </label>
        {batch ? (
          <label>
            原操作原因
            <input value={batch.reason} readOnly />
          </label>
        ) : null}
      </ConfirmDialog>
    </section>
  );
}
