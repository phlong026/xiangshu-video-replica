import { type FormEvent, useEffect, useState } from "react";

import {
  type BillingSettings,
  type ControlSettings,
  getControlSettings,
  updateControlBillingSettings,
  updateControlZPaySettings,
} from "../api";
import { PageBanner } from "./ui/PageBanner";
import { formatFen } from "./ui/vocabulary";

/**
 * 支付与价格设置（从 AdminApp 内联表单抽出）。密钥只展示掩码，
 * 新密钥留空表示保留旧值；网关与回调地址来自部署环境，不可提交。
 */
export function PaymentSettingsSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [settings, setSettings] = useState<ControlSettings | null>(null);
  const [zpayPid, setZpayPid] = useState("");
  const [zpayKey, setZpayKey] = useState("");
  const [channels, setChannels] = useState<Array<"alipay" | "wxpay">>([
    "alipay",
    "wxpay",
  ]);
  const [billing, setBilling] = useState<BillingSettings | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setError("");
      try {
        const nextSettings = await getControlSettings();
        if (cancelled) {
          return;
        }
        setSettings(nextSettings);
        setBilling(nextSettings.billing);
        setZpayPid(nextSettings.zpay.config.pid ?? "");
        setZpayKey("");
        setChannels(parseChannels(nextSettings.zpay.config.enabled_channels));
      } catch (cause) {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message
              ? `加载失败：${cause.message}`
              : "加载失败：读取支付与价格失败。",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function saveZPay(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice("");
    setError("");
    setSaving(true);
    try {
      await updateControlZPaySettings({
        pid: zpayPid,
        key: zpayKey,
        enabled_channels: channels,
      });
      setNotice("ZPay 设置已保存。");
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "保存 ZPay 设置失败。",
      );
    } finally {
      setSaving(false);
    }
  }

  async function saveBilling(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!billing) {
      return;
    }
    setNotice("");
    setError("");
    setSaving(true);
    try {
      const nextBilling = await updateControlBillingSettings({
        internal_base_unit_price_fen: billing.internal_base_unit_price_fen,
        min_recharge_fen: billing.min_recharge_fen,
        recharge_step_fen: billing.recharge_step_fen,
      });
      setBilling(nextBilling);
      setNotice("内部价格已保存。");
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "保存内部价格失败。",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="支付与价格" className="admin-panel">
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      <form className="admin-form" onSubmit={saveZPay}>
        <h2>ZPay</h2>
        <label>
          ZPay 商户 PID
          <input
            disabled={readOnly}
            value={zpayPid}
            onChange={(event) => setZpayPid(event.target.value)}
          />
        </label>
        <div className="admin-readonly-field">
          已保存密钥
          <span className="readonly-value">
            {settings?.zpay.config.key || "未配置"}
          </span>
        </div>
        <label>
          新商户密钥
          <input
            autoComplete="new-password"
            disabled={readOnly}
            placeholder="留空则保留当前密钥"
            type="password"
            value={zpayKey}
            onChange={(event) => setZpayKey(event.target.value)}
          />
        </label>
        <fieldset className="admin-checks">
          <legend>支付渠道</legend>
          <label>
            <input
              checked={channels.includes("alipay")}
              disabled={readOnly}
              type="checkbox"
              onChange={() => toggleChannel("alipay")}
            />
            支付宝
          </label>
          <label>
            <input
              checked={channels.includes("wxpay")}
              disabled={readOnly}
              type="checkbox"
              onChange={() => toggleChannel("wxpay")}
            />
            微信
          </label>
        </fieldset>
        <p className="admin-hint">支付接口地址由系统自动配置，无需填写。</p>
        <button disabled={readOnly || saving} type="submit">
          保存 ZPay 设置
        </button>
      </form>

      <form className="admin-form" onSubmit={saveBilling}>
        <h2>内部价格</h2>
        <p className="admin-hint">
          当前内部单价{" "}
          {billing ? formatFen(billing.internal_base_unit_price_fen) : "—"} /
          条；最低充值与步长只约束 ZPay 在线充值。
        </p>
        <label>
          内部单价（分/条）
          <input
            disabled={readOnly}
            inputMode="numeric"
            type="number"
            value={billing?.internal_base_unit_price_fen ?? 0}
            onChange={(event) =>
              updateBilling("internal_base_unit_price_fen", event.target.value)
            }
          />
        </label>
        <label>
          最低充值（分）
          <input
            disabled={readOnly}
            inputMode="numeric"
            type="number"
            value={billing?.min_recharge_fen ?? 0}
            onChange={(event) =>
              updateBilling("min_recharge_fen", event.target.value)
            }
          />
        </label>
        <label>
          递增步长（分）
          <input
            disabled={readOnly}
            inputMode="numeric"
            type="number"
            value={billing?.recharge_step_fen ?? 0}
            onChange={(event) =>
              updateBilling("recharge_step_fen", event.target.value)
            }
          />
        </label>
        <button disabled={readOnly || saving} type="submit">
          保存内部价格
        </button>
      </form>
    </section>
  );

  function toggleChannel(channel: "alipay" | "wxpay") {
    setChannels((current) =>
      current.includes(channel)
        ? current.filter((item) => item !== channel)
        : [...current, channel],
    );
  }

  function updateBilling(field: keyof BillingSettings, value: string) {
    setBilling((current) =>
      current ? { ...current, [field]: Number(value) } : current,
    );
  }
}

function parseChannels(value: unknown): Array<"alipay" | "wxpay"> {
  if (typeof value !== "string") {
    return ["alipay"];
  }
  const next = value
    .split(",")
    .filter(
      (item): item is "alipay" | "wxpay" =>
        item === "alipay" || item === "wxpay",
    );
  return next.length ? next : ["alipay"];
}
