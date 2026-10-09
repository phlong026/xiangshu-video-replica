import { useCallback, useEffect, useState } from "react";
import {
  createSupportTicket,
  listSupportTickets,
  type SupportTicket,
  updateSupportTicket,
} from "../api.admin";
import { CustomerLink } from "./CustomerLink";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
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
  { value: "open", label: "待处理" },
  { value: "processing", label: "处理中" },
  { value: "resolved", label: "已解决" },
];

export function SupportTicketsPage({
  readOnly = false,
  onCustomer,
}: {
  readOnly?: boolean;
  onCustomer?: (id: string) => void;
}) {
  const [items, setItems] = useState<SupportTicket[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [username, setUsername] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [summary, setSummary] = useState("");
  const [newCategory, setNewCategory] = useState("other");
  const [targetUser, setTargetUser] = useState("");
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
        status: status || undefined,
        category: category || undefined,
        username: username || undefined,
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
  }, [status, category, username, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  async function submitCreate(reason: string) {
    setSaving(true);
    setError("");
    try {
      await createSupportTicket({
        targetUserId: targetUser.trim(),
        category: newCategory,
        summary: summary.trim(),
        reason,
      });
      setNotice("工单已登记。");
      setCreating(false);
      setSummary("");
      setTargetUser("");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登记工单失败");
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
    <section
      className="admin-panel admin-support-tickets"
      aria-label="工单反馈"
    >
      <header>
        <h2>工单 / 反馈</h2>
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
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
        }}
      >
        <label>
          状态
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
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
            value={category}
            onChange={(e) => setCategory(e.target.value)}
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
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="用户名或公司名"
          />
        </label>
        <button type="submit" disabled={loading}>
          查询
        </button>
      </form>
      {loading ? (
        <p role="status">正在读取工单…</p>
      ) : items.length === 0 ? (
        <PageBanner tone="notice">暂无符合条件的工单。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="工单清单"
          headers={
            <>
              <th>状态</th>
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
              <td>{item.summary}</td>
              <td>
                {item.handler_username || "—"}
                {item.resolution ? ` · ${item.resolution.slice(0, 40)}` : ""}
              </td>
              <td>{formatDateTime(item.created_at)}</td>
              <td>
                {readOnly || item.status === "resolved" ? (
                  "—"
                ) : (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => {
                      setResolution("");
                      setPendingTicket(item);
                    }}
                  >
                    {item.status === "open" ? "开始处理" : "结案"}
                  </button>
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
        <label>
          关联客户编号（可选）
          <input
            value={targetUser}
            onChange={(e) => setTargetUser(e.target.value)}
            placeholder="从客户列表复制的用户编号；留空 = 平台侧问题"
          />
        </label>
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
    </section>
  );
}
