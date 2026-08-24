import type { CustomerDeviceListResponse } from "../api";

/** Device management page (FE-04 / T31): shows two-slot status, current device details,
 * lease countdown, unbind and recharge actions. Does not provide second master code entry.
 *
 * Consumes the regenerated device contract (slots/history; the current device is
 * flagged by ``is_current``, and only bound rows occupy a slot).
 */
export function DeviceManagementPage({
  devices,
  isOnline,
  leaseExpiresAt,
  onUnbind,
  onRecharge,
}: {
  devices: CustomerDeviceListResponse;
  isOnline: boolean;
  /** The live session lease, when the session transport exposes one; the device
   * contract itself carries no lease timestamp. */
  leaseExpiresAt?: string | null;
  onUnbind: (deviceId: string) => void;
  onError: (error: Error) => void;
  onRecharge: () => void;
}): React.JSX.Element {
  const occupiedSlots = devices.slots.flatMap((slot) =>
    slot.device === null
      ? []
      : [{ slot_no: slot.slot_no, device: slot.device }],
  );
  const nextFreeSlot =
    devices.slots.find((slot) => slot.device === null)?.slot_no ?? null;

  return (
    <main className="device-management-page" aria-labelledby="page-title">
      <header>
        <h1 id="page-title">Device Management</h1>

        <div
          className={`online-status ${isOnline ? "online" : "offline"}`}
          aria-live="polite"
        >
          Status:
          <strong>{isOnline ? "Online" : "Offline"}</strong>
        </div>
      </header>

      <section className="slot-status" aria-label="Two Slot Status">
        <h2>Your Account Supports Two Devices</h2>

        {occupiedSlots.map(({ slot_no, device }) => (
          <article
            key={device.id}
            className={`slot-card slot-${slot_no} ${device.is_current ? "active" : "inactive"}`}
            aria-labelledby={`slot-${slot_no}-title`}
          >
            <header>
              <h3 id={`slot-${slot_no}-title`}>
                Slot #{slot_no}
                <span
                  className={`status-badge ${device.is_current ? "online" : "inactive"}`}
                >
                  {device.is_current ? "● Online" : "○ Inactive"}
                </span>
              </h3>
            </header>

            <div className="card-body">
              <p className="device-name">
                <strong>Device:</strong> {device.display_name}
              </p>

              {device.is_current && isOnline && leaseExpiresAt && (
                <>
                  <p className="lease-info">
                    <strong>Session expires at:</strong>{" "}
                    {new Date(leaseExpiresAt).toLocaleString()}
                  </p>
                  <p className="lease-countdown">
                    <time dateTime={leaseExpiresAt}>Lease countdown: ...</time>
                  </p>
                </>
              )}

              <div className="slot-actions">
                {device.is_current ? (
                  <>
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => onUnbind(device.id)}
                      disabled={!isOnline}
                      aria-describedby={`unbind-desc-${slot_no}`}
                    >
                      Unbind This Device
                    </button>
                    <button
                      type="button"
                      className="btn-primary"
                      onClick={onRecharge}
                      disabled={!isOnline}
                      aria-describedby="recharge-desc"
                    >
                      Recharge
                    </button>
                  </>
                ) : (
                  <p className="inactive-message">
                    No active session in this slot.{" "}
                    <a href="/customer/pairing" aria-label="Pair a new device">
                      Pair a new device →
                    </a>
                  </p>
                )}
              </div>
            </div>
          </article>
        ))}

        {nextFreeSlot !== null && (
          <p className="next-slot-info">
            You currently have only one device registered.{" "}
            <strong>Next free slot: #{nextFreeSlot}</strong>.{" "}
            <a href="/customer/pairing" aria-label="Register second device">
              Register your second device →
            </a>
          </p>
        )}
      </section>

      <aside className="info-banner">
        <h3>Important Notes</h3>
        <ul>
          <li>
            Your activation code supports up to two devices simultaneously.
          </li>
          <li>The same customer identity can be used across both devices.</li>
          <li>
            Recharging extends the total consumption quota (not per-device).
          </li>
          <li>
            For security reasons, you must confirm any critical action like
            unbinding.
          </li>
        </ul>
      </aside>
    </main>
  );
}
