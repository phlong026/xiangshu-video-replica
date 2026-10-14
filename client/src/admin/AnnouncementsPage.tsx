import { useCallback, useEffect, useState } from "react";
import {
  type AnnouncementItem,
  createAnnouncement,
  deleteAnnouncement,
  listAnnouncements,
  updateAnnouncement,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { ListPage } from "./ui/ListPage";
import { Pagination } from "./ui/Pagination";
import { type BadgeTone, StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * M2 公告与消息中心（ADM-09）：对客触达的唯一出口。
 *
 * 可设生效区间（北京时间）与置顶；客户进入工作台看到横幅并回传已读，
 * 已读人数 / 在役客户数即触达率。维护公告用 maintenance 色调，活动用 promo。
 * 整改 T12：补上编辑（后端早已支持 PATCH 与区间字段）、定时、状态拆分、
 * 触达率、分页与客户端样式预览。
 */
const TONE_OPTIONS = [
  { value: "info", label: "常规" },
  { value: "maintenance", label: "维护" },
  { value: "promo", label: "活动" },
];

const STATUS_DISPLAY: Record<string, { label: string; tone: BadgeTone }> = {
  scheduled: { label: "待生效", tone: "info" },
  live: { label: "生效中", tone: "good" },
  ended: { label: "已结束", tone: "neutral" },
};

const PAGE_SIZE = 20;

/** ISO 时间 → datetime-local 输入值（北京时间）。 */
function toShanghaiInput(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

/** datetime-local 输入值（按北京时间理解）→ 带时区的 ISO。 */
function fromShanghaiInput(value: string): string {
  return value ? `${value}:00+08:00` : "";
}

type Draft = {
  title: string;
  body: string;
  tone: string;
  pinned: boolean;
  startsAt: string;
  endsAt: string;
};

const EMPTY_DRAFT: Draft = {
  title: "",
  body: "",
  tone: "info",
  pinned: false,
  startsAt: "",
  endsAt: "",
};

export function AnnouncementsPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [items, setItems] = useState<AnnouncementItem[]>([]);
  const [total, setTotal] = useState(0);
  const [audience, setAudience] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // editing: null = 关闭；"new" = 发布新公告；否则为正在编辑的那一条。
  const [editing, setEditing] = useState<AnnouncementItem | "new" | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [pendingDelete, setPendingDelete] = useState<AnnouncementItem | null>(
    null,
  );
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listAnnouncements({ limit: PAGE_SIZE, offset });
      setItems(response.items);
      setTotal(response.total ?? response.items.length);
      setAudience(response.audience_total ?? 0);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取公告失败");
    } finally {
      setLoading(false);
    }
  }, [offset]);

  useEffect(() => {
    void load();
  }, [load]);

  function openCreate() {
    setDraft(EMPTY_DRAFT);
    setEditing("new");
  }

  function openEdit(item: AnnouncementItem) {
    setDraft({
      title: item.title,
      body: item.body,
      tone: item.tone,
      pinned: item.pinned,
      startsAt: toShanghaiInput(item.starts_at),
      endsAt: toShanghaiInput(item.ends_at),
    });
    setEditing(item);
  }

  async function submit(reason: string) {
    if (!editing) return;
    if (draft.startsAt && draft.endsAt && draft.endsAt <= draft.startsAt) {
      setError("失效时间必须晚于生效时间。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      if (editing === "new") {
        await createAnnouncement({
          title: draft.title.trim(),
          body: draft.body.trim(),
          tone: draft.tone,
          pinned: draft.pinned,
          startsAt: fromShanghaiInput(draft.startsAt),
          endsAt: fromShanghaiInput(draft.endsAt),
          reason,
        });
        setNotice(
          draft.startsAt
            ? "公告已发布，到生效时间后客户端可见。"
            : "公告已发布，客户端即时可见。",
        );
      } else {
        await updateAnnouncement({
          id: editing.id,
          title: draft.title.trim(),
          body: draft.body.trim(),
          tone: draft.tone,
          pinned: draft.pinned,
          startsAt: fromShanghaiInput(draft.startsAt),
          endsAt: fromShanghaiInput(draft.endsAt),
          clearEndsAt: !draft.endsAt && Boolean(editing.ends_at),
          reason,
        });
        setNotice("公告已更新。");
      }
      setEditing(null);
      setDraft(EMPTY_DRAFT);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存公告失败");
    } finally {
      setSaving(false);
    }
  }

  async function togglePin(item: AnnouncementItem, reason: string) {
    setSaving(true);
    setError("");
    try {
      await updateAnnouncement({ id: item.id, pinned: !item.pinned, reason });
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "修改公告失败");
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete(reason: string) {
    if (!pendingDelete) return;
    setSaving(true);
    setError("");
    try {
      await deleteAnnouncement({ id: pendingDelete.id, reason });
      setNotice("公告已下线。");
      setPendingDelete(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "下线公告失败");
    } finally {
      setSaving(false);
    }
  }

  const isNew = editing === "new";
  return (
    <>
      <ListPage
        ariaLabel="公告与消息"
        className="admin-announcements-page"
        error={error}
        notice={notice}
        loading={loading}
        hasItems={items.length > 0}
        loadingText="正在读取公告…"
        emptyText="还没有公告；点「发布公告」发出第一条。"
        actions={
          <>
            <header>
              <p className="admin-hint">
                面向全部客户的运营触达：维护通知、活动与价格变更。可设生效区间（北京
                时间）；客户进入工作台时展示并回传已读，置顶公告排在最前。
              </p>
            </header>
            {!readOnly && editing === null ? (
              <button type="button" onClick={openCreate}>
                发布公告
              </button>
            ) : null}
          </>
        }
        pagination={
          <Pagination
            disabled={loading}
            limit={PAGE_SIZE}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        }
      >
        <DataTable
          ariaLabel="公告清单"
          headers={
            <>
              <th>标题</th>
              <th>色调</th>
              <th>状态 / 生效区间</th>
              <th>已读（触达率）</th>
              <th>发布时间 / 人</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => {
            const status =
              STATUS_DISPLAY[item.status ?? (item.live ? "live" : "ended")];
            return (
              <tr key={item.id}>
                <td>
                  {item.pinned ? (
                    <StatusBadge tone="info">置顶</StatusBadge>
                  ) : null}
                  <strong>{item.title}</strong>
                  <p className="admin-hint">{item.body.slice(0, 80)}</p>
                </td>
                <td>{item.tone_label}</td>
                <td>
                  <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
                  <small>
                    {item.starts_at ? formatDateTime(item.starts_at) : "立即"}{" "}
                    至 {item.ends_at ? formatDateTime(item.ends_at) : "不限"}
                  </small>
                </td>
                <td>
                  <strong>{item.read_count} 人</strong>
                  {audience > 0 ? (
                    <small>
                      共 {audience} 名客户（
                      {((item.read_count / audience) * 100).toFixed(1)}%）
                    </small>
                  ) : null}
                </td>
                <td>
                  {formatDateTime(item.created_at)} · {item.created_by}
                </td>
                <td>
                  {readOnly ? null : (
                    <>
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => openEdit(item)}
                      >
                        编辑
                      </button>{" "}
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() =>
                          void togglePin(
                            item,
                            item.pinned ? "取消置顶" : "置顶公告",
                          )
                        }
                      >
                        {item.pinned ? "取消置顶" : "置顶"}
                      </button>{" "}
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => setPendingDelete(item)}
                      >
                        下线
                      </button>
                    </>
                  )}
                </td>
              </tr>
            );
          })}
        </DataTable>
      </ListPage>
      {editing !== null ? (
        <ConfirmDialog
          open
          title={isNew ? "发布公告" : "编辑公告"}
          confirmLabel={isNew ? "确认发布" : "保存修改"}
          description={
            isNew
              ? "未设生效时间则立即对全部客户可见；请核对标题、正文、色调与预览。"
              : "修改立即同步到客户端；已读回执保留。"
          }
          busy={saving}
          error={error}
          level="reason"
          onClose={() => !saving && setEditing(null)}
          onConfirm={(reason) => void submit(reason)}
        >
          <label>
            标题（≤120 字）
            <input
              value={draft.title}
              onChange={(event) =>
                setDraft({ ...draft, title: event.target.value })
              }
              placeholder="如：今晚 23:00 例行维护"
              maxLength={120}
            />
          </label>
          <label>
            正文（≤4000 字）
            <textarea
              value={draft.body}
              onChange={(event) =>
                setDraft({ ...draft, body: event.target.value })
              }
              rows={5}
              maxLength={4000}
              placeholder="说明影响范围与恢复时间"
            />
          </label>
          <label>
            色调
            <select
              value={draft.tone}
              onChange={(event) =>
                setDraft({ ...draft, tone: event.target.value })
              }
            >
              {TONE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            生效时间（北京时间，留空 = 立即）
            <input
              type="datetime-local"
              value={draft.startsAt}
              onChange={(event) =>
                setDraft({ ...draft, startsAt: event.target.value })
              }
            />
          </label>
          <label>
            失效时间（北京时间，留空 = 不限）
            <input
              type="datetime-local"
              value={draft.endsAt}
              min={draft.startsAt || undefined}
              onChange={(event) =>
                setDraft({ ...draft, endsAt: event.target.value })
              }
            />
          </label>
          <label className="admin-dialog__ack">
            <input
              type="checkbox"
              checked={draft.pinned}
              onChange={(event) =>
                setDraft({ ...draft, pinned: event.target.checked })
              }
            />
            置顶（排在客户公告列表最前）
          </label>
          {/* 客户端横幅预览：与工作台同样只露出正文前 60 字。 */}
          <section
            aria-label="客户端预览"
            className={`admin-announcement-preview admin-announcement-preview--${draft.tone}`}
          >
            <strong>{draft.title.trim() || "（标题）"}</strong>
            <p>
              {draft.body.trim().length > 60
                ? `${draft.body.trim().slice(0, 60)}…（点击展开）`
                : draft.body.trim() || "（正文）"}
            </p>
          </section>
        </ConfirmDialog>
      ) : null}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="下线公告"
        confirmLabel="确认下线"
        description={
          pendingDelete
            ? `「${pendingDelete.title}」将立即对客户不可见；已读回执保留。`
            : ""
        }
        busy={saving}
        error={error}
        level="reason"
        onClose={() => !saving && setPendingDelete(null)}
        onConfirm={(reason) => void confirmDelete(reason)}
      />
    </>
  );
}
