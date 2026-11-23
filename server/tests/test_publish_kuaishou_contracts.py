"""快手发布的平台、持久化、权限与可逆迁移合同；平台 I/O 不参与测试。

复用已登记的 publish_records_test 隔离库及生产 PG 事务夹具。真实数据库承载
CHECK、加密账号、发布状态与升级/降级；扫码只在领域层保存合成身份，不访问平台。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_cw056_supported_head_matrix import _schema_inventory
from test_publish_accounts import _TEST_KEY, _PgBusinessDb, actor
from test_publish_records import (
    _clear_dependency_overrides,
    client,
    create_payload,
    cw068_dsn,
    holder,
    lane_env,
    open_txn,
    pg,
    record_row,
    seed_account,
    seed_asset,
    seed_record,
)

from app import publish_records
from app.publish_browser import (
    BrowserAccount,
    BrowserAccountImportRequest,
    BrowserLoginRequest,
    BrowserProbeLease,
    Platform,
    claim_browser_probe_work,
    defer_browser_probe_work,
    save_login,
    start_login,
)
from app.publish_records import (
    DELIVERABLE_PLATFORMS,
    PLATFORMS,
    PublishRecordCreateRequest,
    claim_publish_work,
    finalize_publish_work,
    mark_kuaishou_record_submitted,
)
from app.publishers.base import PublishResult

__all__ = ["_clear_dependency_overrides", "client", "cw068_dsn", "holder", "lane_env", "pg"]

BROWSER = "/api/studio/publish/browser"
RECORDS = "/api/studio/publish/records"
HEAD = "20261124T1000_recharge_minimum_step"
PREVIOUS_HEAD = "20261116T1000_recharge_orders_closed_at"
_TABLES = ("publish_browser_accounts", "publish_browser_logins", "publish_records")
_OLD_PLATFORMS = {"douyin", "wechat_channels", "xiaohongshu"}
_STORAGE = {
    "cookies": [
        {
            "name": "kuaishou.server.web_st",
            "value": "synthetic-kuaishou-session",
            "domain": ".kuaishou.com",
            "path": "/",
        }
    ],
    "origins": [],
}


def _import_payload() -> dict[str, Any]:
    return {
        "platform": "kuaishou",
        "identity": {"platform_user_id": "synthetic-ks-uid", "username": "快手发布账号"},
        "storage_state": _STORAGE,
    }


def _confirmed_payload(account_id: str, video_id: str, **extra: Any) -> dict[str, Any]:
    return create_payload(account_id, video_id, **({"publish_confirmed": True} | extra))


def _error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code, response.text


def _config(dsn: str) -> Config:
    server = Path(__file__).resolve().parents[1]
    config = Config(str(server / "alembic.ini"))
    config.set_main_option("script_location", str(server / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    return config


def _assert_platform_checks(pg: psycopg.Connection, expected: set[str]) -> None:
    rows = pg.execute(
        "SELECT t.relname, c.conname, pg_get_constraintdef(c.oid), c.convalidated "
        "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = 'public' AND t.relname = ANY(%s) "
        "AND c.conname = t.relname || '_platform_check' ORDER BY t.relname",
        (list(_TABLES),),
    ).fetchall()
    assert [row[0] for row in rows] == sorted(_TABLES)
    for table, name, definition, validated in rows:
        assert name == f"{table}_platform_check"
        assert validated is True
        assert set(re.findall(r"'([^']+)'::text", definition)) == expected


def test_kuaishou_request_response_models_and_publish_scope() -> None:
    assert BrowserLoginRequest(platform="kuaishou").platform == "kuaishou"
    with pytest.raises(ValidationError):
        BrowserAccountImportRequest.model_validate(_import_payload())
    account = BrowserAccount(
        id="account", platform="kuaishou", platform_user_id="uid", username="快手", verified_at=1
    )
    assert account.model_dump()["platform"] == "kuaishou"
    assert not {"storage_state", "storage_state_enc", "cookie"} & account.model_dump().keys()
    assert set(get_args(Platform)) == set(PLATFORMS) == _OLD_PLATFORMS | {"kuaishou"}
    assert DELIVERABLE_PLATFORMS == {"douyin", "wechat_channels", "kuaishou"}
    # 旧构造调用继续有效，新领取的租约额外携带待核对的真实平台 UID。
    assert BrowserProbeLease("account", "kuaishou", "enc", "lease", 1).platform_user_id is None


@pytest.mark.parametrize("platform", ["unknown", "tiktok", "Kuaishou", ""])
def test_unknown_platform_is_rejected_by_login_and_import_models(platform: str) -> None:
    with pytest.raises(ValidationError):
        BrowserLoginRequest.model_validate({"platform": platform})
    with pytest.raises(ValidationError):
        BrowserAccountImportRequest.model_validate({**_import_payload(), "platform": platform})


def test_record_platform_is_derived_from_owned_account_not_request() -> None:
    with pytest.raises(ValidationError):
        PublishRecordCreateRequest.model_validate(
            {"account_id": "account", "video_material_id": "asset:video", "platform": "kuaishou"}
        )


@pytest.mark.parametrize("value", ["true", 1, None])
def test_confirmation_requires_a_json_boolean(value: Any) -> None:
    with pytest.raises(ValidationError):
        PublishRecordCreateRequest.model_validate(
            _confirmed_payload("account", "video", publish_confirmed=value)
        )


@pytest.mark.parametrize(
    ("source", "overrides", "expected_code"),
    [
        ("desktop", {}, "PUBLISH_CLOUD_LOGIN_REQUIRED"),
        ("cloud", {"publish_confirmed": False}, "PUBLISH_CONFIRMATION_REQUIRED"),
        ("cloud", {"scheduled_at": "2099-01-01T00:00:00Z"}, "PUBLISH_IMMEDIATE_ONLY"),
        ("cloud", {"options": {"visibility": 0}}, "PUBLISH_IMMEDIATE_ONLY"),
        ("cloud", {"title": "  "}, "PUBLISH_TITLE_REQUIRED"),
    ],
)
def test_invalid_kuaishou_request_stops_before_creating_work(
    monkeypatch: pytest.MonkeyPatch, source: str, overrides: dict[str, Any], expected_code: str
) -> None:
    def account_lookup(sql: str, _params: tuple[str, ...]) -> SimpleNamespace:
        assert "FROM publish_browser_accounts" in sql
        return SimpleNamespace(
            fetchone=lambda: {"platform": "kuaishou", "status": "connected", "source": source}
        )

    monkeypatch.setattr(publish_records, "require_not_auditor", lambda *a, **kw: None)
    request = PublishRecordCreateRequest.model_validate(
        _confirmed_payload("account", "video", **overrides)
    )
    with pytest.raises(HTTPException) as rejected:
        publish_records.create_record(
            SimpleNamespace(execute=account_lookup), actor=actor("employee_1"), request=request
        )
    assert rejected.value.status_code == 422 and rejected.value.detail["code"] == expected_code


@pytest.mark.pg
def test_kuaishou_cannot_be_imported_from_desktop(
    client: TestClient, pg: psycopg.Connection
) -> None:
    rejected = client.post(BROWSER + "/accounts/import", json=_import_payload())
    assert rejected.status_code == 422
    assert "synthetic-kuaishou-session" not in rejected.text
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 0


@pytest.mark.pg
def test_kuaishou_relogin_rejects_wrong_platform_owner_and_identity(
    client: TestClient, pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg, platform="kuaishou")
    foreign = seed_account(pg, platform="kuaishou", user_id="employee_2")
    uid = pg.execute(
        "SELECT platform_user_id FROM publish_browser_accounts WHERE id=%s", (account,)
    ).fetchone()[0]
    for platform, account_id in [("douyin", account), ("kuaishou", foreign)]:
        rejected = client.post(
            BROWSER + "/logins", json={"platform": platform, "account_id": account_id}
        )
        assert rejected.status_code == 404, rejected.text
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0

    with open_txn() as conn:
        login = start_login(
            conn, "employee_1", BrowserLoginRequest(platform="kuaishou", account_id=account)
        )
    assert pg.execute(
        "SELECT platform FROM publish_browser_logins WHERE id=%s", (login,)
    ).fetchone() == ("kuaishou",)
    fernet = Fernet(_TEST_KEY.encode())
    for owner, identity, status in [
        ("employee_2", uid, 409),
        ("employee_1", "another-platform-user", 409),
    ]:
        with pytest.raises(HTTPException) as rejected, open_txn() as conn:
            save_login(
                conn,
                owner,
                login,
                {"platform_user_id": identity, "username": "错误身份"},
                _STORAGE,
                fernet,
            )
        assert rejected.value.status_code == status
    with open_txn() as conn:
        refreshed = save_login(
            conn,
            "employee_1",
            login,
            {"platform_user_id": uid, "username": "正确身份"},
            _STORAGE,
            fernet,
        )
    assert refreshed.id == account and refreshed.platform == "kuaishou"
    assert refreshed.username == "正确身份" and refreshed.source == "cloud"
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0


@pytest.mark.pg
def test_kuaishou_record_persists_filters_cancels_and_deletes(
    client: TestClient, pg: psycopg.Connection, holder: _PgBusinessDb
) -> None:
    account = seed_account(pg, platform="kuaishou")
    video = seed_asset(pg)
    cover = seed_asset(pg, kind="material_image", content_type="image/png")
    response = client.post(
        RECORDS,
        json=_confirmed_payload(account, video, cover_material_id=f"asset:{cover}"),
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["platform"] == "kuaishou" and record["status"] == "queued"
    assert record["cover_asset_id"] == cover and record["scheduled_at"] is None
    assert "storage_state_enc" not in record and "synthetic-session" not in response.text
    assert record_row(pg, record["id"], "platform, status, account_id, video_asset_id") == (
        "kuaishou",
        "queued",
        account,
        video,
    )
    _error(
        client.post(RECORDS, json=_confirmed_payload(account, video)),
        409,
        "PUBLISH_RECORD_DUPLICATE",
    )
    other = seed_account(pg, platform="douyin")
    seed_record(pg, account_id=other, video_id=seed_asset(pg))
    listed = client.get(RECORDS, params={"platform": "kuaishou"})
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["records"]] == [record["id"]]
    _error(client.get(RECORDS, params={"platform": "unknown"}), 422, "PUBLISH_PLATFORM_UNSUPPORTED")

    holder.current_actor = actor("employee_2")
    assert client.get(RECORDS, params={"platform": "kuaishou"}).json()["records"] == []
    for method, suffix in [("get", ""), ("post", "/cancel"), ("post", "/retry"), ("delete", "")]:
        _error(
            getattr(client, method)(f"{RECORDS}/{record['id']}{suffix}"),
            404,
            "PUBLISH_RECORD_NOT_FOUND",
        )
    holder.current_actor = actor("employee_1")
    cancelled = client.post(f"{RECORDS}/{record['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["record"]["status"] == "cancelled"
    assert record_row(pg, record["id"], "status") == ("cancelled",)
    assert client.delete(f"{RECORDS}/{record['id']}").json() == {"deleted": True}
    assert client.get(f"{RECORDS}/{record['id']}").status_code == 404


@pytest.mark.pg
def test_kuaishou_creation_enforces_account_and_material_ownership(
    client: TestClient, pg: psycopg.Connection
) -> None:
    video = seed_asset(pg)
    foreign = seed_account(pg, platform="kuaishou", user_id="employee_2")
    invalid = seed_account(pg, platform="kuaishou", status="invalid")
    for account, status, code in [
        (foreign, 404, "PUBLISH_ACCOUNT_NOT_FOUND"),
        ("missing", 404, "PUBLISH_ACCOUNT_NOT_FOUND"),
        (invalid, 409, "PUBLISH_ACCOUNT_INVALID"),
    ]:
        for title in ["合法标题", ""]:
            _error(
                client.post(RECORDS, json=create_payload(account, video, title=title)), status, code
            )
    own = seed_account(pg, platform="kuaishou")
    foreign_video = seed_asset(pg, user_id="employee_2")
    _error(
        client.post(RECORDS, json=_confirmed_payload(own, foreign_video)), 404, "MATERIAL_NOT_FOUND"
    )
    assert (
        client.post(RECORDS, json=create_payload(own, video, platform="douyin")).status_code == 422
    )
    assert pg.execute("SELECT count(*) FROM publish_records").fetchone()[0] == 0


@pytest.mark.pg
@pytest.mark.parametrize(
    ("platform", "title", "expected_status"),
    [
        ("kuaishou", None, 422),
        ("kuaishou", "", 422),
        ("kuaishou", " \t\r\n\u3000 ", 422),
        ("kuaishou", "  快手合法标题  ", 200),
        ("douyin", "", 200),
        ("douyin", " \t\n ", 200),
        ("wechat_channels", "", 200),
        ("wechat_channels", " \t\n ", 200),
    ],
)
def test_kuaishou_requires_nonempty_title_without_changing_other_platforms(
    client: TestClient,
    pg: psycopg.Connection,
    platform: str,
    title: str | None,
    expected_status: int,
) -> None:
    account = seed_account(pg, platform=platform)
    video = seed_asset(pg)
    payload = (_confirmed_payload if platform == "kuaishou" else create_payload)(account, video)
    if title is None:
        payload.pop("title")
    else:
        payload["title"] = title
    response = client.post(RECORDS, json=payload)
    if expected_status == 422:
        _error(response, 422, "PUBLISH_TITLE_REQUIRED")
        assert response.json()["detail"]["message"] == "请填写快手发布标题。"
        assert pg.execute("SELECT count(*) FROM publish_records").fetchone()[0] == 0
    else:
        assert response.status_code == expected_status, response.text
        record = response.json()
        assert record["title"] == (title or "").strip()
        assert record_row(pg, record["id"], "platform, title, status") == (
            platform,
            (title or "").strip(),
            "queued",
        )


@pytest.mark.pg
@pytest.mark.parametrize("platform", ["kuaishou", "douyin", "wechat_channels"])
def test_kuaishou_only_rejects_more_than_three_normalized_tags(
    client: TestClient, pg: psycopg.Connection, platform: str
) -> None:
    account = seed_account(pg, platform=platform)
    video = seed_asset(pg)
    four = ["建房", "预算", "设计", "施工"]
    payload = (_confirmed_payload if platform == "kuaishou" else create_payload)(
        account, video, tags=four
    )
    response = client.post(RECORDS, json=payload)
    if platform == "kuaishou":
        _error(response, 422, "PUBLISH_TAGS_TOO_MANY")
        assert pg.execute("SELECT count(*) FROM publish_records").fetchone()[0] == 0
        response = client.post(
            RECORDS,
            json=_confirmed_payload(account, video, tags=["#建房", "预算", "设计", "建房", " "]),
        )
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == four[:3]
    else:
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == four


@pytest.mark.pg
def test_kuaishou_rejects_unconfirmed_scheduled_or_noncloud_publication(
    client: TestClient, pg: psycopg.Connection
) -> None:
    account, video = seed_account(pg, platform="kuaishou"), seed_asset(pg)
    for payload, code in [
        (create_payload(account, video), "PUBLISH_CONFIRMATION_REQUIRED"),
        (
            _confirmed_payload(account, video, publish_confirmed=False),
            "PUBLISH_CONFIRMATION_REQUIRED",
        ),
        (
            _confirmed_payload(account, video, scheduled_at="2099-01-01T00:00:00Z"),
            "PUBLISH_IMMEDIATE_ONLY",
        ),
        (_confirmed_payload(account, video, options={"visibility": 0}), "PUBLISH_IMMEDIATE_ONLY"),
    ]:
        _error(client.post(RECORDS, json=payload), 422, code)
    pg.execute("UPDATE publish_browser_accounts SET source='desktop' WHERE id=%s", (account,))
    _error(
        client.post(RECORDS, json=_confirmed_payload(account, video)),
        422,
        "PUBLISH_CLOUD_LOGIN_REQUIRED",
    )
    assert pg.execute("SELECT count(*) FROM publish_records").fetchone()[0] == 0


@pytest.mark.pg
def test_kuaishou_pre_submit_failure_is_not_automatically_or_manually_requeued(
    client: TestClient, pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg, platform="kuaishou")
    video = seed_asset(pg)
    record = seed_record(pg, account_id=account, video_id=video)
    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id="mock-worker")
    assert work is not None and work.record_id == record
    with open_txn() as conn:
        result = finalize_publish_work(conn, work=work, result=PublishResult("kuaishou", "failed"))
    assert result == "failed"
    assert record_row(pg, record, "status, attempt_count, submitted_at") == ("failed", 1, None)
    _error(client.post(f"{RECORDS}/{record}/retry"), 409, "PUBLISH_CONFIRMATION_REQUIRED")
    assert client.post(RECORDS, json=_confirmed_payload(account, video)).status_code == 200


@pytest.mark.pg
def test_kuaishou_unknown_submission_cannot_retry_cancel_delete_or_be_reclaimed(
    client: TestClient, pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg, platform="kuaishou")
    video = seed_asset(pg)
    record = seed_record(pg, account_id=account, video_id=video)
    with open_txn() as conn:
        work = claim_publish_work(conn, worker_id="kuaishou-contract")
    assert work is not None and work.record_id == record
    assert work.row["platform"] == "kuaishou" and work.row["platform_user_id"]
    with open_txn() as conn:
        assert mark_kuaishou_record_submitted(conn, work=work)
        assert not mark_kuaishou_record_submitted(conn, work=work)
    with open_txn() as conn:
        assert (
            finalize_publish_work(
                conn, work=work, result=PublishResult(platform="kuaishou", status="unknown")
            )
            == "submission_unknown"
        )
    before = record_row(pg, record, "status, submission_key, submitted_at, attempt_count")
    assert before[0] == "submission_unknown" and before[1] and before[2] is not None
    for suffix in ["retry", "cancel"]:
        _error(client.post(f"{RECORDS}/{record}/{suffix}"), 409, "PUBLISH_RECORD_STATE_CONFLICT")
    _error(client.delete(f"{RECORDS}/{record}"), 409, "PUBLISH_RECORD_STATE_CONFLICT")
    with open_txn() as conn:
        assert claim_publish_work(conn, worker_id="must-not-retry") is None
    assert record_row(pg, record, "status, submission_key, submitted_at, attempt_count") == before
    assert client.get(RECORDS + "/summary").json()["unknown_total"] == 1
    _error(
        client.post(RECORDS, json=_confirmed_payload(account, video)),
        409,
        "PUBLISH_SUBMISSION_UNKNOWN",
    )
    assert client.delete(BROWSER + "/accounts/" + account).status_code == 200
    new_binding = seed_account(pg, platform="kuaishou")
    _error(
        client.post(RECORDS, json=_confirmed_payload(new_binding, video)),
        409,
        "PUBLISH_SUBMISSION_UNKNOWN",
    )


@pytest.mark.pg
def test_kuaishou_unbind_cancels_only_queue_and_keeps_submission_history(
    client: TestClient, pg: psycopg.Connection
) -> None:
    account = seed_account(pg, platform="kuaishou")
    records = {
        status: seed_record(pg, account_id=account, video_id=seed_asset(pg), status=status)
        for status in ["queued", "publishing", "submission_unknown", "published"]
    }
    response = client.delete(BROWSER + "/accounts/" + account)
    assert response.status_code == 200, response.text
    for status, record in records.items():
        assert record_row(pg, record, "platform, status, account_id") == (
            "kuaishou",
            "cancelled" if status == "queued" else status,
            None,
        )
    _error(
        client.post(f"{RECORDS}/{records['submission_unknown']}/retry"),
        409,
        "PUBLISH_RECORD_STATE_CONFLICT",
    )


@pytest.mark.pg
def test_kuaishou_auditor_cannot_bind_create_or_change_records(
    client: TestClient, pg: psycopg.Connection, holder: _PgBusinessDb
) -> None:
    account = seed_account(pg, platform="kuaishou", user_id="auditor_1")
    video = seed_asset(pg, user_id="auditor_1")
    queued = seed_record(pg, account_id=account, video_id=video, user_id="auditor_1")
    failed = seed_record(
        pg, account_id=account, video_id=video, status="failed", user_id="auditor_1"
    )
    holder.current_actor = actor("auditor_1", role="auditor")
    for response in [
        client.post(BROWSER + "/logins", json={"platform": "kuaishou"}),
        client.post(RECORDS, json=create_payload(account, video)),
        client.post(f"{RECORDS}/{queued}/cancel"),
        client.post(f"{RECORDS}/{failed}/retry"),
        client.delete(f"{RECORDS}/{failed}"),
    ]:
        assert response.status_code == 403, response.text
    assert record_row(pg, queued, "status") == ("queued",)
    assert record_row(pg, failed, "status") == ("failed",)
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0


@pytest.mark.pg
def test_kuaishou_subaccount_publish_switch_gates_login(
    client: TestClient, pg: psycopg.Connection
) -> None:
    pg.execute(
        "UPDATE users SET parent_user_id='employee_2', account_type='SUB' WHERE id='employee_1'"
    )
    pg.execute(
        "INSERT INTO sub_account_permissions(user_id, allowed_businesses, allow_publish_accounts) "
        "VALUES ('employee_1', '[]', false)"
    )
    for response in [
        client.post(BROWSER + "/logins", json={"platform": "kuaishou"}),
    ]:
        _error(response, 403, "SUB_ACCOUNT_PUBLISH_ACCOUNTS_DISABLED")
    assert pg.execute("SELECT count(*) FROM publish_browser_accounts").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM publish_browser_logins").fetchone()[0] == 0


@pytest.mark.pg
def test_kuaishou_health_probe_lease_carries_expected_platform_user_id(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg, platform="kuaishou")
    pg.execute(
        "UPDATE publish_browser_accounts SET next_probe_at=clock_timestamp()-interval '1 hour' "
        "WHERE id=%s",
        (account,),
    )
    uid = pg.execute(
        "SELECT platform_user_id FROM publish_browser_accounts WHERE id=%s", (account,)
    ).fetchone()[0]
    with open_txn() as conn:
        lease = claim_browser_probe_work(conn, worker_id="kuaishou-probe-contract")
    assert lease is not None and lease.record_id == account
    assert lease.platform == "kuaishou" and lease.platform_user_id == uid


@pytest.mark.pg
def test_deferred_slow_kuaishou_probes_yield_to_other_due_platforms(
    pg: psycopg.Connection, lane_env: str
) -> None:
    moment = datetime.now(UTC).replace(microsecond=0)
    slow_accounts = [seed_account(pg, platform="kuaishou") for _ in range(5)]
    for index, account in enumerate(slow_accounts):
        pg.execute(
            "UPDATE publish_browser_accounts SET next_probe_at=%s WHERE id=%s",
            (moment - timedelta(hours=6 - index), account),
        )
    other_accounts = {
        seed_account(pg, platform=platform) for platform in ["douyin", "wechat_channels"]
    }
    for account in other_accounts:
        pg.execute(
            "UPDATE publish_browser_accounts SET next_probe_at=%s WHERE id=%s",
            (moment - timedelta(minutes=1), account),
        )
    # 五个未知探测各耗时一分钟：首个原租约到期后仍应由其它到期平台先领取。
    # 用领域时钟推进而不 sleep；否则回归测试本身要等待五分钟。
    for account in slow_accounts:
        with open_txn() as conn:
            lease = claim_browser_probe_work(conn, worker_id="slow-probe", now=moment)
        assert lease is not None and lease.record_id == account
        moment += timedelta(seconds=60)
        with open_txn() as conn:
            assert defer_browser_probe_work(conn, lease=lease, now=moment)
        assert pg.execute(
            "SELECT next_probe_at, probe_lease_owner, probe_lease_expires_at "
            "FROM publish_browser_accounts WHERE id=%s",
            (account,),
        ).fetchone() == (moment + timedelta(seconds=300), None, None)
    claimed = set()
    for _ in other_accounts:
        with open_txn() as conn:
            lease = claim_browser_probe_work(conn, worker_id="other-platform", now=moment)
        assert lease is not None and lease.platform in {"douyin", "wechat_channels"}
        claimed.add(lease.record_id)
    assert claimed == other_accounts


@pytest.mark.pg
@pytest.mark.parametrize("status", ["connected", "invalid"])
def test_deferred_probe_preserves_identity_health_and_existing_error(
    pg: psycopg.Connection, lane_env: str, status: str
) -> None:
    moment = datetime.now(UTC).replace(microsecond=0)
    account = seed_account(pg, platform="kuaishou")
    pg.execute(
        "UPDATE publish_browser_accounts SET next_probe_at=%s WHERE id=%s",
        (moment - timedelta(hours=1), account),
    )
    with open_txn() as conn:
        lease = claim_browser_probe_work(conn, worker_id="defer-contract", now=moment)
    assert lease is not None
    pg.execute(
        "UPDATE publish_browser_accounts SET status=%s, verified_at=%s, error_message=%s "
        "WHERE id=%s",
        (status, moment - timedelta(days=2), "保留既有提示", account),
    )
    before = pg.execute(
        "SELECT platform, platform_user_id, username, storage_state_enc, status, verified_at, "
        "error_message, source, avatar_url, probe_attempt_count "
        "FROM publish_browser_accounts WHERE id=%s",
        (account,),
    ).fetchone()
    later = moment + timedelta(seconds=60)
    with open_txn() as conn:
        assert defer_browser_probe_work(conn, lease=lease, now=later)
    assert (
        pg.execute(
            "SELECT platform, platform_user_id, username, storage_state_enc, status, verified_at, "
            "error_message, source, avatar_url, probe_attempt_count "
            "FROM publish_browser_accounts WHERE id=%s",
            (account,),
        ).fetchone()
        == before
    )
    assert pg.execute(
        "SELECT next_probe_at, probe_lease_owner, probe_lease_expires_at "
        "FROM publish_browser_accounts WHERE id=%s",
        (account,),
    ).fetchone() == (later + timedelta(seconds=300), None, None)
    # 已清掉的租约不能再次延后，也不能把下一次探测不断推远。
    snapshot = pg.execute("SELECT * FROM publish_browser_accounts ORDER BY id").fetchall()
    with open_txn() as conn:
        assert not defer_browser_probe_work(conn, lease=lease, now=later + timedelta(seconds=1))
    assert pg.execute("SELECT * FROM publish_browser_accounts ORDER BY id").fetchall() == snapshot


@pytest.mark.pg
@pytest.mark.parametrize(
    "scenario",
    ["wrong_token", "wrong_attempt", "expired", "expiry_boundary", "reclaimed", "deleted"],
)
def test_deferred_probe_rejects_stale_expired_or_deleted_lease_without_any_write(
    pg: psycopg.Connection, lane_env: str, scenario: str
) -> None:
    moment = datetime.now(UTC).replace(microsecond=0)
    account = seed_account(pg, platform="kuaishou")
    pg.execute(
        "UPDATE publish_browser_accounts SET next_probe_at=%s WHERE id=%s",
        (moment - timedelta(hours=1), account),
    )
    with open_txn() as conn:
        lease = claim_browser_probe_work(conn, worker_id="original-probe", now=moment)
    assert lease is not None
    later = moment + timedelta(seconds=60)
    if scenario == "wrong_token":
        lease = replace(lease, lease_token="different-probe-token")
    elif scenario == "wrong_attempt":
        lease = replace(lease, attempt_count=lease.attempt_count + 1)
    elif scenario == "expired":
        later = moment + timedelta(seconds=301)
    elif scenario == "expiry_boundary":
        later = moment + timedelta(seconds=300)
    elif scenario == "reclaimed":
        later = moment + timedelta(seconds=301)
        with open_txn() as conn:
            replacement = claim_browser_probe_work(conn, worker_id="replacement-probe", now=later)
        assert replacement is not None and replacement.record_id == account
        assert replacement.lease_token != lease.lease_token
        assert replacement.attempt_count == lease.attempt_count + 1
    elif scenario == "deleted":
        pg.execute("DELETE FROM publish_browser_accounts WHERE id=%s", (account,))
    snapshot = pg.execute("SELECT * FROM publish_browser_accounts ORDER BY id").fetchall()
    with open_txn() as conn:
        assert not defer_browser_probe_work(conn, lease=lease, now=later)
    assert pg.execute("SELECT * FROM publish_browser_accounts ORDER BY id").fetchall() == snapshot


@pytest.mark.pg
def test_kuaishou_platform_migration_roundtrip_is_strict_and_preserves_legacy_facts(
    pg: psycopg.Connection, lane_env: str
) -> None:
    account = seed_account(pg)
    record = seed_record(pg, account_id=account, video_id=seed_asset(pg))
    login = str(uuid4())
    pg.execute(
        "INSERT INTO publish_browser_logins(id,user_id,platform,account_id,expires_at) "
        "VALUES (%s,'employee_1','douyin',%s,clock_timestamp()+interval '5 minutes')",
        (login, account),
    )
    ids = {
        "publish_browser_accounts": account,
        "publish_browser_logins": login,
        "publish_records": record,
    }
    before = {table: pg.execute(f"SELECT * FROM {table}").fetchall() for table in _TABLES}
    inventory = _schema_inventory(pg)
    config = _config(lane_env)
    try:
        command.downgrade(config, PREVIOUS_HEAD)
        assert pg.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        _assert_platform_checks(pg, _OLD_PLATFORMS)
        for table in _TABLES:
            for platform in ["kuaishou", "unknown"]:
                with pytest.raises(psycopg.errors.CheckViolation):
                    pg.execute(
                        f"UPDATE {table} SET platform=%s WHERE id=%s", (platform, ids[table])
                    )
        command.upgrade(config, HEAD)
        assert pg.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        _assert_platform_checks(pg, _OLD_PLATFORMS | {"kuaishou"})
        assert _schema_inventory(pg) == inventory
        assert {
            table: pg.execute(f"SELECT * FROM {table}").fetchall() for table in _TABLES
        } == before
        for table in _TABLES:
            for platform in ["douyin", "wechat_channels", "xiaohongshu", "kuaishou"]:
                pg.execute(f"UPDATE {table} SET platform=%s WHERE id=%s", (platform, ids[table]))
            with pytest.raises(psycopg.errors.CheckViolation):
                pg.execute(f"UPDATE {table} SET platform='unknown' WHERE id=%s", (ids[table],))
    finally:
        command.upgrade(config, HEAD)


@pytest.mark.pg
@pytest.mark.parametrize("table", _TABLES)
def test_kuaishou_downgrade_refuses_any_retained_platform_fact_atomically(
    pg: psycopg.Connection, lane_env: str, table: str
) -> None:
    if table == "publish_browser_accounts":
        row_id = seed_account(pg, platform="kuaishou")
    elif table == "publish_browser_logins":
        row_id = str(uuid4())
        pg.execute(
            "INSERT INTO publish_browser_logins(id,user_id,platform,expires_at) "
            "VALUES (%s,'employee_1','kuaishou',clock_timestamp()+interval '5 minutes')",
            (row_id,),
        )
    else:
        account = seed_account(pg, platform="kuaishou")
        row_id = seed_record(pg, account_id=account, video_id=seed_asset(pg), status="published")
        pg.execute("DELETE FROM publish_browser_accounts WHERE id=%s", (account,))
        assert record_row(pg, row_id, "account_id") == (None,)
    config = _config(lane_env)
    try:
        with pytest.raises(RuntimeError, match=table + ".*快手"):
            command.downgrade(config, PREVIOUS_HEAD)
        assert pg.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        _assert_platform_checks(pg, _OLD_PLATFORMS | {"kuaishou"})
        assert pg.execute(f"SELECT platform FROM {table} WHERE id=%s", (row_id,)).fetchone() == (
            "kuaishou",
        )
    finally:
        command.upgrade(config, HEAD)
