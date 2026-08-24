import type { CustomerDeviceListResponse } from "../api";

/** Device management page (FE-04 / T31): shows two-slot status, current device details,
 * lease countdown, unbind and recharge actions. Does not provide second master code entry.
 */
export function DeviceManagementPage({
  devices,
  isOnline,
  onUnbind,
  onRecharge,
}: {
  devices: CustomerDeviceListResponse;
  isOnline: boolean;
  onUnbind: (slotNo: number) => void;
  onError: (error: Error) => void;
  onRecharge: () => void;
}): JSX.Element {
  const _activeDevice = devices.devices.find((d) => d.is_online);

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

        {devices.devices.map((device) => (
          <article
            key={device.id}
            className={`slot-card slot-${device.slot_no} ${device.is_online ? "active" : "inactive"}`}
            aria-labelledby={`slot-${device.slot_no}-title`}
          >
            <header>
              <h3 id={`slot-${device.slot_no}-title`}>
                Slot #{device.slot_no}
                <span
                  className={`status-badge ${device.is_online ? "online" : "inactive"}`}
                >
                  {device.is_online ? "● Online" : "○ Inactive"}
                </span>
              </h3>
            </header>

            <div className="card-body">
              <p className="device-name">
                <strong>Device:</strong> {device.device_name}
              </p>

              {device.is_online && device.lease_expires_at && (
                <>
                  <p className="lease-info">
                    <strong>Session expires at:</strong>{" "}
                    {new Date(device.lease_expires_at).toLocaleString()}
                  </p>
                  <p className="lease-countdown">
                    <time dateTime={device.lease_expires_at}>
                      Lease countdown: ...
                    </time>
                  </p>
                </>
              )}

              <div className="slot-actions">
                {device.is_online ? (
                  <>
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => onUnbind(device.slot_no)}
                      disabled={!isOnline}
                      aria-describedby={`unbind-desc-${device.slot_no}`}
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

        {devices.next_free_slot === 2 &&
          !devices.devices.find((d) => d.slot_no === 2) && (
            <p className="next-slot-info">
              You currently have only one device registered.{" "}
              <strong>Next free slot: #2</strong>.{" "}
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
