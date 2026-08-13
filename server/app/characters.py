from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser

MAIN_CHARACTER_VERSION_KIND = "main_character"


@dataclass(frozen=True)
class CharacterData:
    id: str
    name: str
    reference_asset_ids: list[str]
    authorization_project_ids: list[str]
    authorization_expires_at: str | None
    is_active: bool
    created_by_user_id: str | None
    created_at: str
    updated_at: str


def list_characters(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    project_id: str | None,
) -> list[CharacterData]:
    rows = conn.execute(
        """
        SELECT
            id,
            name,
            reference_asset_ids_json,
            authorization_project_ids_json,
            authorization_expires_at,
            is_active,
            created_by_user_id,
            created_at,
            updated_at
        FROM characters
        ORDER BY created_at, id
        """
    ).fetchall()
    characters = [character_from_row(row) for row in rows]
    if actor.role == "employee":
        return [
            character
            for character in characters
            if character_is_available(character, project_id=project_id)
        ]
    return characters


def get_character(
    conn: sqlite3.Connection,
    *,
    character_id: str,
    actor: CurrentUser,
    project_id: str | None,
) -> CharacterData:
    character = read_character(conn, character_id)
    if actor.role == "employee" and not character_is_available(character, project_id=project_id):
        raise character_not_available()
    return character


