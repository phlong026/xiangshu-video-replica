import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  getStudioMessagePreferences,
  getStudioMessages,
  getStudioMessagesSummary,
  markStudioMessagesRead,
  type StudioBillingStyle,
  type StudioMessage,
  type StudioMessageCategory,
  type StudioMessagePreferences,
  type StudioMessagesPage,
  type StudioMessagesSummary,
  updateStudioMessagePreferences,
} from "../api";
import { useStudio } from "./context";
import { Button, Icon } from "./ui";
import "./message-center.css";

type CategoryTab = "all" | StudioMessageCategory;

const CATEGORIES: Array<{
  value: CategoryTab;
  label: string;
  settingLabel: string;
  description: string;
}> = [
  { value: "all", label: "全部", settingLabel: "", description: "" },
  {
    value: "task",
    label: "任务动态",
    settingLabel: "任务动态",
    description: "视频生成、数字人口播、复刻、拆解、采集和发布的结果。",
  },
  {
    value: "billing",
    label: "账单通知",
    settingLabel: "账单通知",
    description: "充值到账、扣费结算、退款与平台调整。",
  },
  {
    value: "announcement",
    label: "平台公告",
    settingLabel: "平台公告",
    description: "维护通知、活动与产品更新。",
  },
  {
    value: "security",
    label: "账号安全",
    settingLabel: "账号与安全",
    description: "新设备登录、密码修改与全设备退出。",
  },
];

const BILLING_STYLES: Array<{ value: StudioBillingStyle; label: string }> = [
  { value: "per_item", label: "每笔结算" },
  { value: "daily", label: "每日汇总" },
  { value: "off", label: "关闭" },
];

function beijingDay(value: string | number): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

function dayBucket(occurredAt: string): "今天" | "昨天" | "更早" {
  const day = beijingDay(occurredAt);
  const now = Date.now();
  if (day === beijingDay(now)) return "今天";
  if (day === beijingDay(now - 24 * 60 * 60 * 1000)) return "昨天";
  return "更早";
}

