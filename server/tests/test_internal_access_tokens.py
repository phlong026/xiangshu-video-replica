from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app


def run_accounts_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "app.internal_accounts",
            "--db-path",
            str(db_path),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def internal_account(tmp_path: Path) -> tuple[Path, str, str, str]:
    db_path = tmp_path / "internal-auth.db"
    with initialize_database(db_path):
        pass
    created = run_accounts_cli(
        db_path,
        "create-user",
        "--username",
        "operator_1",
        "--display-name",
        "Operator One",
    )
    user_id = str(json.loads(created.stdout)["user_id"])
    issued = run_accounts_cli(db_path, "issue-token", "--user-id", user_id)
    payload = json.loads(issued.stdout)
    return db_path, user_id, str(payload["token_id"]), str(payload["token"])


@pytest.fixture()
def internal_client(
    internal_account: tuple[Path, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    db_path, user_id, _, _ = internal_account

    def database_override() -> Iterator[sqlite3.Connection]:
        with connect_database(db_path) as conn:
            yield conn

    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "internal_token")
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", user_id)
    app.dependency_overrides[get_database] = database_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_create_user_cli_also_creates_empty_wallet(tmp_path: Path) -> None:
    db_path = tmp_path / "create-user.db"
    with initialize_database(db_path):
        pass

    result = run_accounts_cli(
        db_path,
        "create-user",
        "--username",
        "employee_1",
        "--display-name",
        "Employee One",
    )
    user_id = str(json.loads(result.stdout)["user_id"])

    with connect_database(db_path) as conn:
        user = conn.execute(
            "SELECT username, display_name, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        wallet = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    assert dict(user) == {
        "username": "employee_1",
        "display_name": "Employee One",
        "role": "employee",
    }
    assert dict(wallet) == {"available_credits": 0, "reserved_credits": 0}


def test_issue_token_prints_raw_value_once_and_stores_only_digest(tmp_path: Path) -> None:
    db_path = tmp_path / "issue-token.db"
    with initialize_database(db_path):
        pass
    created = run_accounts_cli(
        db_path,
        "create-user",
        "--username",
        "employee_1",
        "--display-name",
        "Employee One",
    )
    user_id = str(json.loads(created.stdout)["user_id"])

    issued = run_accounts_cli(db_path, "issue-token", "--user-id", user_id)
    payload = json.loads(issued.stdout)
    raw_token = str(payload["token"])

    assert issued.stdout.count(raw_token) == 1
    assert len(raw_token) >= 40
    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT token_digest, revoked_at FROM internal_access_tokens WHERE id = ?",
            (payload["token_id"],),
        ).fetchone()
        database_dump = "\n".join(conn.iterdump())
    assert row["token_digest"] == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert row["revoked_at"] is None
    assert raw_token not in database_dump


def test_internal_mode_accepts_only_valid_bearer_token(
    internal_client: TestClient,
    internal_account: tuple[Path, str, str, str],
) -> None:
    _, user_id, _, raw_token = internal_account

    auth_headers = {"Authorization": f"Bearer {raw_token}"}
    valid = internal_client.get("/api/auth/me", headers=auth_headers)
    business_api = internal_client.get("/api/projects", headers=auth_headers)
    missing = internal_client.get("/api/auth/me", headers={"X-Dev-User-Id": user_id})
    invalid = internal_client.get(
        "/api/auth/me",
        headers={
            "Authorization": "Bearer invalid-token",
            "X-Dev-User-Id": user_id,
        },
    )

    assert valid.status_code == 200
    assert valid.json()["id"] == user_id
    assert business_api.status_code == 200
    assert business_api.json() == []
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert invalid.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


def test_revoked_token_returns_401_immediately(
    internal_client: TestClient,
    internal_account: tuple[Path, str, str, str],
) -> None:
    db_path, _, token_id, raw_token = internal_account
    before = internal_client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})

    revoked = run_accounts_cli(db_path, "revoke-token", "--token-id", token_id)
    after = internal_client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})

    assert before.status_code == 200
    assert json.loads(revoked.stdout) == {"revoked": True, "token_id": token_id}
    assert after.status_code == 401
    assert after.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"
