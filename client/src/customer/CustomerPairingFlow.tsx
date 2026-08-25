import { useState } from "react";
import { DevicePairingPage } from "./DevicePairingPage";
import type { CustomerCredentialStore } from "./useCustomerSession";

/**
 * T30 / FE-03 — the second-device pairing entry at /customer/pairing.
 *
 * Wires the M4-shipped DevicePairingPage into the customer lane:
 * - a 202 enroll (another device is already bound) parks the user on a
 *   waiting screen with the pairing expiry; the next enroll attempt is
 *   idempotent server-side, so "我已审批,重新配对" is the polling-free
 *   re-check that eventually lands on 201;
 * - a 201 enroll (primary device approved meanwhile) stores the device
 *   credential — the same shape the vault holds after activation, but
 *   without a session token — then hands back to /customer, where the
 *   state machine's boot path logs the new device in automatically.
 */
export function CustomerPairingFlow({
  store,
  onPaired,
}: {
  store: CustomerCredentialStore;
  onPaired: () => void;
}) {
  const [stage, setStage] = useState<"form" | "pending" | "consumed">("form");
  const [pendingExpiry, setPendingExpiry] = useState("");
  const [error, setError] = useState("");

  async function handleEnrollSuccess(result: {
    status: "pending" | "consumed";
    data: unknown;
  }) {
    setError("");
    if (result.status === "pending") {
      const pending = result.data as { expires_at: string };
      setPendingExpiry(pending.expires_at);
      setStage("pending");
      return;
    }
    const consumed = result.data as { device_token: string };
    try {
      // The consumed branch carries only the device credential; the session
      // token arrives on the first login, so the vault is primed without one.
      await store.saveActivation(consumed.device_token, "");
      setStage("consumed");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存设备凭据失败");
      setStage("form");
    }
  }

  if (stage === "pending") {
    return (
      <main
        className="device-pairing-page"
        aria-labelledby="pairing-waiting-title"
      >
        <header>
          <h1 id="pairing-waiting-title">等待主设备审批</h1>
          <p className="page-subtitle">
            配对请求已提交,请在主设备上打开设备管理并确认本次配对。
          </p>
        </header>
        <section className="pairing-pending" aria-live="polite">
          {pendingExpiry ? (
            <p className="request-time">
              本请求将在 {new Date(pendingExpiry).toLocaleString()} 过期。
            </p>
          ) : null}
          <p className="pending-status">Pending - 等待主设备确认</p>
          <div className="form-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setStage("form")}
            >
              返回修改
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => setStage("form")}
            >
              我已审批,重新配对
            </button>
          </div>
        </section>
      </main>
    );
  }

  if (stage === "consumed") {
    return (
      <main
        className="device-pairing-page"
        aria-labelledby="pairing-success-title"
      >
        <header>
          <h1 id="pairing-success-title">配对成功</h1>
          <p className="page-subtitle">本设备已加入您的客户账号。</p>
        </header>
        <section className="pairing-pending" aria-live="polite">
          <button type="button" className="btn-primary" onClick={onPaired}>
            进入客户工作区
          </button>
        </section>
      </main>
    );
  }

  return (
    <>
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      <DevicePairingPage
        onSuccess={(result) => void handleEnrollSuccess(result)}
        onError={(cause) => setError(cause.message)}
        onCancel={onPaired}
      />
    </>
  );
}
