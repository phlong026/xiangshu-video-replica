import { type FormEvent, useState } from "react";

import type { CustomerApiError } from "../api";
import type { CustomerActivationFormInput } from "./useCustomerSession";

/** The first-run activation form (FE-02): redeem an activation code, name
 * this device, and land in the workspace. The dev doc §13.2 client
 * behaviours drive the error copy: the anti-enumeration rejection shows the
 * server's unified message verbatim (never a guessed reason), a rate limit
 * shows the Retry-After wait instead of looping submits, and an idempotency
 * conflict surfaces the request id for the audit-trail lookup. */
export function ActivationPage({
  onActivate,
  isBusy,
  error,
}: {
  onActivate(input: CustomerActivationFormInput): void;
  isBusy: boolean;
  error: CustomerApiError | null;
}) {
  const [activationCode, setActivationCode] = useState("");
  const [deviceName, setDeviceName] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isBusy || !activationCode.trim() || !deviceName.trim()) {
      return;
    }
    onActivate({
      activationCode: activationCode.trim(),
      deviceName: deviceName.trim(),
    });
  };

  return (
    <section className="login-card" aria-labelledby="activation-title">
      <span className="eyebrow">JINGXU STUDIO</span>
      <h1 id="activation-title">激活短视频复刻工作台</h1>
      <p className="login-hint">
        输入激活码完成首次激活，激活后本机将成为您的第一台绑定设备。
      </p>
      <form onSubmit={handleSubmit}>
        <label>
          激活码
          <input
            value={activationCode}
            onChange={(event) => setActivationCode(event.target.value)}
            autoComplete="off"
            placeholder="XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"
            disabled={isBusy}
          />
        </label>
        <label>
          设备名称
          <input
            value={deviceName}
            onChange={(event) => setDeviceName(event.target.value)}
            autoComplete="off"
            placeholder="例如：工作电脑"
            disabled={isBusy}
          />
        </label>
        {error ? (
          <p className="form-error">{activationErrorText(error)}</p>
        ) : null}
        <button type="submit" disabled={isBusy}>
          {isBusy ? "正在激活…" : "激活并进入工作台"}
        </button>
      </form>
    </section>
  );
}

function activationErrorText(error: CustomerApiError): string {
  switch (error.kind) {
    case "rate-limited":
      return error.retryAfterSeconds !== undefined
        ? `${error.message}，请等待 ${error.retryAfterSeconds} 秒后重试`
        : error.message;
    case "idempotency-conflict":
      return error.requestId
        ? `${error.message}（请求编号 ${error.requestId}，请联系客服核查）`
        : error.message;
    default:
      return error.message;
  }
}
