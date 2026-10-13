import { useEffect, useState } from "react";
import {
  type CustomerCredential,
  customerMarkAnnouncementRead,
  customerListAnnouncements,
} from "../api";

/**
 * M2 公告与消息中心（ADM-09）——客户端展示位。
 *
 * T34：此前这里用裸 fetch 直发 Bearer——浏览器模式下会话 token 是
 * `web-session:` 句柄，缺少 `X-Customer-Web` 传输头必然 401，失败又被静默
 * 吞掉，公告永远为空、已读回执从不落库。现在复用共享客户请求器（自动带
 * 浏览器传输头与 Request-Id），已读回执成功才从界面移除，失败保留可重试。
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

export function CustomerAnnouncementCenter({
  credential,
}: {
  credential: CustomerCredential;
}) {
  // 拆出原始值做 effect 依赖（T34/P2）：父组件每次渲染新建 credential
  // 对象，按引用比较会让公告列表被反复重拉。
  const { kind, token } = credential;
  const [items, setItems] = useState<Announcement[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<string | null>(null);
  const [readFailures, setReadFailures] = useState<Set<string>>(new Set());

  useEffect(() => {
    let active = true;
    setLoaded(false);
    setLoadError("");
    customerListAnnouncements({ kind, token })
      .then((list) => {
        if (active) {
          setItems(list);
          setLoaded(true);
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setLoaded(true);
          setLoadError(cause instanceof Error ? cause.message : "公告读取失败");
        }
      });
    return () => {
      active = false;
    };
  }, [kind, token]);

  async function markRead(id: string) {
    try {
      // 回执成功才从界面移除（T34）：服务端 read_count 只计成功回执，
      // 失败时公告保留原位，用户可以再点一次。
      await customerMarkAnnouncementRead({ kind, token }, id);
      setReadFailures((previous) => {
        const next = new Set(previous);
        next.delete(id);
        return next;
      });
      setDismissed((previous) => new Set(previous).add(id));
    } catch {
      setReadFailures((previous) => new Set(previous).add(id));
    }
  }

  const visible = items.filter((item) => !dismissed.has(item.id));
  if (visible.length === 0) {
    if (loadError) {
      return (
        <section className="customer-announcements" aria-label="平台公告">
          <div
            className="customer-announcement customer-announcement--maintenance"
            role="alert"
          >
            <div className="customer-announcement__head">
              <strong>公告读取失败</strong>
              <button type="button" onClick={() => window.location.reload()}>
                刷新重试
              </button>
            </div>
            <button
              type="button"
              className="customer-announcement__body"
              onClick={() => window.location.reload()}
            >
              {loadError}（点击刷新重试）
            </button>
          </div>
        </section>
      );
    }
    return null;
  }
  return (
    <section className="customer-announcements" aria-label="平台公告">
      {loaded && readFailures.size > 0 ? (
        <p role="alert" className="admin-hint">
          有 {readFailures.size} 条公告的「已读」回执未送达，公告仍会保留。
        </p>
      ) : null}
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
              onClick={() => void markRead(item.id)}
            >
              知道了
            </button>
          </div>
          <button
            type="button"
            className="customer-announcement__body"
            aria-expanded={expanded === item.id}
            onClick={() => setExpanded(expanded === item.id ? null : item.id)}
          >
            {expanded === item.id
              ? item.body
              : item.body.length > 60
                ? `${item.body.slice(0, 60)}…（点击展开）`
                : item.body}
          </button>
        </div>
      ))}
    </section>
  );
}