function formatTime(occurredAt: string, bucket: string): string {
  const date = new Date(occurredAt);
  if (Number.isNaN(date.getTime())) return occurredAt;
  if (bucket === "更早") {
    return date.toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }
  return date.toLocaleTimeString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function iconFor(item: StudioMessage): string {
  if (item.category === "announcement") return "megaphone";
  if (item.category === "billing") return "wallet";
  if (item.category === "security") return "shield";
  if (item.tone === "danger") return "warning";
  if (item.kind === "oral") return "person";
  if (item.kind === "analysis") return "folder";
  if (item.kind === "collection") return "download";
  if (item.kind === "publish") return "upload";
  return "video";
}

function signedCredits(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toLocaleString("zh-CN")} 积分`;
}

function actionHint(item: StudioMessage): string {
  if (!item.action) return "";
  if (item.action.kind === "task") {
    return item.tone === "danger" || item.tone === "warning"
      ? "去处理"
      : "查看任务";
  }
  if (item.action.kind === "wallet") return item.action.label;
  return item.action.label;
}

export function MessageCenter() {
  const { review, navigate, openLive, user } = useStudio();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<"list" | "settings">("list");
  const [category, setCategory] = useState<CategoryTab>("all");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [page, setPage] = useState<StudioMessagesPage | null>(null);
  const [items, setItems] = useState<StudioMessage[]>([]);
  const [summary, setSummary] = useState<StudioMessagesSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [prefs, setPrefs] = useState<StudioMessagePreferences | null>(null);
  const [prefsError, setPrefsError] = useState("");
  const [savingPrefs, setSavingPrefs] = useState(false);
  const [thresholdDraft, setThresholdDraft] = useState("50");
  const [revision, setRevision] = useState(0);
  const listGeneration = useRef(0);

  const canMark = user.role !== "auditor";

  const loadSummary = useCallback(async () => {
    if (review) return;
    try {
      const next = await getStudioMessagesSummary();
      setSummary(next);
    } catch {
      // 未读数失败不打断当前界面；错误由列表请求统一提示。
    }
  }, [review]);

  // 徽标轮询：页面隐藏时跳过本轮（沿用旧通知的性能约定）。
  useEffect(() => {
    void revision;
    void user.id;
    if (review) return;
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;
    async function tick() {
      if (!document.hidden) await loadSummary();
      if (!cancelled) timer = setTimeout(() => void tick(), 60_000);
    }
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [loadSummary, review, revision, user.id]);

  const loadPage = useCallback(
    async (offset: number, append: boolean) => {
      if (review) return;
      const generation = ++listGeneration.current;
      if (append) setLoadingMore(true);
      else {
        setLoading(true);
        setError("");
      }
      try {
        const next = await getStudioMessages({
          category,
          unreadOnly,
          limit: 20,
          offset,
        });
        if (generation !== listGeneration.current) return;
        setPage(next);
        setItems((current) =>
          append ? [...current, ...next.items] : next.items,
        );
        setError("");
      } catch (cause) {
        if (generation !== listGeneration.current) return;
        setError(cause instanceof Error ? cause.message : "读取消息失败");
      } finally {
        if (generation === listGeneration.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [category, review, unreadOnly],
  );

  useEffect(() => {
    void revision;
    if (!open || view !== "list" || review) return;
    void loadPage(0, false);
  }, [loadPage, open, review, revision, view]);

  const loadPrefs = useCallback(async () => {
    if (review) return;
    try {
      const next = await getStudioMessagePreferences();
      setPrefs(next);
      setThresholdDraft(String(next.balance_threshold));
      setPrefsError("");
    } catch (cause) {
      setPrefsError(
        cause instanceof Error ? cause.message : "读取消息设置失败",
      );
    }
  }, [review]);

  useEffect(() => {
    if (view !== "settings") return;
    void loadPrefs();
  }, [loadPrefs, view]);

  async function savePrefs(next: StudioMessagePreferences) {
    if (review || savingPrefs) return;
    setPrefs(next);
    setSavingPrefs(true);
    setPrefsError("");
    try {
      const saved = await updateStudioMessagePreferences(next);
      setPrefs(saved);
      setSummary((current) =>
        current
          ? {
              ...current,
              enabled: saved.enabled,
              muted: saved.muted,
              billing_style: saved.billing_style,
            }
          : current,
      );
      setRevision((value) => value + 1);
    } catch (cause) {
      setPrefsError(
        cause instanceof Error ? cause.message : "保存消息设置失败",
      );
      await loadPrefs();
    } finally {
      setSavingPrefs(false);
    }
  }

  function toggleMuted(name: StudioMessageCategory, mutedNow: boolean) {
    if (!prefs) return;
    const nextMuted = mutedNow
      ? prefs.muted.filter((value) => value !== name)
      : [...prefs.muted, name];
    void savePrefs({ ...prefs, muted: nextMuted });
  }

  async function markRead(keys: string[]) {
    // 已读会收缩未读分页；偏好保存也会刷新列表，回执需与两种请求串行。
    if (
      review ||
      !canMark ||
      keys.length === 0 ||
      busy ||
      savingPrefs ||
      loading ||
      loadingMore
    )
      return;
    setBusy(true);
    try {
      await markStudioMessagesRead({ keys });
      setItems((current) =>
        current.map((item) =>
          keys.includes(item.id) ? { ...item, unread: false } : item,
        ),
      );
      await loadSummary();
    } catch {
      // 标记失败保留未读态；下一次点击或「全部已读」可重试。
    } finally {
      setBusy(false);
    }
  }

  async function markAll() {
    if (review || !canMark || busy || savingPrefs || loading || loadingMore)
      return;
    const scope =
      category === "all"
        ? (summary?.total_unread ?? 0)
        : (summary?.by_category[category] ?? 0);
    if (scope === 0) return;
    setBusy(true);
    try {
      await markStudioMessagesRead({ category });
      setItems((current) =>
        current.map((item) => ({ ...item, unread: false })),
      );
      setRevision((value) => value + 1);
      await loadSummary();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "标记已读失败");
    } finally {
      setBusy(false);
    }
  }

  async function openItem(item: StudioMessage) {
    if (item.unread) void markRead([item.id]);
    if (item.category === "announcement") {
      setExpanded((current) => (current === item.id ? null : item.id));
      return;
    }
    if (!item.action) return;
    setOpen(false);
    if (item.action.kind === "task") {
      navigate("task-detail", {
        selectedTaskId:
          item.action.task_kind === "oral_task"
            ? `oral-${item.action.task_id ?? ""}`
            : (item.action.task_id ?? ""),
        selectedTaskKind:
          item.action.task_kind === "oral_task"
            ? "oral_task"
            : "generation_batch",
        selectedTaskBackendId: item.action.task_id ?? "",
        returnTo: "tasks",
      });
      return;
    }
    if (item.action.kind === "wallet") {
      openLive("wallet");
      return;
    }
    navigate("profile");
  }

  const groups = useMemo(() => {
    const order: Array<"今天" | "昨天" | "更早"> = ["今天", "昨天", "更早"];
    return order
      .map((bucket) => ({
        bucket,
        rows: items.filter((item) => dayBucket(item.occurred_at) === bucket),
      }))
      .filter((group) => group.rows.length > 0);
  }, [items]);

  const unreadBadge = review ? 0 : (summary?.total_unread ?? 0);
  const currentUnread =
    category === "all"
      ? (summary?.total_unread ?? 0)
      : (summary?.by_category[category] ?? 0);

  function close() {
    setOpen(false);
    setView("list");
  }

  return (
    <>
      <button
        type="button"
        className="studio-notifications"
        aria-label="打开消息中心"
        disabled={busy}
        onClick={() => setOpen(true)}
      >
        <Icon name="bell" size={28} />
        {!!unreadBadge && (
          <small>{unreadBadge > 99 ? "99+" : unreadBadge}</small>
        )}
      </button>
      {open && (
        <MessageDrawer onClose={close}>
          <button
            type="button"
            className="mc-drawer__scrim"
            aria-label="关闭消息中心"
            onClick={close}
          />
          <section className="mc-panel" aria-label="消息中心" tabIndex={-1}>
            <header className="mc-head">
              <div className="mc-head__row">
                <h2>{view === "settings" ? "消息设置" : "消息中心"}</h2>
                <div className="mc-head__actions">
                  {view === "settings" ? (
                    <button
                      type="button"
                      className="mc-text-btn"
                      onClick={() => setView("list")}
                    >
                      返回
                    </button>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="mc-text-btn"
                        disabled={
                          busy ||
                          savingPrefs ||
                          loading ||
                          loadingMore ||
                          currentUnread === 0 ||
                          !canMark
                        }
                        onClick={() => void markAll()}
                      >
                        全部已读
                      </button>
                      <button
                        type="button"
                        className="mc-icon-btn"
                        aria-label="消息设置"
                        disabled={busy}
                        onClick={() => setView("settings")}
                      >
                        <Icon name="settings" size={18} />
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    className="mc-icon-btn"
                    aria-label="关闭消息中心"
                    onClick={close}
                  >
                    <Icon name="close" size={18} />
                  </button>
                </div>
              </div>
              {view === "list" && (
                <nav className="mc-tabs" aria-label="消息分类">
                  {CATEGORIES.map((tab) => {
                    const count =
                      tab.value === "all"
                        ? (summary?.total_unread ?? 0)
                        : (summary?.by_category[tab.value] ?? 0);
                    return (
                      <button
                        key={tab.value}
                        type="button"
                        disabled={busy}
                        className={`mc-tab ${category === tab.value ? "is-active" : ""}`}
                        aria-current={
                          category === tab.value ? "true" : undefined
                        }
                        onClick={() => setCategory(tab.value)}
                      >
                        {tab.label}
                        {count > 0 && (
                          <span className="mc-tab__count">{count}</span>
                        )}
                      </button>
                    );
                  })}
                </nav>
              )}
            </header>

            {view === "list" ? (
              <>
                <div className="mc-toolbar">
                  <label className="mc-check">
                    <input
                      type="checkbox"
                      checked={unreadOnly}
                      disabled={busy}
                      onChange={(event) => setUnreadOnly(event.target.checked)}
                    />
                    <span className="mc-switch" aria-hidden="true" />
                    仅看未读
                  </label>
                  <span className="mc-toolbar__meta">
                    {page ? `${page.total} 条${unreadOnly ? "未读" : ""}` : ""}
                  </span>
                </div>
                {error && (
                  <div className="mc-error" role="alert">
                    <span>{error}</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void loadPage(0, false)}
                    >
                      重试
                    </button>
                  </div>
                )}
                <div className="mc-list">
                  {review ? (
                    <p className="mc-hint">审核模式不读取真实消息。</p>
                  ) : loading && items.length === 0 ? (
                    <p className="mc-hint" role="status">
                      正在读取消息…
                    </p>
                  ) : items.length === 0 ? (
                    <div className="mc-empty">
                      <span className="mc-empty__icon">
                        <Icon name="bell" size={26} />
                      </span>
                      <h3>
                        {summary?.enabled === false
                          ? "消息中心已关闭"
                          : unreadOnly
                            ? "暂无未读消息"
                            : "暂无消息"}
                      </h3>
                      <p>
                        {summary?.enabled === false
                          ? "可在消息设置中重新开启。"
                          : unreadOnly
                            ? "所有消息都已读完了。"
                            : "新任务完成、账务变动或平台公告会在这里提醒你。"}
                      </p>
                      {summary?.enabled === false ? (
                        <Button onClick={() => setView("settings")}>
                          打开消息设置
                        </Button>
                      ) : unreadOnly ? (
                        <Button onClick={() => setUnreadOnly(false)}>
                          查看全部消息
                        </Button>
                      ) : null}
                    </div>
                  ) : (
                    <>
                      {groups.map((group) => (
                        <section key={group.bucket} aria-label={group.bucket}>
                          <h3 className="mc-group">{group.bucket}</h3>
                          {group.rows.map((item) => {
                            const isExpanded = expanded === item.id;
                            return (
                              <button
                                type="button"
                                key={item.id}
                                disabled={
                                  item.unread &&
                                  (busy ||
                                    savingPrefs ||
                                    loading ||
                                    loadingMore)
                                }
                                className={`mc-item ${item.unread ? "mc-item--unread" : "mc-item--read"} ${
                                  item.tone === "danger" ||
                                  item.tone === "warning"
                                    ? "mc-item--attention"
                                    : ""
                                }`}
                                aria-expanded={
                                  item.category === "announcement"
                                    ? isExpanded
                                    : undefined
                                }
                                onClick={() => void openItem(item)}
                              >
                                <span
                                  className={`mc-item__icon mc-item__icon--${item.category}`}
                                >
                                  <Icon name={iconFor(item)} size={18} />
                                </span>
                                <span className="mc-item__body">
                                  <span className="mc-item__head">
                                    {item.unread && (
                                      <span
                                        className="mc-item__dot"
                                        aria-hidden="true"
                                      />
                                    )}
                                    <strong className="mc-item__title">
                                      {item.title}
                                    </strong>
                                    {item.category === "billing" &&
                                    item.amount_credits !== null &&
                                    item.amount_credits !== 0 ? (
                                      <span
                                        className={`mc-chip ${
                                          item.amount_credits >= 0
                                            ? "mc-chip--success"
                                            : "mc-chip--amount-out"
                                        }`}
                                      >
                                        {signedCredits(item.amount_credits)}
                                      </span>
                                    ) : (
                                      <span
                                        className={`mc-chip mc-chip--${item.tone}`}
                                      >
                                        {item.status_label}
                                      </span>
                                    )}
                                    <time className="mc-item__time">
                                      {formatTime(
                                        item.occurred_at,
                                        dayBucket(item.occurred_at),
                                      )}
                                    </time>
                                  </span>
                                  <span className="mc-item__summary">
                                    {item.summary}
                                  </span>
                                  {item.category === "announcement" &&
                                    isExpanded && (
                                      <span className="mc-item__detail">
                                        {item.detail}
                                      </span>
                                    )}
                                  {item.action &&
                                    item.category !== "announcement" &&
                                    canMark && (
                                      <span className="mc-item__link">
                                        {actionHint(item)} →
                                      </span>
                                    )}
                                  {item.category === "announcement" && (
                                    <span className="mc-item__link">
                                      {isExpanded ? "收起" : "展开全文"}
                                    </span>
                                  )}
                                </span>
                                <span className="mc-item__chev">›</span>
                              </button>
                            );
                          })}
                        </section>
                      ))}
                      <div className="mc-list__foot">
                        {page?.has_more ? (
                          <button
                            type="button"
                            disabled={busy || loading || loadingMore}
                            onClick={() =>
                              void loadPage(
                                unreadOnly
                                  ? items.filter((item) => item.unread).length
                                  : items.length,
                                true,
                              )
                            }
                          >
                            {loadingMore ? "正在加载…" : "加载更多"}
                          </button>
                        ) : (
                          <span>已经到底了</span>
                        )}
                      </div>
                    </>
                  )}
                </div>
              </>
            ) : (
              <div className="mc-settings">
                {prefsError && (
                  <p className="mc-error" role="alert">
                    {prefsError}
                  </p>
                )}
                {!prefs ? (
                  <p className="mc-hint" role="status">
                    正在读取消息设置…
                  </p>
                ) : (
                  <>
                    <section className="mc-settings__sec">
                      <label className="mc-setting">
                        <span>
                          <b>消息中心</b>
                          <small>
                            关闭后不再产生任何消息，顶部铃铛不再显示红点。
                          </small>
                        </span>
                        <input
                          type="checkbox"
                          className="mc-check__input"
                          checked={prefs.enabled}
                          disabled={savingPrefs}
                          onChange={(event) =>
                            void savePrefs({
                              ...prefs,
                              enabled: event.target.checked,
                            })
                          }
                        />
                        <span className="mc-switch" aria-hidden="true" />
                      </label>
                    </section>

                    <section className="mc-settings__sec">
                      <h4>接收哪些消息</h4>
                      {CATEGORIES.filter((tab) => tab.value !== "all").map(
                        (tab) => {
                          const name = tab.value as StudioMessageCategory;
                          const mutedNow = prefs.muted.includes(name);
                          return (
                            <label className="mc-setting" key={name}>
                              <span>
                                <b>{tab.settingLabel}</b>
                                <small>{tab.description}</small>
                              </span>
                              <input
                                type="checkbox"
                                className="mc-check__input"
                                checked={!mutedNow}
                                disabled={savingPrefs}
                                aria-label={`接收${tab.settingLabel}`}
                                onChange={() => toggleMuted(name, mutedNow)}
                              />
                              <span className="mc-switch" aria-hidden="true" />
                            </label>
                          );
                        },
                      )}
                    </section>

                    <section className="mc-settings__sec">
                      <h4>扣费消息粒度</h4>
                      <div className="mc-segmented">
                        {BILLING_STYLES.map((option) => (
                          <button
                            key={option.value}
                            type="button"
                            disabled={savingPrefs}
                            className={
                              prefs.billing_style === option.value
                                ? "is-active"
                                : ""
                            }
                            aria-pressed={prefs.billing_style === option.value}
                            onClick={() =>
                              void savePrefs({
                                ...prefs,
                                billing_style: option.value,
                              })
                            }
                          >
                            {option.label}
                          </button>
                        ))}
                      </div>
                      <p className="mc-settings__note">
                        「每笔结算」会为每次任务的最终扣费生成一条消息（已含多退少补）；
                        「每日汇总」每天只推一条合计。
                      </p>
                    </section>

                    <section className="mc-settings__sec">
                      <h4>余额提醒</h4>
                      <label className="mc-setting">
                        <span>
                          <b>余额不足时提醒</b>
                          <small>
                            结算后余额低于阈值时发一条提醒，避免下次生成失败。
                          </small>
                        </span>
                        <input
                          type="checkbox"
                          className="mc-check__input"
                          checked={prefs.balance_threshold > 0}
                          disabled={savingPrefs}
                          aria-label="余额不足时提醒"
                          onChange={(event) =>
                            void savePrefs({
                              ...prefs,
                              balance_threshold: event.target.checked ? 50 : 0,
                            })
                          }
                        />
                        <span className="mc-switch" aria-hidden="true" />
                      </label>
                      {prefs.balance_threshold > 0 && (
                        <label className="mc-threshold">
                          低于
                          <input
                            type="number"
                            min={1}
                            max={1_000_000}
                            value={thresholdDraft}
                            disabled={savingPrefs}
                            onChange={(event) =>
                              setThresholdDraft(event.target.value)
                            }
                            onBlur={() => {
                              const parsed = Number(thresholdDraft);
                              if (
                                Number.isFinite(parsed) &&
                                parsed >= 1 &&
                                parsed <= 1_000_000 &&
                                parsed !== prefs.balance_threshold
                              ) {
                                void savePrefs({
                                  ...prefs,
                                  balance_threshold: Math.round(parsed),
                                });
                              } else {
                                setThresholdDraft(
                                  String(prefs.balance_threshold),
                                );
                              }
                            }}
                          />
                          积分
                        </label>
                      )}
                    </section>

                    <p className="mc-settings__note">
                      设置只对当前账号生效。子账号共用主账号钱包，只接收任务与公告，
                      不接收账务消息。
                    </p>
                  </>
                )}
              </div>
            )}
          </section>
        </MessageDrawer>
      )}
    </>
  );
}

function MessageDrawer({
  children,
  onClose,
}: {
  children: ReactNode;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    // 把焦点放到面板上，避免落到遮罩按钮（第一个可聚焦元素）。
    dialog.querySelector<HTMLElement>(".mc-panel")?.focus();
    return () => {
      if (typeof dialog.close === "function") dialog.close();
    };
  }, []);
  return (
    <dialog
      ref={dialogRef}
      className="mc-drawer"
      aria-label="消息中心"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      {children}
    </dialog>
  );
}
