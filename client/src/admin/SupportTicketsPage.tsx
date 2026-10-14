import { useCallback, useEffect, useState } from "react";
import {
  createSupportTicket,
  listSupportTickets,
  listTicketHandlers,
  type SupportTicket,
  type TicketHandler,
  updateSupportTicket,
} from "../api.admin";
import { CustomerLink } from "./CustomerLink";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { CustomerPicker, type PickedCustomer } from "./ui/CustomerPicker";
import { DataTable } from "./ui/DataTable";
import { FilterBar } from "./ui/FilterBar";
import { ListPage } from "./ui/ListPage";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * ADM-21 工单/反馈 v1（管理端代录）：客户问题的登记—处理—结案闭环。
 *
 * 状态机 open → processing → resolved（关单后不可复活）；开始处理自动把
 * 当前管理员记为负责人；每次变更带原因入审计。
 */
const PAGE_SIZE = 20;

// 优先级（T15）：P0 最紧急；未结案工单按优先级排在前面。
const PRIORITY_OPTIONS = [
  { value: "P0", label: "P0 紧急" },
  { value: "P1", label: "P1 高" },
  { value: "P2", label: "P2 普通" },
  { value: "P3", label: "P3 低" },
];
const PRIORITY_TONE: Record<string, "danger" | "warn" | "info" | "neutral"> = {
  P0: "danger",
  P1: "warn",
  P2: "info",
  P3: "neutral",
};

/** 关联对象（T14）→ 可点击的跳转地址；处理人不必再从描述里抄编号。 */
function relatedLink(ref: string): { label: string; href: string } | null {
  const [kind, ...rest] = ref.split(":");
  const value = rest.join(":");
  if (!value) return null;
  if (kind === "task")
    return {
      label: `生成任务 ${value}`,
      href: `#admin/generationRecords?${new URLSearchParams({ taskId: value })}`,
    };
  if (kind === "order")
    return {
      label: `充值订单 ${value}`,
      href: `#admin/funds?${new URLSearchParams({ sub: "orders", orderNo: value })}`,
    };
  return null;
}

const CATEGORY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "billing", label: "计费/资金" },
  { value: "generation", label: "生成问题" },
  { value: "publish", label: "发布问题" },
  { value: "content", label: "内容/素材" },
  { value: "account", label: "账号/设备" },
  { value: "other", label: "其他" },
];

const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  // 未结案 = 待处理 + 处理中，与总览「工单未结案」待办同口径。
  { value: "unresolved", label: "未结案" },
  { value: "open", label: "待处理" },
  { value: "processing", label: "处理中" },
  { value: "resolved", label: "已解决" },
];

