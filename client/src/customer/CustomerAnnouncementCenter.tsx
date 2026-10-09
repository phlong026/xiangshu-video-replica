import { useEffect, useState } from "react";
import { apiBaseUrl } from "../apiBase";

/**
 * M2 公告与消息中心（ADM-09）——客户端展示位。
 *
 * 独立小模块而不进 api.ts：公告只此一处消费，鉴权直接用壳层传入的
 * 客户会话 token（Bearer），与共享请求器同一鉴权语义。
 */

type Announcement = {
  id: string;
  title: string;
  body: string;
  tone: string;
  pinned: boolean;
  read: boolean;
};

const TONE_CLASS: Record<string, string> = {
  info: "customer-announcement--info",
  maintenance: "customer-announcement--maintenance",
  promo: "customer-announcement--promo",
};

export function CustomerAnnouncementCenter({ token }: { token: string }) {
  const [items, setItems] = useState<Announcement[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void fetch(`${apiBaseUrl()}/api/announcements`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) return [];
        const body: unknown = await response.json();
        if (
          active &&
          typeof body === "object" &&
          body !== null &&
          Array.isArray((body as { items?: unknown }).items)
        ) {
          return (body as { items: Announcement[] }).items;
        }
        return [];
      })
      .then((list) => {
        if (active) setItems(list);
      })
      .catch(() => {
        /* 公告拉取失败不打扰工作台 */
      });
    return () => {
      active = false;
    };
  }, [token]);

  function markRead(id: string) {
    void fetch(`${apiBaseUrl()}/api/announcements/${id}/read`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => {
      /* 已读回执失败不影响展示 */
    });
    setDismissed((previous) => new Set(previous).add(id));
  }

  const visible = items.filter((item) => !dismissed.has(item.id));
  if (visible.length === 0) return null;
  return (
    <div className="customer-announcements" aria-label="平台公告">
      {visible.slice(0, 3).map((item) => (
        <div
          key={item.id}
          className={`customer-announcement ${TONE_CLASS[item.tone] ?? ""}`}
          role="status"
        >
          <div className="customer-announcement__head">
            {item.pinned ? <strong>置顶</strong> : null}
            <strong>{item.title}</strong>
            <button
              type="button"
              aria-label={`关闭公告 ${item.title}`}
              onClick={() => markRead(item.id)}
            >
              知道了
            </button>
          </div>
          <p
            role="button"
            tabIndex={0}
            onClick={() => setExpanded(expanded === item.id ? null : item.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter")
                setExpanded(expanded === item.id ? null : item.id);
            }}
          >
            {expanded === item.id
              ? item.body
              : item.body.length > 60
                ? `${item.body.slice(0, 60)}…（点击展开）`
                : item.body}
          </p>
        </div>
      ))}
    </div>
  );
}
