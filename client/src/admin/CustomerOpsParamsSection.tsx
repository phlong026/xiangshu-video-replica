import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  adminActivationErrorMessage,
  type CustomerOpsParams,
  getCustomerOpsParams,
  updateCustomerOpsParams,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";

const MAX_BALANCE = 1_000_000;

interface Draft {
  balance: string;
  score: string;
}

function toDraft(params: CustomerOpsParams): Draft {
  return {
    balance: String(params.low_balance_threshold),
    score: String(params.health_alert_score),
  };
}

function parseDraft(
  draft: Draft,
):
  | { ok: true; balance: number; score: number }
  | { ok: false; message: string } {
  const balance = Number(draft.balance.trim());
  const score = Number(draft.score.trim());
  if (
    !draft.balance.trim() ||
    !Number.isInteger(balance) ||
    balance < 0 ||
    balance > MAX_BALANCE
  )
    return {
      ok: false,
      message: `低余额阈值需为 0 到 ${MAX_BALANCE} 之间的整数`,
    };
  if (
    !draft.score.trim() ||
    !Number.isInteger(score) ||
    score < 1 ||
    score > 100
  )
    return { ok: false, message: "健康分预警线需为 1 到 100 之间的整数" };
  return { ok: true, balance, score };
}

/**
 * 客户运营参数（整改 T29）：低余额阈值与健康分预警线。
 *
 * 客户列表「余额不足」卡片和客户运营的健康分共用这两个值，改一次两页同步；
 * 此前阈值只能在列表里临时手填，健康分的 100 / 40 写死在服务端 SQL 里。
 */
export function CustomerOpsParamsSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [params, setParams] = useState<CustomerOpsParams | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [formError, setFormError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [saving, setSaving] = useState(false);
  // 同一份内容失败后重试沿用同一幂等键，改了内容才换键。
  const retryRef = useRef<{ fingerprint: string; key: string } | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      const current = await getCustomerOpsParams();
      setParams(current);
      setDraft(toDraft(current));
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取运营参数失败"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!params || !draft) {
    return (
      <section aria-label="运营参数" className="admin-panel">
        <h2>运营参数</h2>
        {error ? (
          <>
            <PageBanner tone="error">{error}</PageBanner>
            <button type="button" onClick={() => void load()}>
              重新读取
            </button>
          </>
        ) : (
          <p className="admin-hint">正在读取运营参数…</p>
        )}
      </section>
    );
  }

  const parsed = parseDraft(draft);
  const dirty =
    !parsed.ok ||
    parsed.balance !== params.low_balance_threshold ||
    parsed.score !== params.health_alert_score;

  function patch(next: Partial<Draft>) {
    setDraft((current) => (current ? { ...current, ...next } : current));
    setFormError("");
    setNotice("");
  }

  function requestSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!parsed.ok) {
      setFormError(parsed.message);
      return;
    }
    setDialogError("");
    setDialogOpen(true);
  }

  async function persist(reason: string) {
    if (!parsed.ok || saving) return;
    const fingerprint = `${parsed.balance}:${parsed.score}`;
    if (retryRef.current?.fingerprint !== fingerprint)
      retryRef.current = { fingerprint, key: crypto.randomUUID() };
    setSaving(true);
    setDialogError("");
    try {
      const updated = await updateCustomerOpsParams({
        lowBalanceThreshold: parsed.balance,
        healthAlertScore: parsed.score,
        reason,
        idempotencyKey: retryRef.current.key,
      });
      retryRef.current = null;
      setParams(updated);
      setDraft(toDraft(updated));
      setNotice("运营参数已保存：客户列表与客户运营已同步使用新口径。");
      setDialogOpen(false);
    } catch (cause) {
      setDialogError(adminActivationErrorMessage(cause, "保存运营参数失败"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="运营参数" className="admin-panel">
      <h2>运营参数</h2>
      <p className="admin-hint">{params.rule}</p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {readOnly ? (
        <dl className="admin-settings-view">
          <div>
            <dt>低余额阈值</dt>
            <dd>{params.low_balance_threshold} 积分</dd>
          </div>
          <div>
            <dt>健康分预警线</dt>
            <dd>{params.health_alert_score} 分</dd>
          </div>
        </dl>
      ) : (
        <form className="admin-form" onSubmit={requestSave}>
          <label>
            低余额阈值（积分）
            <input
              inputMode="numeric"
              value={draft.balance}
              onChange={(event) => patch({ balance: event.target.value })}
            />
          </label>
          <label>
            健康分预警线（分）
            <input
              inputMode="numeric"
              value={draft.score}
              onChange={(event) => patch({ score: event.target.value })}
            />
          </label>
          {formError ? <PageBanner tone="error">{formError}</PageBanner> : null}
          <div className="admin-actions">
            <button disabled={saving || !dirty} type="submit">
              保存运营参数
            </button>
            <button
              disabled={saving || !dirty}
              type="button"
              onClick={() => {
                setDraft(toDraft(params));
                setFormError("");
              }}
            >
              还原
            </button>
          </div>
        </form>
      )}
      <ConfirmDialog
        busy={saving}
        confirmLabel="确认保存"
        description="保存后客户列表「余额不足」和客户运营健康分立即按新口径计算，不回溯历史记录。"
        error={dialogError}
        level="reason"
        open={dialogOpen}
        title="保存运营参数"
        onClose={() => {
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={(reason) => void persist(reason)}
      />
    </section>
  );
}
