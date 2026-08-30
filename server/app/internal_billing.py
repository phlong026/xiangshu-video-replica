from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from app.db_portable import BusinessConnection

BillingOutcome = Literal["success", "failed", "cancelled"]
TerminalTransactionType = Literal["SETTLE", "RELEASE"]


class InternalBillingError(RuntimeError):
    """Base error for wallet invariants that callers must not silently ignore."""


class InsufficientCreditsError(InternalBillingError):
    pass


class BillingInvariantError(InternalBillingError):
    pass


@dataclass(frozen=True)
class BillingFinalization:
    task_id: str
    billing_round: int | None
    transaction_type: TerminalTransactionType | None


@dataclass(frozen=True)
class DanglingBillingReservation:
    task_id: str
    user_id: str
    billing_round: int
    reservation_id: str
    outcome: BillingOutcome


@dataclass(frozen=True)
class DanglingBillingReconciliation:
    scanned: int
    settled: int
    released: int
    failed: int


def find_dangling_billing_reservations(
    conn: BusinessConnection,
    *,
    limit: int = 100,
) -> list[DanglingBillingReservation]:
    """RESERVE rows whose task reached a terminal state without a terminal row.

    BILL-03: a dangling RESERVE holds one credit of the user's wallet
    reserved forever. The task is already terminal (archived success or
    failed/cancelled), so finalization is safe; the terminal write was lost
    when a fenced worker transaction rolled back after the task UPDATE was
    visible. SUBMISSION_UNCERTAIN and un-archived tasks are NOT terminal and
    must keep their reservation until reconciliation decides.
    """
    rows = conn.execute(
        """
        SELECT
            wt.id AS reservation_id,
            wt.task_id,
            wt.user_id,
            wt.billing_round,
            task.status
        FROM wallet_transactions AS wt
        JOIN generation_tasks AS task ON task.id = wt.task_id
        WHERE wt.type = 'RESERVE'
          AND NOT EXISTS (
              SELECT 1
              FROM wallet_transactions AS terminal
              WHERE terminal.task_id = wt.task_id
                AND terminal.billing_round = wt.billing_round
                AND terminal.type IN ('SETTLE', 'RELEASE')
          )
          AND (
              (task.status = 'SUCCEEDED' AND task.archive_status = 'ARCHIVED')
              OR task.status IN ('FAILED', 'CANCELLED')
          )
        ORDER BY wt.created_at
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        DanglingBillingReservation(
            task_id=str(row["task_id"]),
            user_id=str(row["user_id"]),
            billing_round=int(row["billing_round"]),
            reservation_id=str(row["reservation_id"]),
            outcome=(
                "success"
                if str(row["status"]) == "SUCCEEDED"
                else "cancelled"
                if str(row["status"]) == "CANCELLED"
                else "failed"
            ),
        )
        for row in rows
    ]


def reserve_internal_billing(
    conn: BusinessConnection,
    *,
    user_id: str,
    task_id: str,
    billing_round: int | None = None,
) -> int:
    """Reserve one credit inside the caller's existing database transaction."""
    task = conn.execute(
        """
        SELECT batch.created_by_user_id
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        WHERE task.id = %s
        """,
        (task_id,),
    ).fetchone()
    if task is None:
        raise BillingInvariantError("generation task does not exist")
    if str(task["created_by_user_id"]) != user_id:
        raise BillingInvariantError("wallet owner does not match generation task owner")

    latest = conn.execute(
        """
        SELECT billing_round
        FROM wallet_transactions
        WHERE task_id = %s AND type = 'RESERVE'
        ORDER BY billing_round DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    latest_round = int(latest["billing_round"]) if latest is not None else None

    if billing_round is None:
        if latest_round is None:
            billing_round = 1
        else:
            terminal = conn.execute(
                """
                SELECT 1
                FROM wallet_transactions
                WHERE task_id = %s AND billing_round = %s
                  AND type IN ('SETTLE', 'RELEASE')
                """,
                (task_id, latest_round),
            ).fetchone()
            if terminal is None:
                return latest_round
            billing_round = latest_round + 1
    if billing_round < 1:
        raise BillingInvariantError("billing round must be positive")

    existing = conn.execute(
        """
        SELECT user_id
        FROM wallet_transactions
        WHERE task_id = %s AND billing_round = %s AND type = 'RESERVE'
        """,
        (task_id, billing_round),
    ).fetchone()
    if existing is not None:
        if str(existing["user_id"]) != user_id:
            raise BillingInvariantError("existing reservation belongs to another wallet")
        return billing_round
    if latest_round is not None and billing_round <= latest_round:
        raise BillingInvariantError("billing round cannot move backwards")
    if latest_round is not None:
        if billing_round != latest_round + 1:
            raise BillingInvariantError("billing rounds must be sequential")
        previous_terminal = conn.execute(
            """
            SELECT 1
            FROM wallet_transactions
            WHERE task_id = %s AND billing_round = %s
              AND type IN ('SETTLE', 'RELEASE')
            """,
            (task_id, latest_round),
        ).fetchone()
        if previous_terminal is None:
            raise BillingInvariantError("previous billing round is still active")

    cursor = conn.execute(
        """
        UPDATE wallets
        SET
            available_credits = available_credits - 1,
            reserved_credits = reserved_credits + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = %s AND available_credits >= 1
        """,
        (user_id,),
    )
    if cursor.rowcount != 1:
        wallet = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if wallet is None:
            raise BillingInvariantError("wallet does not exist")
        raise InsufficientCreditsError("available credits are insufficient")

    conn.execute(
        """
        INSERT INTO wallet_transactions (
            id, user_id, type, available_delta, reserved_delta,
            task_id, billing_round, idempotency_key
        ) VALUES (%s, %s, 'RESERVE', -1, 1, %s, %s, %s)
        """,
        (
            str(uuid4()),
            user_id,
            task_id,
            billing_round,
            f"reserve:{task_id}:{billing_round}",
        ),
    )
    return billing_round


def finalize_internal_billing(
    conn: BusinessConnection,
    *,
    task_id: str,
    outcome: BillingOutcome,
) -> BillingFinalization:
    """Settle or release the latest reserved round in the caller's transaction."""
    task = conn.execute(
        """
        SELECT
            task.status,
            task.archive_status,
            task.result_asset_id,
            task.provider,
            batch.created_by_user_id,
            asset.storage_uri
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        LEFT JOIN assets AS asset ON asset.id = task.result_asset_id
        WHERE task.id = %s
        """,
        (task_id,),
    ).fetchone()
    if task is None:
        raise BillingInvariantError("generation task does not exist")

    reservation = conn.execute(
        """
        SELECT user_id, billing_round
        FROM wallet_transactions
        WHERE task_id = %s AND type = 'RESERVE'
        ORDER BY billing_round DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if reservation is None:
        # Tasks created before internal billing was enabled remain historical
        # records. They must not mutate a wallet retroactively.
        return BillingFinalization(task_id=task_id, billing_round=None, transaction_type=None)

    user_id = str(reservation["user_id"])
    billing_round = int(reservation["billing_round"])
    if user_id != str(task["created_by_user_id"]):
        raise BillingInvariantError("reservation owner does not match generation task owner")

    existing = conn.execute(
        """
        SELECT type
        FROM wallet_transactions
        WHERE task_id = %s AND billing_round = %s
          AND type IN ('SETTLE', 'RELEASE')
        """,
        (task_id, billing_round),
    ).fetchone()
    if existing is not None:
        return BillingFinalization(
            task_id=task_id,
            billing_round=billing_round,
            transaction_type=cast(TerminalTransactionType, str(existing["type"])),
        )

    if outcome == "success":
        storage_uri = task["storage_uri"]
        if (
            str(task["status"]) != "SUCCEEDED"
            or str(task["archive_status"]) != "ARCHIVED"
            or task["result_asset_id"] is None
            or storage_uri is None
            or not str(storage_uri).strip()
            or (str(task["provider"]) == "metaso" and not str(storage_uri).startswith("cos://"))
        ):
            raise BillingInvariantError("successful billing requires an archived result asset")
        transaction_type: TerminalTransactionType = "SETTLE"
        available_delta = 0
    else:
        if str(task["status"]) not in {"FAILED", "CANCELLED"}:
            raise BillingInvariantError("released billing requires a failed or cancelled task")
        transaction_type = "RELEASE"
        available_delta = 1

    cursor = conn.execute(
        """
        UPDATE wallets
        SET
            available_credits = available_credits + %s,
            reserved_credits = reserved_credits - 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = %s AND reserved_credits >= 1
        """,
        (available_delta, user_id),
    )
    if cursor.rowcount != 1:
        raise BillingInvariantError("reserved wallet credit is missing")

    conn.execute(
        """
        INSERT INTO wallet_transactions (
            id, user_id, type, available_delta, reserved_delta,
            task_id, billing_round, idempotency_key
        ) VALUES (%s, %s, %s, %s, -1, %s, %s, %s)
        """,
        (
            str(uuid4()),
            user_id,
            transaction_type,
            available_delta,
            task_id,
            billing_round,
            f"{transaction_type.lower()}:{task_id}:{billing_round}",
        ),
    )
    return BillingFinalization(
        task_id=task_id,
        billing_round=billing_round,
        transaction_type=transaction_type,
    )


def reconcile_dangling_billing_reservations(
    conn: BusinessConnection,
    *,
    limit: int = 100,
) -> DanglingBillingReconciliation:
    """Finalize safe terminal reservations that lost their terminal ledger row.

    The detector deliberately excludes active and ``SUBMISSION_UNCERTAIN``
    tasks. Each candidate is revalidated by ``finalize_internal_billing`` in
    the caller-owned transaction, so a stale sweep cannot settle an
    unarchived result or release a non-terminal task.
    """
    candidates = find_dangling_billing_reservations(conn, limit=limit)
    settled = 0
    released = 0
    failed = 0
    for candidate in candidates:
        conn.execute("SAVEPOINT billing_reconcile_item")
        try:
            result = finalize_internal_billing(
                conn,
                task_id=candidate.task_id,
                outcome=candidate.outcome,
            )
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT billing_reconcile_item")
            conn.execute("RELEASE SAVEPOINT billing_reconcile_item")
            failed += 1
            continue
        conn.execute("RELEASE SAVEPOINT billing_reconcile_item")
        if result.transaction_type == "SETTLE":
            settled += 1
        elif result.transaction_type == "RELEASE":
            released += 1
    return DanglingBillingReconciliation(
        scanned=len(candidates),
        settled=settled,
        released=released,
        failed=failed,
    )
