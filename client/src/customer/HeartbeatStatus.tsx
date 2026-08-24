/** Heartbeat status indicator (FE-04 / T31).
 * Shows the last heartbeat time, refresh interval, and connection health.
 */
export function HeartbeatStatus({
  lastHeartbeatAt,
  intervalSeconds = 30,
  onRefresh,
}: {
  lastHeartbeatAt: string;
  intervalSeconds?: number;
  onRefresh: () => void;
}): JSX.Element {
  const now = new Date();
  const heartbeat = new Date(lastHeartbeatAt);
  const diffMs = now.getTime() - heartbeat.getTime();
  const secondsSinceLast = Math.floor(diffMs / 1_000);

  // Status thresholds
  const isHealthy = secondsSinceLast < intervalSeconds * 0.8; // < 80% of interval
  const isWarning = secondsSinceLast >= intervalSeconds; // overdue
  const isExpired = secondsSinceLast > intervalSeconds * 2; // > 2x interval

  const formattedHeartbeat = heartbeat.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  return (
    <div
      className={`heartbeat-status ${isHealthy ? "healthy" : isWarning ? "warning" : "expired"}`}
      role="status"
      aria-live="polite"
    >
      <p className="label">
        Last heartbeat:{" "}
        <time dateTime={lastHeartbeatAt}>{formattedHeartbeat}</time>
      </p>

      <p className="interval-info">Refreshes every {intervalSeconds} seconds</p>

      {isHealthy && (
        <span className="status-text healthy">✓ Connection healthy</span>
      )}

      {isWarning && !isExpired && (
        <span className="status-text warning">
          ⚠️ Heartbeat overdue — will expire soon
        </span>
      )}

      {isExpired && (
        <span className="status-text expired">
          ✕ Session expired — refresh required
        </span>
      )}

      <button
        type="button"
        className="btn-refresh"
        onClick={onRefresh}
        disabled={isExpired}
        aria-label="Manually trigger a heartbeat refresh"
      >
        Send Heartbeat Now
      </button>

      <footer className="help-text">
        The session automatically refreshes every {intervalSeconds} seconds. If
        inactive for too long, you may need to log in again.
      </footer>
    </div>
  );
}
