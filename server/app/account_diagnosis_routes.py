"""账号诊断的客户接口（自助诊断，P0 只支持抖音）."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.account_diagnosis import availability, create_diagnosis, get_report, list_reports
from app.account_link import AccountLinkError, parse_account_target
from app.auth import AuthenticatedUser, Database

router = APIRouter(prefix="/api", tags=["account-diagnosis"])


class DiagnosisRequest(BaseModel):
    input: str = Field(min_length=1, max_length=2000)
    # 可选：前端只传链接时由后端解析；传抖音号也可以。
    platform: str = "douyin"


@router.get("/account-diagnosis/availability")
def account_diagnosis_availability(
    user: AuthenticatedUser,
    conn: Database,
) -> dict[str, Any]:
    return availability(conn, user_id=user.id)


@router.post("/account-diagnosis")
def create_account_diagnosis(
    payload: DiagnosisRequest,
    user: AuthenticatedUser,
    conn: Database,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if payload.platform != "douyin":
        raise HTTPException(
            422,
            detail={
                "code": "ACCOUNT_DIAGNOSIS_UNSUPPORTED_PLATFORM",
                "message": "目前仅支持抖音账号诊断。",
            },
        )
    try:
        target = parse_account_target(payload.input)
    except AccountLinkError as exc:
        raise HTTPException(
            exc.status_code, detail={"code": exc.code, "message": exc.message}
        ) from exc
    return create_diagnosis(
        conn,
        user_id=user.id,
        target=target,
        input_url=payload.input,
        idempotency_key=idempotency_key,
    )


@router.get("/account-diagnosis")
def list_account_diagnoses(
    user: AuthenticatedUser,
    conn: Database,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    return {"items": list_reports(conn, user_id=user.id, limit=limit)}


@router.get("/account-diagnosis/{report_id}")
def get_account_diagnosis(
    report_id: str,
    user: AuthenticatedUser,
    conn: Database,
) -> dict[str, Any]:
    return get_report(conn, user_id=user.id, report_id=report_id)
