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
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime } from "./ui/vocabulary";

/**
 * M2 公告与消息中心（ADM-09）：对客触达的唯一出口。
 *
 * 发布即生效（可选定时区间/置顶）；客户进入工作台看到横幅并回传已读，
 * read_count 即触达人数。维护公告用 maintenance 色调，活动用 promo。
 */
const TONE_OPTIONS = [
  { value: "info", label: "常规" },
  { value: "maintenance", label: "维护" },
  { value: "promo", label: "活动" },
];

export function AnnouncementsPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [items, setItems] = useState<AnnouncementItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [tone, setTone] = useState("info");
  const [pinned, setPinned] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<AnnouncementItem | null>(
    null,
  );
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listAnnouncements();
      setItems(response.items);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取公告失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function submit(reason: string) {
    setSaving(true);
    setError("");
    try {
      await createAnnouncement({
        title: title.trim(),
        body: body.trim(),
        tone,
        pinned,
        reason,
      });
      setNotice("公告已发布，客户端即时可见。");
      setTitle("");
      setBody("");
      setTone("info");
      setPinned(false);
      setCreating(false);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "发布公告失败");
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

  return (
    <section
      className="admin-panel admin-announcements-page"
      aria-label="公告与消息"
    >
      <header>
        <h2>公告与消息</h2>
        <p className="admin-hint">
          面向全部客户的运营触达：维护通知、活动与价格变更。客户进入工作台时
          展示并回传已读；置顶公告排在最前。
        </p>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!readOnly && !creating ? (
        <button type="button" onClick={() => setCreating(true)}>
          发布公告
        </button>
      ) : null}
      {creating ? (
        <ConfirmDialog
          open
          title="发布公告"
          confirmLabel="确认发布"
          description="发布后立即对全部客户可见；请核对标题、正文与色调。"
          busy={saving}
          error={error}
          level="reason"
          onClose={() => !saving && setCreating(false)}
          onConfirm={(reason) => void submit(reason)}
        >
          <label>
            标题（≤120 字）
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="如：今晚 23:00 例行维护"
              maxLength={120}
            />
          </label>
          <label>
            正文（≤4000 字）
            <textarea
              value={body}
              onChange={(event) => setBody(event.target.value)}
              rows={5}
              maxLength={4000}
              placeholder="说明影响范围与恢复时间"
            />
          </label>
          <label>
            色调
            <select
              value={tone}
              onChange={(event) => setTone(event.target.value)}
            >
              {TONE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="admin-dialog__ack">
            <input
              type="checkbox"
              checked={pinned}
              onChange={(event) => setPinned(event.target.checked)}
            />
            置顶（排在客户公告列表最前）
          </label>
        </ConfirmDialog>
      ) : null}
      {loading ? (
        <p role="status">正在读取公告…</p>
      ) : items.length === 0 ? (
        <PageBanner tone="notice">
          还没有公告；点「发布公告」发出第一条。
        </PageBanner>
      ) : (
        <DataTable
          ariaLabel="公告清单"
          headers={
            <>
              <th>标题</th>
              <th>色调</th>
              <th>状态</th>
              <th>已读</th>
              <th>发布时间 / 人</th>
              <th>操作</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                {item.pinned ? "📌 " : ""}
                <strong>{item.title}</strong>
                <p className="admin-hint">{item.body.slice(0, 80)}</p>
              </td>
              <td>{item.tone_label}</td>
              <td>
                <StatusBadge tone={item.live ? "good" : "neutral"}>
                  {item.live ? "生效中" : "未生效/已过期"}
                </StatusBadge>
              </td>
              <td>{item.read_count} 人</td>
              <td>
                {formatDateTime(item.created_at)} · {item.created_by}
              </td>
              <td>
                {readOnly ? null : (
                  <>
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
          ))}
        </DataTable>
      )}
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
    </section>
  );
}
