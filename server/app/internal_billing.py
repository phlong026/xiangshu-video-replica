from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

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


def reserve_internal_billing(
    conn: sqlite3.Connection,
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
        WHERE task.id = ?
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
        WHERE task_id = ? AND type = 'RESERVE'
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
                WHERE task_id = ? AND billing_round = ?
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
        WHERE task_id = ? AND billing_round = ? AND type = 'RESERVE'
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
            WHERE task_id = ? AND billing_round = ?
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
        WHERE user_id = ? AND available_credits >= 1
        """,
        (user_id,),
    )
    if cursor.rowcount != 1:
        wallet = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = ?",
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
        ) VALUES (?, ?, 'RESERVE', -1, 1, ?, ?, ?)
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
    conn: sqlite3.Connection,
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
        WHERE task.id = ?
        """,
        (task_id,),
    ).fetchone()
    if task is None:
        raise BillingInvariantError("generation task does not exist")

    reservation = conn.execute(
        """
        SELECT user_id, billing_round
        FROM wallet_transactions
        WHERE task_id = ? AND type = 'RESERVE'
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
        WHERE task_id = ? AND billing_round = ?
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
            available_credits = available_credits + ?,
            reserved_credits = reserved_credits - 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND reserved_credits >= 1
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
        ) VALUES (?, ?, ?, ?, -1, ?, ?, ?)
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
