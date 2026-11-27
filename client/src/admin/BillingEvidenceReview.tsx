import { useEffect, useRef, useState } from "react";
import {
  adminRead,
  adminWrite,
  createIdempotencyKey,
  downloadBillingCsv,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { useCan } from "./ui/permissions";
import { formatDateTime, formatFen } from "./ui/vocabulary";

const kinds = { cost: "成本", revenue: "收入", legacy_settlement: "历史结算" };
const states = {
  evidence: "待补凭证",
  review: "待复核",
  verified: "已核对",
  unverifiable: "暂无法核实",
};
const reasons = {
  missing_attempt: "找不到服务使用记录",
  attempt_pending: "服务结果还未确认",
  missing_usage: "缺少实际使用量",
  missing_price: "缺少当时的计费单价",
  cost_evidence: "缺少成本凭证",
  missing_funding: "缺少这笔积分的充值或赠送凭证",
  historical_unlinked: "历史流水缺少账务凭证",
};
const suggestions: Record<keyof typeof reasons, string> = {
  missing_attempt: "查找原任务及服务账单。",
  attempt_pending: "先确认任务结果，再核对费用。",
  missing_usage: "查原账单上的时长、数量或次数。",
  missing_price: "查业务当时的价目或服务账单。",
  cost_evidence: "查找原服务账单或结算凭证。",
  missing_funding: "查找客户的原充值订单或赠送记录。",
  historical_unlinked: "对照原流水和业务时间，找到对应记录。",
};
type Kind = keyof typeof kinds;
type State = keyof typeof states;
type Review = {
  subject_id: string;
  kind: Kind;
  service: string;
  customer: string | null;
  occurred_at: string;
  age_days: number;
  reason_code: keyof typeof reasons;
  state: State;
  needs_evidence: boolean;
  owner: string | null;
  next_step: string | null;
  next_review_on: string | null;
  evidence_ref: string | null;
  reviewed_by: string | null;
  last_checked_at: string | null;
  version: number;
};
type Reviews = {
  items: Review[];
  total: number;
  without_owner: number;
  overdue: number;
};
type RevenueBatch = {
  items: { subject_id: string; expected_version: number }[];
  owner: string;
  evidence_ref: string;
  reason: string;
};
const pageSize = 50;
const maxBatchSize = 25;
const initialFilters = {
  kind: "cost" as Kind,
  state: "",
  age: "",
  reason_code: "",
  owner: "",
  subjectIds: "",
};

function batchIds(value: string) {
  return [...new Set(value.split(/[\s,，;；]+/).filter(Boolean))];
}

export function BillingEvidenceReview({
  query,
  refreshRevision,
  readOnly,
  serviceName,
  onInspect,
}: {
  query: string;
  refreshRevision: number;
  readOnly: boolean;
  serviceName: (key: string) => string;
  onInspect: (id: string) => void;
}) {
  const can = useCan();
  const canWrite = !readOnly && can("funds.write");
  const [draft, setDraft] = useState(initialFilters);
  const [filters, setFilters] = useState(initialFilters);
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<Reviews>();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<Review>();
  const [confirmingBatch, setConfirmingBatch] = useState<RevenueBatch>();
  const batchRetry = useRef<{ key: string; fingerprint: string } | null>(null);
  const batchInFlight = useRef(false);
  const [saving, setSaving] = useState(false);
  const [changeReason, setChangeReason] = useState("");
  const params = new URLSearchParams();
  const range = new URLSearchParams(query);
  for (const key of ["start", "end"]) params.set(key, range.get(key) ?? "");
  const { subjectIds, ...otherFilters } = filters;
  for (const [key, value] of Object.entries(otherFilters)) {
    if (value.trim()) params.set(key, value.trim());
  }
  const requestedIds = batchIds(subjectIds);
  for (const id of requestedIds) params.append("subject_id", id);
  const applied = params.toString();
  const draftIds = batchIds(draft.subjectIds);
  const batchError =
    draftIds.length > maxBatchSize
      ? `一次最多查找 ${maxBatchSize} 个编号，请分批粘贴。`
      : draftIds.some((id) => id.length > 100)
        ? "有编号太长，请检查粘贴的内容。"
        : "";
  const canConfirmBatch =
    canWrite &&
    filters.kind === "revenue" &&
    requestedIds.length > 0 &&
    data?.items.length === requestedIds.length &&
    data.items.every((item) => item.needs_evidence);

  useEffect(() => {
    void revision;
    void refreshRevision;
    let active = true;
    setData(undefined);
    setError("");
    void adminRead<Reviews>(
      `/api/control/billing/reviews?${applied}&limit=${pageSize}&offset=${page * pageSize}`,
      "读取凭证核对清单失败",
    )
      .then((result) => {
        if (active) setData(result);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取失败");
      });
    return () => {
      active = false;
    };
  }, [applied, page, revision, refreshRevision]);

  const fields =
    draft.kind === "cost"
      ? Object.keys(reasons).slice(0, 5)
      : draft.kind === "revenue"
        ? ["missing_funding"]
        : ["historical_unlinked"];

  async function save() {
    if (!canWrite || !editing || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await adminWrite<{ confirmed_revenue_fen: string | null }>(
        `/api/control/billing/reviews/${editing.kind}/${encodeURIComponent(editing.subject_id)}`,
        {
          state: editing.state,
          owner: editing.owner,
          next_step: editing.next_step,
          next_review_on: editing.next_review_on || null,
          evidence_ref: editing.evidence_ref ?? "",
          expected_version: editing.version,
        },
        changeReason,
        "保存核对记录失败",
      );
      setNotice(
        result.confirmed_revenue_fen == null
          ? "核对记录已保存。"
          : `核对记录已保存，已确认收入 ${formatFen(Number(result.confirmed_revenue_fen))}。`,
      );
      setEditing(undefined);
      setRevision((value) => value + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存核对记录失败");
    } finally {
      setSaving(false);
    }
  }

  async function saveBatch() {
    if (!canConfirmBatch || !confirmingBatch || saving || batchInFlight.current)
      return;
    const { reason, ...fields } = confirmingBatch;
    const fingerprint = JSON.stringify(confirmingBatch);
    if (batchRetry.current?.fingerprint !== fingerprint) {
      batchRetry.current = { fingerprint, key: createIdempotencyKey() };
    }
    batchInFlight.current = true;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await adminWrite<{
        confirmed_count: number;
        confirmed_revenue_fen: string;
      }>(
        "/api/control/billing/reviews/revenue-batch",
        fields,
        reason,
        "这批收入确认失败",
        batchRetry.current.key,
      );
      setNotice(
        `已核对 ${result.confirmed_count} 条收入，合计 ${formatFen(Number(result.confirmed_revenue_fen))}。`,
      );
      batchRetry.current = null;
      setConfirmingBatch(undefined);
      setRevision((value) => value + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "这批收入确认失败");
    } finally {
      batchInFlight.current = false;
      setSaving(false);
    }
  }

  return (
    <section aria-label="凭证核对清单" className="billing-review">
      <h3>凭证核对</h3>
      <p className="admin-hint">
        筛选待核对的账目，安排负责人和下次跟进日期。天数按北京时间从业务结束日算起，历史流水从入账日算起。
      </p>
      <a href="#admin/funds?sub=billingQueue">去资金中心处理冻结积分</a>
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          if (batchError || saving) return;
          setEditing(undefined);
          setConfirmingBatch(undefined);
          setPage(0);
          setFilters(draft);
          setRevision((value) => value + 1);
        }}
      >
        <label>
          核对类型
          <select
            value={draft.kind}
            onChange={(event) =>
              setDraft({
                ...draft,
                kind: event.target.value as Kind,
                reason_code: "",
              })
            }
          >
            {Object.entries(kinds).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          处理状态
          <select
            value={draft.state}
            onChange={(event) =>
              setDraft({ ...draft, state: event.target.value })
            }
          >
            <option value="">全部状态</option>
            {Object.entries(states).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          距发生日
          <select
            value={draft.age}
            onChange={(event) =>
              setDraft({ ...draft, age: event.target.value })
            }
          >
            <option value="">不限天数</option>
            {["0-7", "8-30", "31-60", "61-90"].map((age) => (
              <option key={age} value={age}>
                {age} 天
              </option>
            ))}
            <option value="over-90">90 天以上</option>
          </select>
        </label>
        <label>
          待核对原因
          <select
            value={draft.reason_code}
            onChange={(event) =>
              setDraft({ ...draft, reason_code: event.target.value })
            }
          >
            <option value="">全部原因</option>
            {fields.map((key) => (
              <option key={key} value={key}>
                {reasons[key as keyof typeof reasons]}
              </option>
            ))}
          </select>
        </label>
        <label>
          负责人
          <input
            value={draft.owner}
            placeholder="管理员账号"
            onChange={(event) =>
              setDraft({ ...draft, owner: event.target.value })
            }
          />
        </label>
        <label>
          核对批次编号
          <textarea
            rows={2}
            value={draft.subjectIds}
            placeholder="粘贴记录编号，用逗号或换行分开"
            onChange={(event) =>
              setDraft({ ...draft, subjectIds: event.target.value })
            }
          />
          <small>最多 25 个编号，按编号跨日期查找。</small>
        </label>
        <button
          type="button"
          aria-pressed={draft.owner === "unassigned"}
          onClick={() =>
            setDraft({
              ...draft,
              owner: draft.owner === "unassigned" ? "" : "unassigned",
            })
          }
        >
          未分配负责人
        </button>
        <button type="submit" disabled={Boolean(batchError) || saving}>
          应用筛选
        </button>
        {can("export") && (
          <button
            type="button"
            disabled={!data}
            onClick={() => {
              setError("");
              void downloadBillingCsv(applied, true)
                .then(setNotice)
                .catch((cause: unknown) =>
                  setError(cause instanceof Error ? cause.message : "导出失败"),
                );
            }}
          >
            下载当前清单（CSV）
          </button>
        )}
      </form>
      {batchError && <PageBanner tone="error">{batchError}</PageBanner>}
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <p role="status">{notice}</p>}
      {canConfirmBatch && !confirmingBatch && (
        <button
          type="button"
          disabled={saving}
          onClick={() => {
            setEditing(undefined);
            setConfirmingBatch({
              items: data.items.map((item) => ({
                subject_id: item.subject_id,
                expected_version: item.version,
              })),
              owner: "",
              evidence_ref: "",
              reason: "",
            });
          }}
        >
          核对本批收入（{data.items.length} 条）
        </button>
      )}
      {canConfirmBatch && confirmingBatch && (
        <form
          className="admin-panel billing-review-edit"
          aria-label="核对本批收入"
          onSubmit={(event) => {
            event.preventDefault();
            void saveBatch();
          }}
        >
          <p className="admin-hint">
            系统会逐笔核对原充值或赠送记录。任何一笔凭证不足，整批都保留待核对状态。
          </p>
          <div className="admin-toolbar">
            {(
              [
                ["owner", "本批负责人账号", 100],
                ["evidence_ref", "本批凭证编号", 500],
                ["reason", "本批核对说明", 500],
              ] as const
            ).map(([key, label, maxLength]) => (
              <label key={key}>
                {label}
                <input
                  required
                  disabled={saving}
                  maxLength={maxLength}
                  value={confirmingBatch[key]}
                  onChange={(event) =>
                    setConfirmingBatch({
                      ...confirmingBatch,
                      [key]: event.target.value,
                    })
                  }
                />
              </label>
            ))}
            <button type="submit" disabled={saving}>
              确认本批收入
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => setConfirmingBatch(undefined)}
            >
              取消批次核对
            </button>
          </div>
        </form>
      )}
      {editing && (
        <form
          className="admin-panel billing-review-edit"
          aria-label="更新核对记录"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <h4>
            更新核对记录 ·{" "}
            {editing.kind === "legacy_settlement"
              ? kinds[editing.kind]
              : serviceName(editing.service)}
          </h4>
          {editing.kind === "legacy_settlement" && (
            <p className="admin-hint">
              确认前会核对原任务、交付结果和冻结扣分记录。历史金额仍按原账务凭证核对。
            </p>
          )}
          <div className="admin-toolbar">
            <label>
              负责人账号
              <input
                value={editing.owner ?? ""}
                maxLength={100}
                required
                onChange={(event) =>
                  setEditing({ ...editing, owner: event.target.value })
                }
              />
            </label>
            <label>
              处理状态
              <select
                value={editing.state}
                onChange={(event) =>
                  setEditing({ ...editing, state: event.target.value as State })
                }
              >
                {Object.entries(states).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              下一步
              <input
                value={editing.next_step ?? ""}
                maxLength={500}
                required
                onChange={(event) =>
                  setEditing({ ...editing, next_step: event.target.value })
                }
              />
            </label>
            <label>
              下次跟进日期
              <input
                type="date"
                value={editing.next_review_on ?? ""}
                required={editing.state === "unverifiable"}
                onChange={(event) =>
                  setEditing({ ...editing, next_review_on: event.target.value })
                }
              />
            </label>
            <label>
              凭证编号
              <input
                value={editing.evidence_ref ?? ""}
                maxLength={500}
                required={editing.state === "verified"}
                onChange={(event) =>
                  setEditing({ ...editing, evidence_ref: event.target.value })
                }
              />
            </label>
            <label>
              本次更新说明
              <input
                value={changeReason}
                required
                onChange={(event) => setChangeReason(event.target.value)}
              />
            </label>
            <button type="submit" disabled={saving}>
              确认保存
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => setEditing(undefined)}
            >
              取消更新
            </button>
          </div>
          {editing.kind === "revenue" && editing.state === "verified" && (
            <p className="admin-hint">
              保存时会核对原充值或赠送记录。确认后，经营报表会更新这笔收入。
            </p>
          )}
        </form>
      )}
      {data ? (
        <>
          <p role="status">
            {requestedIds.length > 0
              ? `本批找到 ${data.total} / ${requestedIds.length} 条`
              : `共 ${data.total} 条`}{" "}
            · 超过 30 天仍未核对完成 {data.overdue} 条 · 未分配负责人{" "}
            {data.without_owner} 条
          </p>
          <DataTable
            ariaLabel="凭证核对明细"
            headers={
              <>
                <th>业务与客户</th>
                <th>距发生日</th>
                <th>原因</th>
                <th>处理状态</th>
                <th>负责人和下一步</th>
                <th>凭证及核对记录</th>
                <th>操作</th>
              </>
            }
          >
            {data.items.length === 0 && (
              <tr>
                <td colSpan={7}>当前筛选下没有核对记录。</td>
              </tr>
            )}
            {data.items.map((item) => (
              <tr key={item.subject_id}>
                <td>
                  {item.kind === "legacy_settlement"
                    ? kinds[item.kind]
                    : serviceName(item.service)}
                  <br />
                  {item.customer ?? "平台后台"}
                  {requestedIds.length > 0 && (
                    <>
                      <br />
                      <small>{item.subject_id}</small>
                    </>
                  )}
                </td>
                <td>
                  {item.age_days} 天<br />
                  {formatDateTime(item.occurred_at)}
                </td>
                <td>
                  {item.needs_evidence
                    ? reasons[item.reason_code]
                    : "账务凭证已齐"}
                </td>
                <td>{states[item.state]}</td>
                <td>
                  {item.owner ?? "未分配负责人"}
                  <br />
                  {item.next_step ||
                    (item.needs_evidence && suggestions[item.reason_code])}
                  <br />
                  {item.next_review_on && `下次跟进 ${item.next_review_on}`}
                </td>
                <td>
                  {item.evidence_ref ?? "—"}
                  <br />
                  {item.reviewed_by && `复核 ${item.reviewed_by}`}
                  {item.last_checked_at && (
                    <>
                      <br />
                      {formatDateTime(item.last_checked_at)}
                    </>
                  )}
                </td>
                <td>
                  {item.kind !== "legacy_settlement" && (
                    <button
                      type="button"
                      onClick={() => onInspect(item.subject_id)}
                    >
                      查看账务详情
                    </button>
                  )}
                  {canWrite && (
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => {
                        setConfirmingBatch(undefined);
                        setEditing(item);
                        setChangeReason("");
                      }}
                    >
                      更新进度
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </DataTable>
          <nav aria-label="核对清单分页">
            <button
              type="button"
              disabled={page === 0}
              onClick={() => setPage(page - 1)}
            >
              上一页
            </button>{" "}
            第 {page + 1} 页{" "}
            <button
              type="button"
              disabled={(page + 1) * pageSize >= data.total}
              onClick={() => setPage(page + 1)}
            >
              下一页
            </button>
          </nav>
        </>
      ) : (
        !error && <p role="status">正在加载凭证核对…</p>
      )}
    </section>
  );
}