def create_character(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    name: str,
    reference_asset_ids: list[str],
    authorization_project_ids: list[str],
    authorization_expires_at: datetime | None,
    is_active: bool,
) -> CharacterData:
    clean_name = normalize_name(name)
    clean_reference_asset_ids = normalize_ids(reference_asset_ids)
    clean_authorization_project_ids = normalize_ids(authorization_project_ids)
    ensure_assets_exist(conn, clean_reference_asset_ids)
    ensure_projects_exist(conn, clean_authorization_project_ids)

    character_id = str(uuid4())
    expires_at = encode_datetime(authorization_expires_at)
    with conn:
        conn.execute(
            """
            INSERT INTO characters (
                id,
                name,
                reference_asset_ids_json,
                authorization_project_ids_json,
                authorization_expires_at,
                is_active,
                created_by_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                character_id,
                clean_name,
                encode_json_list(clean_reference_asset_ids),
                encode_json_list(clean_authorization_project_ids),
                expires_at,
                1 if is_active else 0,
                actor.id,
            ),
        )
    return read_character(conn, character_id)


def update_character(
    conn: sqlite3.Connection,
    *,
    character_id: str,
    name: str | None,
    reference_asset_ids: list[str] | None,
    authorization_project_ids: list[str] | None,
    authorization_expires_at: datetime | None,
    clear_authorization_expires_at: bool,
    is_active: bool | None,
) -> CharacterData:
    read_character(conn, character_id)
    updates: list[str] = []
    params: list[object] = []

    if name is not None:
        updates.append("name = ?")
        params.append(normalize_name(name))
    if reference_asset_ids is not None:
        clean_reference_asset_ids = normalize_ids(reference_asset_ids)
        ensure_assets_exist(conn, clean_reference_asset_ids)
        updates.append("reference_asset_ids_json = ?")
        params.append(encode_json_list(clean_reference_asset_ids))
    if authorization_project_ids is not None:
        clean_project_ids = normalize_ids(authorization_project_ids)
        ensure_projects_exist(conn, clean_project_ids)
        updates.append("authorization_project_ids_json = ?")
        params.append(encode_json_list(clean_project_ids))
    if clear_authorization_expires_at:
        updates.append("authorization_expires_at = NULL")
    elif authorization_expires_at is not None:
        updates.append("authorization_expires_at = ?")
        params.append(encode_datetime(authorization_expires_at))
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if is_active else 0)

    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(character_id)
        with conn:
            conn.execute(
                f"""
                UPDATE characters
                SET {", ".join(updates)}
                WHERE id = ?
                """,
                params,
            )
    return read_character(conn, character_id)


def delete_character(conn: sqlite3.Connection, *, character_id: str) -> None:
    read_character(conn, character_id)
    with conn:
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))


def choose_project_main_character(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    project_id: str,
    character_id: str,
) -> dict[str, object]:
    character = read_character(conn, character_id)
    if not character_is_available(character, project_id=project_id):
        raise character_not_available()

    snapshot = character_snapshot(character)
    version_number = next_version_number(conn, project_id)
    version_id = str(uuid4())
    payload = {
        "project_id": project_id,
        "character_id": character.id,
        "character_snapshot": snapshot,
        "selected_by_user_id": actor.id,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO versions (
                id,
                project_id,
                asset_id,
                kind,
                version_number,
                payload_json,
                created_by_user_id
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                version_id,
                project_id,
                MAIN_CHARACTER_VERSION_KIND,
                version_number,
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
                actor.id,
            ),
        )
        conn.execute(
            """
            INSERT INTO project_main_characters (
                project_id,
                character_id,
                version_id,
                selected_by_user_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                character_id = excluded.character_id,
                version_id = excluded.version_id,
                selected_by_user_id = excluded.selected_by_user_id,
                selected_at = CURRENT_TIMESTAMP
            """,
            (project_id, character_id, version_id, actor.id),
        )
    return {
        "project_id": project_id,
        "character_id": character_id,
        "version_id": version_id,
        "version_number": version_number,
        "character_snapshot": snapshot,
    }


def read_character(conn: sqlite3.Connection, character_id: str) -> CharacterData:
    row = conn.execute(
        """
        SELECT
            id,
            name,
            reference_asset_ids_json,
            authorization_project_ids_json,
            authorization_expires_at,
            is_active,
            created_by_user_id,
            created_at,
            updated_at
        FROM characters
        WHERE id = ?
        """,
        (character_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CHARACTER_NOT_FOUND", "message": "Character does not exist."},
        )
    return character_from_row(row)


def character_from_row(row: sqlite3.Row) -> CharacterData:
    return CharacterData(
        id=str(row["id"]),
        name=str(row["name"]),
        reference_asset_ids=decode_json_list(row["reference_asset_ids_json"]),
        authorization_project_ids=decode_json_list(row["authorization_project_ids_json"]),
        authorization_expires_at=(
            None
            if row["authorization_expires_at"] is None
            else str(row["authorization_expires_at"])
        ),
        is_active=int(row["is_active"]) == 1,
        created_by_user_id=(
            None if row["created_by_user_id"] is None else str(row["created_by_user_id"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def character_snapshot(character: CharacterData) -> dict[str, object]:
    return {
        "id": character.id,
        "name": character.name,
        "reference_asset_ids": character.reference_asset_ids,
        "authorization_project_ids": character.authorization_project_ids,
        "authorization_expires_at": character.authorization_expires_at,
        "is_active": character.is_active,
    }


def character_is_available(character: CharacterData, *, project_id: str | None) -> bool:
    if not character.is_active:
        return False
    if is_expired(character.authorization_expires_at):
        return False
    if not character.authorization_project_ids:
        return True
    return project_id in character.authorization_project_ids


def next_version_number(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM versions
        WHERE project_id = ? AND kind = ?
        """,
        (project_id, MAIN_CHARACTER_VERSION_KIND),
    ).fetchone()
    return int(row[0])


def normalize_name(name: str) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=422,
            detail={"code": "CHARACTER_NAME_REQUIRED", "message": "Character name is required."},
        )
    return clean_name


def normalize_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean_value = value.strip()
        if not clean_value or clean_value in seen:
            continue
        seen.add(clean_value)
        result.append(clean_value)
    return result


def ensure_assets_exist(conn: sqlite3.Connection, asset_ids: list[str]) -> None:
    ensure_ids_exist(conn, table="assets", ids=asset_ids, code="ASSET_NOT_FOUND")


def ensure_projects_exist(conn: sqlite3.Connection, project_ids: list[str]) -> None:
    ensure_ids_exist(conn, table="projects", ids=project_ids, code="PROJECT_NOT_FOUND")


def ensure_ids_exist(
    conn: sqlite3.Connection,
    *,
    table: str,
    ids: list[str],
    code: str,
) -> None:
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    found = {str(row["id"]) for row in rows}
    missing = [item_id for item_id in ids if item_id not in found]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"code": code, "message": f"Missing ids: {', '.join(missing)}"},
        )


def encode_json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=True)


def decode_json_list(value: Any) -> list[str]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


def encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def is_expired(value: str | None) -> bool:
    if value is None:
        return False
    return parse_datetime(value) <= datetime.now(UTC)


def character_not_available() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "CHARACTER_NOT_AVAILABLE",
            "message": "Character is inactive, expired, or outside this project scope.",
        },
    )
