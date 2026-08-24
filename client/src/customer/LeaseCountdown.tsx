/** Lease countdown display with auto-refresh (FE-04 / T31).
 * Shows formatted expiry time, warning states, and provides refresh action.
 */
export function LeaseCountdown({
  expiresAt,
  onRefresh,
}: {
  expiresAt: string;
  onRefresh: () => void;
}): JSX.Element {
  const now = new Date();
  const expiry = new Date(expiresAt);
  const diffMs = expiry.getTime() - now.getTime();

  let status: "normal" | "warning" | "expired" = "normal";
  if (diffMs <= 0) {
    status = "expired";
  } else if (diffMs < 5 * 60_000) {
    // Less than 5 minutes
    status = "warning";
  }

  const minutesRemaining = Math.floor(diffMs / 60_000);
  const formattedExpiry = expiry.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className={`lease-countdown ${status}`}>
      <p className="label">
        Session expires at: <time dateTime={expiresAt}>{formattedExpiry}</time>
      </p>

      {status === "expired" && (
        <span className="status-text expired">
          ⚠️ Expired — please refresh your session
        </span>
      )}

      {status === "warning" && (
        <span className="status-text warning">
          ⏰ Expiring soon ({minutesRemaining} minutes remaining)
        </span>
      )}

      {status === "normal" && (
        <span className="status-text normal">
          {minutesRemaining} minutes remaining
        </span>
      )}

      <button
        type="button"
        className="btn-renew"
        onClick={onRefresh}
        disabled={status === "expired"}
        aria-label="Renew or extend this session"
      >
        Refresh Session
      </button>
    </div>
  );
}
