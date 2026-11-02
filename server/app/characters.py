from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.auth import CurrentUser
from app.db_portable import BusinessConnection, BusinessRow

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


def get_project_main_character(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT
            main_character.version_id,
            version.version_number,
            version.payload_json
        FROM project_main_characters AS main_character
        JOIN versions AS version ON version.id = main_character.version_id
        WHERE main_character.project_id = %s
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MAIN_CHARACTER_NOT_FOUND",
                "message": "Project has no main character.",
            },
        )
    payload = json.loads(str(row["payload_json"]))
    return {
        "project_id": project_id,
        "character_id": (
            None if payload.get("character_id") is None else str(payload["character_id"])
        ),
        "character_version_id": (
            None
            if payload.get("character_version_id") is None
            else str(payload["character_version_id"])
        ),
        "version_id": str(row["version_id"]),
        "version_number": int(row["version_number"]),
        "character_snapshot": payload["character_snapshot"],
    }


def read_character(conn: BusinessConnection, character_id: str) -> CharacterData:
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
        WHERE id = %s
        """,
        (character_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CHARACTER_NOT_FOUND", "message": "Character does not exist."},
        )
    return character_from_row(row)


def character_from_row(row: BusinessRow) -> CharacterData:
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


def character_is_available(character: CharacterData, *, project_id: str | None) -> bool:
    if not character.is_active:
        return False
    if is_expired(character.authorization_expires_at):
        return False
    if not character.authorization_project_ids:
        return True
    return project_id in character.authorization_project_ids


def character_is_owned_or_project_authorized(
    character: CharacterData,
    *,
    actor: CurrentUser,
    project_id: str | None,
) -> bool:
    if character.created_by_user_id == actor.id:
        return True
    return project_id is not None and project_id in character.authorization_project_ids


def next_version_number(conn: BusinessConnection, project_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM versions
        WHERE project_id = %s AND kind = %s
        """,
        (project_id, MAIN_CHARACTER_VERSION_KIND),
    ).fetchone()
    return int(row[0])


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
