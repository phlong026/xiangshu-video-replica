/** Device pairing approval card (FE-03 / T30).
 * Shows pending device pairings waiting for user confirmation.
 */
export function PairingApprovalCard({
  pairing,
  onApprove,
  onReject,
}: {
  pairing: {
    id: string;
    deviceFingerprint: string;
    /** The slot is assigned at approval time, so a pending request (which
     * carries no slot yet) renders "待分配" instead of a fabricated number. */
    slotNo?: number;
    createdAt: string;
  };
  onApprove: (pairingId: string) => void;
  onReject: () => void;
}): React.JSX.Element {
  return (
    <article
      className="pairing-approval-card"
      aria-labelledby={`pairing-${pairing.id}-title`}
    >
      <header>
        <h2 id={`pairing-${pairing.id}-title`}>Device Pairing Request</h2>
        <p className="card-subtitle">
          A device is requesting to pair with your account
        </p>
      </header>

      <div className="pairing-details">
        <p className="fingerprint-label">Device Fingerprint:</p>
        <span className="fingerprint-value">{pairing.deviceFingerprint}</span>

        <p className="slot-label">Slot:</p>
        <p className="slot-value">
          {pairing.slotNo === undefined
            ? "待分配 (审批时确定)"
            : `Slot #${pairing.slotNo}`}
        </p>

        <p className="request-time">
          Requested at: {new Date(pairing.createdAt).toLocaleString()}
        </p>
      </div>

      <p className="pending-status">Pending - waiting for your confirmation</p>

      <div className="card-actions">
        <button
          type="button"
          className="btn-secondary"
          onClick={onReject}
          aria-label="Reject this pairing request"
        >
          Reject
        </button>

        <button
          type="button"
          className="btn-primary"
          onClick={() => onApprove(pairing.id)}
          aria-label={`Approve pairing for device ${pairing.deviceFingerprint}`}
        >
          Approve Pairing
        </button>
      </div>
    </article>
  );
}
