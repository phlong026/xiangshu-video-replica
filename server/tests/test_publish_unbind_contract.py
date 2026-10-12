"""账号解绑的事务内状态契约；真实行锁另由 test_publish_records 的 PG 用例验证。"""

from typing import Any

import pytest
from fastapi import HTTPException

from app.publish_browser import delete_browser_account


class Connection:
    def __init__(self) -> None:
        self.account = {"id": "account", "owner": "owner"}
        self.records = [
            {"status": "queued", "account_id": "account"},
            {"status": "publishing", "account_id": "account"},
            {"status": "published", "account_id": "account"},
        ]
        self.row: dict[str, str] | None = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[str, ...]) -> Any:
        self.row = None
        self.rowcount = 0
        owned = self.account and params == (self.account["id"], self.account["owner"])
        if sql.startswith("SELECT"):
            self.row = self.account if owned else None
        elif "UPDATE publish_records" in sql and owned:
            for record in self.records:
                if record["status"] == "queued":
                    record["status"] = "cancelled"
        elif sql.startswith("DELETE"):
            if owned:
                self.account = {}
                self.rowcount = 1
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self) -> dict[str, str] | None:
        return self.row


def test_queued_stops_and_inflight_boundary_stays_truthful() -> None:
    conn = Connection()
    delete_browser_account(conn, "owner", "account")  # type: ignore[arg-type]
    assert not conn.account
    assert [record["status"] for record in conn.records] == ["cancelled", "publishing", "published"]
    with pytest.raises(HTTPException) as absent:
        delete_browser_account(conn, "owner", "account")  # type: ignore[arg-type]
    assert absent.value.status_code == 404


def test_unbind_never_removes_another_owners_account() -> None:
    conn = Connection()
    try:
        delete_browser_account(conn, "another-owner", "account")  # type: ignore[arg-type]
    except HTTPException as exc:
        assert exc.status_code == 404
    assert conn.account
    assert conn.records[0]["status"] == "queued"