export function SupportTicketsPage({
  readOnly = false,
  onCustomer,
  initialStatus = "",
}: {
  readOnly?: boolean;
  onCustomer?: (id: string) => void;
  initialStatus?: string;
}) {
  const [items, setItems] = useState<SupportTicket[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  // 草稿与已提交筛选分开（T22/T23）：点「查询」才发请求。
  const emptyFilters = { status: initialStatus, category: "", username: "" };
  const [draft, setDraft] = useState(emptyFilters);
  const [filters, setFilters] = useState(emptyFilters);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [summary, setSummary] = useState("");
  const [newCategory, setNewCategory] = useState("other");
  const [targetCustomer, setTargetCustomer] = useState<PickedCustomer | null>(
    null,
  );
  const [newPriority, setNewPriority] = useState("P2");
  const [relatedKind, setRelatedKind] = useState<"" | "task" | "order">("");
  const [relatedValue, setRelatedValue] = useState("");
  // 指派与改优先级（T14/T15）：负责人候选在首次打开时读取。
  const [assigning, setAssigning] = useState<SupportTicket | null>(null);
  const [handlers, setHandlers] = useState<TicketHandler[] | null>(null);
  const [assignHandler, setAssignHandler] = useState("");
  const [assignPriority, setAssignPriority] = useState("P2");
  const [pendingTicket, setPendingTicket] = useState<SupportTicket | null>(
    null,
  );
  const [resolution, setResolution] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listSupportTickets({
        status: filters.status || undefined,
        category: filters.category || undefined,
        username: filters.username || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取工单失败");
    } finally {
      setLoading(false);
    }
  }, [filters, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  async function submitCreate(reason: string) {
    setSaving(true);
    setError("");
    try {
      await createSupportTicket({
        targetUserId: targetCustomer?.user_id ?? "",
        category: newCategory,
        summary: summary.trim(),
        priority: newPriority,
        relatedRef:
          relatedKind && relatedValue.trim()
            ? `${relatedKind}:${relatedValue.trim()}`
            : "",
        reason,
      });
      setNotice("工单已登记。");
      setCreating(false);
      setSummary("");
      setTargetCustomer(null);
      setNewPriority("P2");
      setRelatedKind("");
      setRelatedValue("");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登记工单失败");
    } finally {
      setSaving(false);
    }
  }

  function openAssign(item: SupportTicket) {
    setAssigning(item);
    setAssignHandler(item.handler_user_id);
    setAssignPriority(item.priority || "P2");
    if (handlers === null) {
      void listTicketHandlers()
        .then((response) => setHandlers(response.items))
        .catch((cause) => {
          setHandlers([]);
          setError(
            cause instanceof Error ? cause.message : "读取可指派负责人失败",
          );
        });
    }
  }

  async function confirmAssign(reason: string) {
    if (!assigning) return;
    setSaving(true);
    setError("");
    try {
      await updateSupportTicket({
        id: assigning.id,
        priority:
          assignPriority !== (assigning.priority || "P2") ? assignPriority : "",
        handlerUserId:
          assignHandler && assignHandler !== assigning.handler_user_id
            ? assignHandler
            : "",
        reason,
      });
      setNotice("工单负责人与优先级已更新。");
      setAssigning(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "更新工单失败");
    } finally {
      setSaving(false);
    }
  }

  async function confirmUpdate(reason: string) {
    if (!pendingTicket) return;
    setSaving(true);
    setError("");
    try {
      const nextStatus =
        pendingTicket.status === "open"
          ? "processing"
          : pendingTicket.status === "processing"
            ? "resolved"
            : "";
      await updateSupportTicket({
        id: pendingTicket.id,
        status: nextStatus,
        resolution: resolution.trim(),
        reason,
      });
      setNotice(
        nextStatus === "resolved"
          ? "工单已结案，结论已留痕。"
          : "工单已开始处理，你被记为负责人。",
      );
      setPendingTicket(null);
      setResolution("");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "更新工单失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <ListPage
        ariaLabel="工单反馈"
        className="admin-support-tickets"
        error={error}
        notice={notice}
        loading={loading}
        hasItems={items.length > 0}
        loadingText="正在读取工单…"
        emptyText="暂无符合条件的工单。"
        actions={
          <header>
            <p className="admin-hint">
              客户问题的登记—处理—结案闭环（v1 管理端代录；客户端提单入口后续
              开放）。状态只能前进：待处理 → 处理中 → 已解决，结案后不可复活。
            </p>
            {!readOnly && !creating ? (
              <button type="button" onClick={() => setCreating(true)}>
                登记工单
              </button>
            ) : null}
          </header>
        }
        filters={
          <FilterBar
            ariaLabel="工单筛选"
            busy={loading}
            onReset={() => {
              setOffset(0);
              setDraft(emptyFilters);
              setFilters(emptyFilters);
            }}
            onSubmit={() => {
              setOffset(0);
              setFilters(draft);
            }}
          >
            <label>
              状态
              <select
                value={draft.status}
                onChange={(e) => setDraft({ ...draft, status: e.target.value })}
              >
                {STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              类目
              <select
                value={draft.category}
                onChange={(e) =>
                  setDraft({ ...draft, category: e.target.value })
                }
              >
                <option value="">全部类目</option>
                {CATEGORY_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              客户
              <input
                value={draft.username}
                onChange={(e) =>
                  setDraft({ ...draft, username: e.target.value })
                }
                placeholder="用户名或公司名"
              />
            </label>
          </FilterBar>
        }
        pagination={
          <Pagination
            total={total}
            limit={PAGE_SIZE}
            offset={offset}
            onPageChange={setOffset}
          />
        }
      >
        <DataTable
          ariaLabel="工单清单"
          headers={
            <>
              <th>状态 / 优先级</th>
              <th>客户 / 类目</th>
              <th>问题描述</th>
              <th>负责人 / 结论</th>
              <th>时间</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                <StatusBadge
                  tone={
                    item.status === "resolved"
                      ? "good"
                      : item.status === "processing"
                        ? "info"
                        : "warn"
                  }
                >
                  {item.status_label}
                </StatusBadge>{" "}
                <StatusBadge
                  tone={PRIORITY_TONE[item.priority ?? "P2"] ?? "info"}
                >
                  {item.priority ?? "P2"}
                </StatusBadge>
              </td>
              <td>
                {item.target_user_id ? (
                  <CustomerLink
                    userId={item.target_user_id}
                    username={item.target_username}
                    company={item.target_display_name || undefined}
                    onCustomer={onCustomer}
                  />
                ) : (
                  "平台侧"
                )}{" "}
                · {item.category_label}
              </td>
              <td>
                {item.summary}
                {(() => {
                  const link = item.related_ref
                    ? relatedLink(item.related_ref)
                    : null;
                  return link ? (
                    <small>
                      <a href={link.href}>{link.label}</a>
                    </small>
                  ) : null;
                })()}
              </td>
              <td>
                {item.handler_username || "—"}
                {item.resolution ? ` · ${item.resolution.slice(0, 40)}` : ""}
              </td>
              <td>{formatDateTime(item.created_at)}</td>
              <td>
                {readOnly || item.status === "resolved" ? (
                  "—"
                ) : (
                  <>
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => {
                        setResolution("");
                        setPendingTicket(item);
                      }}
                    >
                      {item.status === "open" ? "开始处理" : "结案"}
                    </button>{" "}
                    <button
                      type="button"
                      disabled={saving}
                      onClick={() => openAssign(item)}
                    >
                      指派 / 优先级
                    </button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      </ListPage>
      <ConfirmDialog
        open={creating}
        title="登记工单"
        confirmLabel="确认登记"
        description="代录客户反馈或平台侧问题；问题描述与类目必填，关联客户可选。"
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setCreating(false)}
        onConfirm={(reason) => void submitCreate(reason)}
      >
        {/* 客户按名字联想选择（T14），不再粘贴用户编号；不选 = 平台侧问题。 */}
        <CustomerPicker
          label="关联客户（可选，不选 = 平台侧问题）"
          value={targetCustomer}
          onChange={setTargetCustomer}
        />
        <label>
          优先级
          <select
            value={newPriority}
            onChange={(e) => setNewPriority(e.target.value)}
          >
            {PRIORITY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          关联对象（可选）
          <select
            value={relatedKind}
            onChange={(e) =>
              setRelatedKind(e.target.value as "" | "task" | "order")
            }
          >
            <option value="">无</option>
            <option value="task">生成任务号</option>
            <option value="order">充值订单号</option>
          </select>
        </label>
        {relatedKind ? (
          <label>
            {relatedKind === "task" ? "生成任务号" : "充值订单号"}
            <input
              value={relatedValue}
              maxLength={100}
              onChange={(e) => setRelatedValue(e.target.value)}
            />
          </label>
        ) : null}
        <label>
          类目
          <select
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
          >
            {CATEGORY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          问题描述（≤500 字）
          <textarea
            rows={4}
            maxLength={500}
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="客户遇到了什么、如何复现、期望结果"
          />
        </label>
      </ConfirmDialog>
      <ConfirmDialog
        open={assigning !== null}
        title="指派负责人 / 调整优先级"
        confirmLabel="保存"
        description="负责人只能是启用中的管理员；变更带原因入审计。"
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setAssigning(null)}
        onConfirm={(reason) => void confirmAssign(reason)}
      >
        <label>
          负责人
          <select
            value={assignHandler}
            onChange={(e) => setAssignHandler(e.target.value)}
            disabled={handlers === null}
          >
            <option value="">
              {handlers === null ? "正在读取…" : "暂不指派"}
            </option>
            {(handlers ?? []).map((h) => (
              <option key={h.user_id} value={h.user_id}>
                {h.display_name
                  ? `${h.display_name}（${h.username}）`
                  : h.username}
              </option>
            ))}
          </select>
        </label>
        <label>
          优先级
          <select
            value={assignPriority}
            onChange={(e) => setAssignPriority(e.target.value)}
          >
            {PRIORITY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      </ConfirmDialog>
      <ConfirmDialog
        open={pendingTicket !== null}
        title={pendingTicket?.status === "open" ? "开始处理工单" : "结案工单"}
        confirmLabel="确认"
        description={
          pendingTicket
            ? pendingTicket.status === "open"
              ? "工单转为处理中，你将被记为负责人。"
              : "结案后工单不可复活；请填写处理结论供复盘。"
            : ""
        }
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setPendingTicket(null)}
        onConfirm={(reason) => void confirmUpdate(reason)}
      >
        {pendingTicket?.status === "processing" ? (
          <label>
            处理结论（≤2000 字）
            <textarea
              rows={3}
              maxLength={2000}
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              placeholder="原因、处理动作与结果（如已补积分/已修复）"
            />
          </label>
        ) : null}
      </ConfirmDialog>
    </>
  );
}
