import { useEffect, useState } from "react";
import {
  adminActivationErrorMessage,
  createIdempotencyKey,
  getSoftwareLicenses,
  issueSoftwareLicense,
  renewSoftwareLicense,
  revealSoftwareLicense,
  type SoftwareLicense,
  updateSoftwareActivationPolicy,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime } from "./ui/vocabulary";
import "./admin-activation.css";

const PAGE_SIZE = 25;

type Action =
  | { kind: "issue" }
  | { kind: "policy"; enabled: boolean }
  | { kind: "renew"; license: SoftwareLicense };

const actionLabels = {
  issue: "生成软件激活码",
  policy: "修改自动延期设置",
  renew: "线下续费一年",
};

function initialDeadline() {
  const beijingTimeInSevenDays = new Date(
    Date.now() + (7 * 24 + 8) * 3_600_000,
  );
  return beijingTimeInSevenDays.toISOString().slice(0, 16);
}

export function SoftwareLicensesPage({ readOnly }: { readOnly: boolean }) {
  const [page, setPage] =
    useState<Awaited<ReturnType<typeof getSoftwareLicenses>>>();
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [action, setAction] = useState<Action | null>(null);
  const [actionKey, setActionKey] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [years, setYears] = useState(1);
  const [deadline, setDeadline] = useState(initialDeadline);
  const [revealedCode, setRevealedCode] = useState("");

  useEffect(() => {
    void revision;
    let active = true;
    setLoading(true);
    setError("");
    void getSoftwareLicenses(search, PAGE_SIZE, offset)
      .then((result) => {
        if (active) setPage(result);
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(adminActivationErrorMessage(cause, "读取软件授权失败"));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [search, offset, revision]);

  function openAction(next: Action) {
    setAction(next);
    setActionKey(createIdempotencyKey());
    setActionError("");
    setNotice("");
    setRevealedCode("");
    if (next.kind === "issue") {
      setYears(1);
      setDeadline(initialDeadline());
    }
  }

  async function submitAction(reason: string) {
    if (!action || busy || readOnly) return;
    setBusy(true);
    setActionError("");
    try {
      switch (action.kind) {
        case "issue": {
          await issueSoftwareLicense(
            {
              authorization_years: years,
              activation_expires_at: new Date(
                `${deadline}:00+08:00`,
              ).toISOString(),
            },
            actionKey,
          );
          setSearchDraft("");
          setSearch("");
          setOffset(0);
          setNotice("激活码已生成，可从列表复制后交付。");
          break;
        }
        case "policy": {
          await updateSoftwareActivationPolicy(
            action.enabled,
            reason,
            actionKey,
          );
          setNotice(action.enabled ? "自动延期已开启。" : "自动延期已关闭。");
          break;
        }
        case "renew": {
          const result = await renewSoftwareLicense(
            action.license.id,
            reason,
            actionKey,
          );
          setNotice(
            `已续费一年，授权至 ${formatDateTime(result.authorized_until)}。`,
          );
          break;
        }
      }
      setAction(null);
      setRevision((value) => value + 1);
    } catch (cause) {
      setActionError(adminActivationErrorMessage(cause, "软件授权操作失败"));
    } finally {
      setBusy(false);
    }
  }

  async function copyLicense(id: string) {
    if (busy || readOnly) return;
    setBusy(true);
    setError("");
    setNotice("");
    setRevealedCode("");
    try {
      const result = await revealSoftwareLicense(id, createIdempotencyKey());
      try {
        await navigator.clipboard.writeText(result.activation_code);
        setNotice("激活码已复制。");
      } catch {
        setRevealedCode(result.activation_code);
      }
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取激活码失败"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="activation-device-page" aria-label="软件授权">
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {revealedCode ? (
        <PageBanner tone="notice">
          剪贴板不可用，请手动复制 <code>{revealedCode}</code>{" "}
          <button type="button" onClick={() => setRevealedCode("")}>
            隐藏
          </button>
        </PageBanner>
      ) : null}
      <section className="activation-code-list-card">
        <header className="activation-code-list-header">
          <h2>软件激活码</h2>
          {!readOnly ? (
            <button
              type="button"
              className="admin-primary-btn"
              disabled={busy}
              onClick={() => openAction({ kind: "issue" })}
            >
              生成激活码
            </button>
          ) : null}
        </header>
        <div className="activation-code-list-header">
          <label className="activation-filter-archived">
            <input
              type="checkbox"
              role="switch"
              aria-checked={page?.auto_extend ?? false}
              checked={page?.auto_extend ?? false}
              disabled={readOnly || busy || loading || !page}
              onChange={(event) =>
                openAction({ kind: "policy", enabled: event.target.checked })
              }
            />
            自动延长未使用激活码
          </label>
          <p className="admin-hint">
            开启后，未使用的码到期时每次延长 7
            天。关闭后按发码时的截止时间校验。
          </p>
        </div>
        <form
          className="activation-device-filters"
          onSubmit={(event) => {
            event.preventDefault();
            setOffset(0);
            setSearch(searchDraft.trim());
          }}
        >
          <label className="activation-filter-search">
            <span className="sr-only">搜索激活码或账号</span>
            <input
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              placeholder="搜索激活码或账号"
            />
          </label>
          <button type="submit">搜索</button>
        </form>
        {loading ? (
          <p className="admin-hint" role="status">
            正在读取软件授权…
          </p>
        ) : null}
        <div className="table-scroll admin-table-card">
          <table
            className="internal-table admin-data-table"
            aria-label="软件授权列表"
          >
            <thead>
              <tr>
                <th>激活码</th>
                <th>状态</th>
                <th>绑定账号</th>
                <th>初始授权年限</th>
                <th>激活截止</th>
                <th>软件使用到期</th>
                {!readOnly && <th>操作</th>}
              </tr>
            </thead>
            <tbody>
              {page?.items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <code>{item.masked_code}</code>
                  </td>
                  <td>
                    {item.expired
                      ? "已到期"
                      : item.username
                        ? "已绑定"
                        : "待激活"}
                  </td>
                  <td>{item.username ?? "—"}</td>
                  <td>{item.authorization_years} 年</td>
                  <td>{formatDateTime(item.activation_expires_at)}</td>
                  <td>{formatDateTime(item.authorized_until)}</td>
                  {!readOnly && (
                    <td>
                      {item.can_reveal && !item.expired ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void copyLicense(item.id)}
                        >
                          复制
                        </button>
                      ) : null}
                      {item.username ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            openAction({ kind: "renew", license: item })
                          }
                        >
                          续费一年
                        </button>
                      ) : null}
                    </td>
                  )}
                </tr>
              ))}
              {!loading && page?.items.length === 0 ? (
                <tr>
                  <td colSpan={readOnly ? 6 : 7}>暂无软件授权</td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
        {page ? (
          <Pagination
            total={page.total}
            limit={PAGE_SIZE}
            offset={offset}
            onPageChange={setOffset}
          />
        ) : null}
      </section>
      <ConfirmDialog
        open={action !== null}
        title={action ? actionLabels[action.kind] : ""}
        description={
          action?.kind === "renew"
            ? `请确认已收到「${action.license.username}」的续费款。授权将从当前到期日或续费当天（取较晚日期）顺延一年。`
            : action?.kind === "policy"
              ? action.enabled
                ? "开启后，所有未使用的软件激活码将按 7 天周期自动延期。"
                : "关闭后，所有未使用的软件激活码恢复按发码时的截止时间校验。"
              : undefined
        }
        level={action?.kind === "issue" ? "standard" : "reason"}
        confirmLabel={action?.kind === "issue" ? "生成激活码" : undefined}
        busy={busy}
        error={actionError}
        onConfirm={(reason) => void submitAction(reason)}
        onClose={() => setAction(null)}
      >
        {action?.kind === "issue" ? (
          <>
            <label>
              软件授权年限
              <select
                value={years}
                onChange={(event) => setYears(Number(event.target.value))}
                disabled={busy}
              >
                <option value={1}>1 年</option>
                <option value={2}>2 年</option>
                <option value={3}>3 年</option>
              </select>
            </label>
            <label>
              激活码截止时间（北京时间）
              <input
                type="datetime-local"
                required
                value={deadline}
                onChange={(event) => setDeadline(event.target.value)}
                disabled={busy}
              />
            </label>
          </>
        ) : null}
      </ConfirmDialog>
    </section>
  );
}
