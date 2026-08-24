import { useState } from "react";
import { customerEnrollDevice } from "../api";

/** Second-device pairing enrollment page (FE-03 / T30).
 * Collects the activation code, device fingerprint and name, and enrolls
 * through the customer API adapter so the request carries the
 * Idempotency-Key the server's enroll route requires (T17 / DEV-02).
 */
export function DevicePairingPage({
  onSuccess,
  onError,
  onCancel,
}: {
  onSuccess: (result: {
    status: "pending" | "consumed";
    data: unknown;
  }) => void;
  onError: (error: Error) => void;
  onCancel: () => void;
}): React.JSX.Element {
  const [isBusy, setIsBusy] = useState(false);
  const [activationCode, setActivationCode] = useState("");
  const [deviceFingerprint, setDeviceFingerprint] = useState("");
  const [deviceName, setDeviceName] = useState("");

  const handleEnroll = async () => {
    setIsBusy(true);
    try {
      const result = await customerEnrollDevice({
        activationCode: activationCode.trim(),
        deviceFingerprint: deviceFingerprint.trim(),
        deviceName: deviceName.trim(),
        devicePlatform: "web",
        // Every enrollment attempt gets its own key; the transport adds the
        // X-Request-Id correlation id.
        idempotencyKey: crypto.randomUUID(),
      });
      if (result.status === 202) {
        onSuccess({ status: "pending", data: result.pending });
      } else {
        onSuccess({ status: "consumed", data: result.credential });
      }
    } catch (cause) {
      const error = cause instanceof Error ? cause : new Error(String(cause));
      onError(error);
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <main className="device-pairing-page" aria-labelledby="page-title">
      <header>
        <h1 id="page-title">Pair New Device</h1>
        <p className="page-subtitle">
          Register your second device under this customer identity
        </p>
      </header>

      <form onSubmit={(e) => e.preventDefault()} className="pairing-form">
        <div className="form-group">
          <label htmlFor="activation-code">Activation Code</label>
          <input
            type="text"
            id="activation-code"
            value={activationCode}
            onChange={(e) => setActivationCode(e.target.value)}
            placeholder="XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"
            autoComplete="off"
            required
            aria-required="true"
          />
        </div>
        <div className="form-group">
          <label htmlFor="device-fingerprint">Device Fingerprint</label>
          <input
            type="text"
            id="device-fingerprint"
            value={deviceFingerprint}
            onChange={(e) => setDeviceFingerprint(e.target.value)}
            placeholder="e.g., Chrome on Windows •••• AB12"
            required
            aria-required="true"
          />
        </div>
        <div className="form-group">
          <label htmlFor="device-name">Device Name</label>
          <input
            type="text"
            id="device-name"
            value={deviceName}
            onChange={(e) => setDeviceName(e.target.value)}
            placeholder="e.g., My Personal Laptop"
            required
            aria-required="true"
          />
        </div>
        <div className="form-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={onCancel}
            disabled={isBusy}
          >
            Cancel
          </button>

          <button
            type="button"
            className="btn-primary"
            onClick={handleEnroll}
            disabled={isBusy}
          >
            {isBusy ? "Enrolling Device..." : "Enroll Device"}
          </button>
        </div>
      </form>
    </main>
  );
}
