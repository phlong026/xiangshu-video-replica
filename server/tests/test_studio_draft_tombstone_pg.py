"""T35 草稿墓碑围栏：真实 PostgreSQL 矩阵。

物理删除改成墓碑（清空 payload、revision +1）后：删除后带旧 revision 的
条件写必然错位 409；拒绝窗内的盲写（旧客户端 last-write-wins PUT）409
STUDIO_DRAFT_TOMBSTONED；知道墓碑版本号的「真正新稿」凭 revision 复活；
窗口过后盲写放行，未升级旧客户端仍能开新稿。重复放弃幂等返回当前版本。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-studio-draft-tombstone-tests-48-bytes-minimum",
)

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.studio_drafts import (
    StudioDraftUpsertRequest,
    delete_studio_draft,
    load_studio_draft,
    save_studio_draft,
)

TOMBSTONE_DB_NAME = "studio_draft_tombstone_test"


@pytest.fixture(scope="module")
def tombstone_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(TOMBSTONE_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(TOMBSTONE_DB_NAME)


@pytest.fixture()
def tombstone_db(tombstone_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, tombstone_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    # 默认拉满拒绝窗，需要窗口行为的用例自行覆盖该 env。
    monkeypatch.setenv("VIDEO_REPLICA_DRAFT_TOMBSTONE_WINDOW_SECONDS", "600")
    yield tombstone_dsn
    close_pg_pool()


_ACTOR = CurrentUser(id="user_1", username="user_1", display_name="User One", role="employee")


def _seed_user(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        # 036 的 append-only 守卫拒绝审计表被 TRUNCATE；本次清理挂起触发器。
        pg.execute("SET session_replication_role = replica")
        pg.execute("TRUNCATE studio_drafts, users CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            ("user_1", "user_1", "User One", "employee"),
        )


def _save(
    dsn: str,
    *,
    payload: dict[str, object],
    expected_revision: int | None = None,
) -> object:
    with pg_transaction() as raw:
        return save_studio_draft(
            BusinessConnection.postgres(raw),
            actor=_ACTOR,
            kind="copy",
            request=StudioDraftUpsertRequest(
                payload=payload, script_confirmed=False, expected_revision=expected_revision
            ),
        )


def _load(dsn: str) -> object:
    with pg_transaction() as raw:
        return load_studio_draft(BusinessConnection.postgres(raw), actor_id="user_1", kind="copy")


def _delete(dsn: str) -> int:
    with pg_transaction() as raw:
        return delete_studio_draft(BusinessConnection.postgres(raw), actor=_ACTOR, kind="copy")


def test_delete_creates_tombstone_and_blocks_stale_revision_write(
    tombstone_db: str,
) -> None:
    dsn = tombstone_db
    _seed_user(dsn)
    first = _save(dsn, payload={"text": "第一版"})
    assert first.revision == 1
    second = _save(dsn, payload={"text": "第二版"}, expected_revision=1)
    assert second.revision == 2

    # 放弃：返回墓碑版本号，行保留但内容清空、读取视为不存在。
    tombstone_revision = _delete(dsn)
    assert tombstone_revision == 3
    with pytest.raises(HTTPException) as missing:
        _load(dsn)
    assert missing.value.status_code == 404
    with psycopg.connect(dsn) as pg:
        row = pg.execute(
            "SELECT payload, deleted_at IS NOT NULL, revision FROM studio_drafts "
            "WHERE user_id='user_1' AND draft_kind='copy'"
        ).fetchone()
    assert row[0] == "{}"
    assert row[1] is True
    assert int(row[2]) == 3

    # 旧 revision 的迟到条件写：必然错位，且报墓碑而不是普通冲突。
    with pytest.raises(HTTPException) as stale:
        _save(dsn, payload={"text": "迟到旧稿"}, expected_revision=1)
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "STUDIO_DRAFT_TOMBSTONED"
    assert stale.value.detail["current_revision"] == 3

    # 拒绝窗内的盲写（旧客户端 last-write-wins）同样被墓碑挡住。
    with pytest.raises(HTTPException) as blind:
        _save(dsn, payload={"text": "盲写旧稿"})
    assert blind.value.status_code == 409
    assert blind.value.detail["code"] == "STUDIO_DRAFT_TOMBSTONED"

    # 知道墓碑版本号的「真正新稿」凭 revision 复活。
    revived = _save(dsn, payload={"text": "放弃后的新稿"}, expected_revision=3)
    assert revived.revision == 4
    loaded = _load(dsn)
    assert loaded.payload == {"text": "放弃后的新稿"}
    with psycopg.connect(dsn) as pg:
        deleted = pg.execute(
            "SELECT deleted_at IS NULL FROM studio_drafts "
            "WHERE user_id='user_1' AND draft_kind='copy'"
        ).fetchone()[0]
    assert deleted is True

    # 复活后旧版本写仍是普通冲突（活行语义）。
    with pytest.raises(HTTPException) as conflict:
        _save(dsn, payload={"text": "更旧的写"}, expected_revision=1)
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "STUDIO_DRAFT_CONFLICT"
    assert conflict.value.detail["current_revision"] == 4


def test_repeated_discard_is_idempotent(tombstone_db: str) -> None:
    dsn = tombstone_db
    _seed_user(dsn)
    _save(dsn, payload={"text": "只放弃一次"})
    first = _delete(dsn)
    second = _delete(dsn)
    assert first == second
    # 幂等放弃不推进版本号：版本号只由真实写动作推进。
    with psycopg.connect(dsn) as pg:
        revision = pg.execute(
            "SELECT revision FROM studio_drafts WHERE user_id='user_1' AND draft_kind='copy'"
        ).fetchone()[0]
    assert int(revision) == second


def test_blind_write_recovers_after_tombstone_window(tombstone_db: str) -> None:
    dsn = tombstone_db
    _seed_user(dsn)
    _save(dsn, payload={"text": "旧客户端的草稿"})
    _delete(dsn)
    # 窗口归零：拒绝窗外（含旧客户端场景）盲写可以开始真正的新稿。
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE studio_drafts SET deleted_at = %s, updated_at = %s "
            "WHERE user_id='user_1' AND draft_kind='copy'",
            ("2020-01-01 00:00:00.000000", "2020-01-01 00:00:00"),
        )
        conn.commit()
    revived = _save(dsn, payload={"text": "窗口后的新稿"})
    assert revived.revision >= 1
    loaded = _load(dsn)
    assert loaded.payload == {"text": "窗口后的新稿"}


def test_expected_revision_zero_still_conflicts_with_live_row(tombstone_db: str) -> None:
    dsn = tombstone_db
    _seed_user(dsn)
    _save(dsn, payload={"text": "已有一份"})
    with pytest.raises(HTTPException) as exc:
        _save(dsn, payload={"text": "别覆盖我"}, expected_revision=0)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STUDIO_DRAFT_CONFLICT"
    loaded = _load(dsn)
    assert loaded.payload == {"text": "已有一份"}
