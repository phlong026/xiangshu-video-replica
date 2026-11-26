"""H3 asynchronous task, ownership, replay and billing integration on isolated PG."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    signup_license_payload,
    upgrade_test_database_to_head,
)

from app.analysis import AnalysisProviderFailed
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.prompt_optimizer import run_prompt_task, validate_result
from app.storage import LocalStorageAdapter

DB_NAME = "prompt_optimize_route_test"
REGISTER_PATH = "/api/customer/register"
VALID_PASSWORD = "correct-horse-battery"
OPTIMIZE_PATH = "/api/prompt-optimizations"


@pytest.fixture(scope="module")
def route_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(resolve_test_dsn())
    dsn = create_test_database(DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DB_NAME)


@pytest.fixture()
def route_state(route_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(route_dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE wallets, users, security_rate_limit_counters, billing_tariffs CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        # CASCADE 会连坐带 users 外键的单行配置表——注册赠送的种子行被清掉后，
        # 注册路径会 fail-closed（配置行缺失拒绝注册），必须重建回默认 0。
        conn.execute(
            "INSERT INTO registration_bonus_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"
        )
    yield route_dsn
    close_pg_pool()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[TestClient]:
    from app.customer_auth_routes import CustomerBrowserTransport
    from app.customer_auth_routes import router as customer_auth_router
    from app.customer_session_routes import router as session_router
    from app.prompt_optimizer_routes import router as prompt_router

    app = FastAPI()
    app.add_middleware(CustomerBrowserTransport)
    app.include_router(customer_auth_router)
    app.include_router(session_router)
    app.include_router(prompt_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    import base64
    import secrets

    for name in (
        "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY",
        "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY",
    ):
        monkeypatch.setenv(name, secrets.token_urlsafe(48))
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_ACCOUNT", "1000")
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def _customer(
    client: TestClient, dsn: str, *, credits: int = 0, username: str = "alice"
) -> tuple[dict[str, Any], dict[str, str]]:
    user = client.post(REGISTER_PATH, json=signup_license_payload(username, VALID_PASSWORD)).json()
    session = client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "username": username,
            "password": VALID_PASSWORD,
            "device_fingerprint": "prompt-optimize-device-0001",
        },
    ).json()
    if credits:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
                "VALUES('prompt_optimize',true,3,1)"
            )
            conn.execute(
                "UPDATE wallets SET available_credits=%s WHERE user_id=%s",
                (credits, user["user_id"]),
            )
    headers = {"Authorization": "Bearer " + session["session_token"]}
    return user, headers


GOOD_TEXT = (
    "integrated_multimodal_description: [Shot 1] A person waves.\n"
    "overall_soundscape: N/A\n"
    "non_diegetic_music: N/A"
)


class Provider:
    def __init__(self, model: str = "gemini-4-carbon") -> None:
        self.model = model
        self.calls = 0
        self.failure: Exception | None = None
        self.text = json.dumps({"prompt_text": GOOD_TEXT, "warnings": []})
        self.payloads: list[dict[str, Any]] = []

    def _complete(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        self.calls += 1
        self.payloads.append(payload)
        if self.failure:
            raise self.failure
        return self.text, {}

    def failover_pause(self, _: float) -> None:
        pass


def bind_providers(monkeypatch: pytest.MonkeyPatch, primary: Provider) -> Provider:
    fallback = Provider("gemini-3.8-flash")
    monkeypatch.setattr(
        "app.prompt_optimizer_routes.get_prompt_optimizer_providers",
        lambda conn: (primary, fallback),
    )
    return fallback


def run_one(dsn: str, tmp_path: Any) -> bool:
    @contextmanager
    def connection() -> Iterator[BusinessConnection]:
        with psycopg.connect(dsn) as raw:
            yield BusinessConnection.postgres(raw)

    return run_prompt_task(
        connection, worker_id="test-worker", storage=LocalStorageAdapter(root=tmp_path)
    )


def request_body(key: str = "click") -> dict[str, Any]:
    return {
        "route": "text_image",
        "prompt_text": "A person waves.",
        "duration_seconds": 8,
        "editor_revision": 1,
        "idempotency_key": key,
    }


def test_final_replica_preview_submission_and_stale_script_on_pg(
    client: TestClient,
    route_state: str,
) -> None:
    from fastapi import HTTPException

    from app.analysis import (
        FakeGemini,
        analyze_video,
        create_analysis_version,
        create_shot_card_version,
        insert_version,
    )
    from app.auth import CurrentUser
    from app.generation import (
        GenerationBatchRequest,
        PromptCompileRequest,
        PromptContext,
        ScriptRequest,
        compile_prompt_version,
        create_generation_batch,
        create_script_version,
    )

    user, _ = _customer(client, route_state, credits=100)
    actor = CurrentUser(id=user["user_id"], username="alice", display_name="alice", role="customer")
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO projects(id,name,owner_user_id) VALUES('final-project','Final',%s)",
            (actor.id,),
        )
        for asset, kind, mime in (
            ("final-source", "reference_video", "video/mp4"),
            ("final-frame", "first_frame", "image/png"),
        ):
            conn.execute(
                "INSERT INTO assets(id,project_id,kind,storage_uri,sha256,size_bytes,"
                "content_type,created_by_user_id) "
                "VALUES(%s,'final-project',%s,%s,'hash',10,%s,%s)",
                (asset, kind, f"local://test/{asset}", mime, actor.id),
            )
        raw.commit()
        result = analyze_video(video_uri="fake", video_duration_seconds=4, provider=FakeGemini())
        analysis = create_analysis_version(
            conn,
            project_id="final-project",
            asset_id="final-source",
            asset_uri="fake",
            created_by_user_id=actor.id,
            result=result,
        )
        shots = create_shot_card_version(
            conn,
            analysis_version=analysis,
            created_by_user_id=actor.id,
            shots=result.analysis.shots,
        )

        def version(kind: str, payload: dict[str, Any]):
            return insert_version(
                conn,
                project_id="final-project",
                asset_id="final-frame",
                kind=kind,
                created_by_user_id=actor.id,
                payload=payload,
            )

        source = version("source_frame_selection", {"timestamp_seconds": 0})
        candidates = version(
            "first_frame_candidates",
            {
                "source_frame_selection_version_id": source["id"],
                "review_mode": "HUMAN_CONFIRMATION",
                "candidates": [{"asset_id": "final-frame"}],
            },
        )
        version(
            "first_frame_selection",
            {
                "first_frame_candidates_version_id": candidates["id"],
                "first_frame_asset_id": "final-frame",
                "review_mode": "HUMAN_CONFIRMATION",
                "reviewed_by_user_id": actor.id,
            },
        )
        script = create_script_version(
            conn,
            project_id="final-project",
            actor=actor,
            request=ScriptRequest(
                source="custom", text="今天带你看庭院", shot_card_version_id=shots["id"]
            ),
        )
        final = compile_prompt_version(
            conn,
            project_id="final-project",
            actor=actor,
            request=PromptCompileRequest(
                script_version_id=script["id"],
                shot_card_version_id=shots["id"],
                first_frame_asset_id="final-frame",
                output_duration_seconds=4,
            ),
        )
        text = json.loads(final["payload_json"])["prompt_text"]
        request = GenerationBatchRequest(
            quantity=1,
            prompt_text=text,
            prompt_context=PromptContext(
                final_prompt_version_id=final["id"],
                script_version_id=script["id"],
                shot_card_version_id=shots["id"],
            ),
            first_frame_asset_id="final-frame",
            output_duration_seconds=4,
            idempotency_key="final-click",
        )
        batch = create_generation_batch(
            conn, project_id="final-project", actor=actor, request=request
        )
        replay = create_generation_batch(
            conn, project_id="final-project", actor=actor, request=request
        )
        assert batch.id == replay.id
        frozen = conn.execute(
            "SELECT prompt_snapshot_json FROM generation_tasks WHERE batch_id=%s", (batch.id,)
        ).fetchone()
        assert json.loads(frozen["prompt_snapshot_json"])["prompt_text"] == text
        create_script_version(
            conn,
            project_id="final-project",
            actor=actor,
            request=ScriptRequest(source="no_narration", text="", shot_card_version_id=shots["id"]),
        )
        with pytest.raises(HTTPException) as failure:
            create_generation_batch(
                conn,
                project_id="final-project",
                actor=actor,
                request=request.model_copy(update={"idempotency_key": "new-click"}),
            )
        assert failure.value.detail["code"] == "PROMPT_STALE"


def test_stale_conflicts_name_the_changed_stage(
    client: TestClient,
    route_state: str,
) -> None:
    """409 详情必须点名变旧的环节（stale_reasons）。

    统一只说“上游内容已变化”，用户不知道回去改拆解、分镜还是文案；
    服务端已经算出具体原因，不能丢掉。
    """
    from fastapi import HTTPException

    from app.analysis import (
        FakeGemini,
        analyze_video,
        create_analysis_version,
        create_shot_card_version,
        insert_version,
    )
    from app.auth import CurrentUser
    from app.generation import (
        PromptCompileRequest,
        ScriptRequest,
        compile_prompt_version,
        create_script_version,
    )

    user, _ = _customer(client, route_state, credits=100)
    actor = CurrentUser(id=user["user_id"], username="alice", display_name="alice", role="customer")
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)

        def seed_project(project_id: str, source_id: str, frame_id: str) -> None:
            conn.execute(
                "INSERT INTO projects(id,name,owner_user_id) VALUES(%s,%s,%s)",
                (project_id, project_id, actor.id),
            )
            for asset, kind, mime in (
                (source_id, "reference_video", "video/mp4"),
                (frame_id, "first_frame", "image/png"),
            ):
                conn.execute(
                    "INSERT INTO assets(id,project_id,kind,storage_uri,sha256,size_bytes,"
                    "content_type,created_by_user_id) "
                    "VALUES(%s,%s,%s,%s,'hash',10,%s,%s)",
                    (asset, project_id, kind, f"local://test/{asset}", mime, actor.id),
                )
            raw.commit()

        seed_project("stale-project", "stale-source", "stale-frame")
        result = analyze_video(video_uri="fake", video_duration_seconds=4, provider=FakeGemini())
        analysis = create_analysis_version(
            conn,
            project_id="stale-project",
            asset_id="stale-source",
            asset_uri="fake",
            created_by_user_id=actor.id,
            result=result,
        )
        shots = create_shot_card_version(
            conn,
            analysis_version=analysis,
            created_by_user_id=actor.id,
            shots=result.analysis.shots,
        )

        def version(project_id: str, asset_id: str, kind: str, payload: dict[str, Any]):
            return insert_version(
                conn,
                project_id=project_id,
                asset_id=asset_id,
                kind=kind,
                created_by_user_id=actor.id,
                payload=payload,
            )

        def confirm_first_frame(project_id: str, frame_id: str, review_mode: str) -> None:
            source = version(
                project_id, frame_id, "source_frame_selection", {"timestamp_seconds": 0}
            )
            candidates = version(
                project_id,
                frame_id,
                "first_frame_candidates",
                {
                    "source_frame_selection_version_id": source["id"],
                    "review_mode": review_mode,
                    "candidates": [{"asset_id": frame_id}],
                },
            )
            selection: dict[str, Any] = {
                "first_frame_candidates_version_id": candidates["id"],
                "first_frame_asset_id": frame_id,
                "review_mode": review_mode,
            }
            if review_mode == "HUMAN_CONFIRMATION":
                selection["reviewed_by_user_id"] = actor.id
            version(project_id, frame_id, "first_frame_selection", selection)

        confirm_first_frame("stale-project", "stale-frame", "HUMAN_CONFIRMATION")
        script = create_script_version(
            conn,
            project_id="stale-project",
            actor=actor,
            request=ScriptRequest(
                source="custom", text="今天带你看庭院", shot_card_version_id=shots["id"]
            ),
        )

        # 分镜被新版本取代：保存文案与编译都必须点名“分镜”。
        shots2 = create_shot_card_version(
            conn,
            analysis_version=analysis,
            created_by_user_id=actor.id,
            shots=result.analysis.shots,
        )
        with pytest.raises(HTTPException) as failure:
            create_script_version(
                conn,
                project_id="stale-project",
                actor=actor,
                request=ScriptRequest(
                    source="custom", text="今天带你看庭院", shot_card_version_id=shots["id"]
                ),
            )
        assert failure.value.detail["code"] == "SHOT_CARD_STALE"
        assert failure.value.detail["stale_reasons"] == ["SHOT_CARD_SUPERSEDED"]

        with pytest.raises(HTTPException) as failure:
            compile_prompt_version(
                conn,
                project_id="stale-project",
                actor=actor,
                request=PromptCompileRequest(
                    script_version_id=script["id"],
                    shot_card_version_id=shots["id"],
                    first_frame_asset_id="stale-frame",
                    output_duration_seconds=4,
                ),
            )
        assert failure.value.detail["code"] == "SCRIPT_STALE"
        assert failure.value.detail["stale_reasons"] == ["SHOT_CARD_SUPERSEDED"]

        # 拆解被重跑：分镜基于旧拆解，保存文案与编译都必须点名“拆解”。
        script2 = create_script_version(
            conn,
            project_id="stale-project",
            actor=actor,
            request=ScriptRequest(
                source="custom", text="今天带你看庭院", shot_card_version_id=shots2["id"]
            ),
        )
        create_analysis_version(
            conn,
            project_id="stale-project",
            asset_id="stale-source",
            asset_uri="fake",
            created_by_user_id=actor.id,
            result=result,
        )
        with pytest.raises(HTTPException) as failure:
            create_script_version(
                conn,
                project_id="stale-project",
                actor=actor,
                request=ScriptRequest(
                    source="custom", text="今天带你看庭院", shot_card_version_id=shots2["id"]
                ),
            )
        assert failure.value.detail["code"] == "SHOT_CARD_STALE"
        assert failure.value.detail["stale_reasons"] == ["ANALYSIS_SUPERSEDED"]

        with pytest.raises(HTTPException) as failure:
            compile_prompt_version(
                conn,
                project_id="stale-project",
                actor=actor,
                request=PromptCompileRequest(
                    script_version_id=script2["id"],
                    shot_card_version_id=shots2["id"],
                    first_frame_asset_id="stale-frame",
                    output_duration_seconds=4,
                ),
            )
        assert failure.value.detail["code"] == "SCRIPT_STALE"
        assert failure.value.detail["stale_reasons"] == ["ANALYSIS_SUPERSEDED"]

        # 首帧质检证据过期是独立环节：客户端需要能单独识别这个码并可自救。
        seed_project("quality-project", "quality-source", "quality-frame")
        quality_analysis = create_analysis_version(
            conn,
            project_id="quality-project",
            asset_id="quality-source",
            asset_uri="fake",
            created_by_user_id=actor.id,
            result=result,
        )
        quality_shots = create_shot_card_version(
            conn,
            analysis_version=quality_analysis,
            created_by_user_id=actor.id,
            shots=result.analysis.shots,
        )
        confirm_first_frame("quality-project", "quality-frame", "AUTO")
        quality_script = create_script_version(
            conn,
            project_id="quality-project",
            actor=actor,
            request=ScriptRequest(
                source="custom",
                text="今天带你看庭院",
                shot_card_version_id=quality_shots["id"],
            ),
        )
        with pytest.raises(HTTPException) as failure:
            compile_prompt_version(
                conn,
                project_id="quality-project",
                actor=actor,
                request=PromptCompileRequest(
                    script_version_id=quality_script["id"],
                    shot_card_version_id=quality_shots["id"],
                    first_frame_asset_id="quality-frame",
                    output_duration_seconds=4,
                ),
            )
        assert failure.value.detail["code"] == "FIRST_FRAME_QUALITY_NOT_VERIFIED"


def test_async_request_replay_and_single_charge(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    user, headers = _customer(client, route_state, credits=10)
    provider = Provider()
    fallback = bind_providers(monkeypatch, provider)
    body = request_body()
    first = client.post(OPTIMIZE_PATH, headers=headers, json=body)
    assert first.status_code == 202, first.text
    task = first.json()
    assert task["status"] == "PENDING" and provider.calls == 0
    assert (
        client.post(OPTIMIZE_PATH, headers=headers, json=body).json()["task_id"] == task["task_id"]
    )
    assert (
        client.post(
            OPTIMIZE_PATH, headers=headers, json={**body, "prompt_text": "different"}
        ).status_code
        == 409
    )
    assert run_one(route_state, tmp_path)
    result = client.get(OPTIMIZE_PATH + "/" + task["task_id"], headers=headers).json()
    assert result["status"] == "SUCCEEDED", result
    assert result["result"]["prompt_text"] == GOOD_TEXT
    assert not run_one(route_state, tmp_path)
    assert provider.calls == 1
    assert fallback.calls == 0
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s",
            (user["user_id"],),
        ).fetchone() == (7, 0)
        assert conn.execute("SELECT count(*) FROM generation_batches").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state,usage FROM billing_attempts WHERE service='prompt_optimize'"
        ).fetchone() == ("ACTUAL", 1)


@pytest.mark.parametrize("bad", ["network", "json", "structure", "fallback_network", "filtered"])
def test_failure_preserves_cost_and_never_resubmits(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, bad: str
) -> None:
    user, headers = _customer(client, route_state, credits=10)
    provider = Provider()
    fallback = bind_providers(monkeypatch, provider)
    if bad == "network":
        provider.failure = AnalysisProviderFailed("network", failure_phase="network")
    elif bad == "filtered":
        provider.failure = AnalysisProviderFailed(
            "filtered", upstream_diagnostic={"reason": "finish_reason_content_filter"}
        )
    elif bad in ("json", "fallback_network"):
        provider.text = "broken"
        fallback.text = "broken"
        if bad == "fallback_network":
            fallback.failure = AnalysisProviderFailed("network", failure_phase="network")
    else:
        provider.text = json.dumps(
            {"prompt_text": GOOD_TEXT.replace("[Shot 1]", "[Shot 2]"), "warnings": []}
        )
        fallback.text = provider.text
    first = client.post(OPTIMIZE_PATH, headers=headers, json=request_body()).json()
    assert run_one(route_state, tmp_path)
    result = client.get(OPTIMIZE_PATH + "/" + first["task_id"], headers=headers).json()
    expected = "SUBMISSION_UNCERTAIN" if bad in ("network", "fallback_network") else "FAILED"
    assert result["status"] == expected, result
    client.post(OPTIMIZE_PATH, headers=headers, json=request_body())
    assert not run_one(route_state, tmp_path)
    assert provider.calls == 1
    assert fallback.calls == (0 if bad in ("network", "filtered") else 1)
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s",
            (user["user_id"],),
        ).fetchone() == (10, 0)


def test_assumptions_apply_without_blocking(
    client: TestClient, route_state: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """warnings 是假设清单：只记录推断、不阻断应用，随结果回传给核对区。"""
    user, headers = _customer(client, route_state, credits=10)
    provider = Provider()
    provider.text = json.dumps(
        {
            "prompt_text": GOOD_TEXT,
            "warnings": [{"code": "ASSUMED_SCENE", "message": "已按需求推断为室内客厅"}],
        }
    )
    fallback = bind_providers(monkeypatch, provider)
    task = client.post(OPTIMIZE_PATH, headers=headers, json=request_body()).json()
    assert run_one(route_state, tmp_path)
    result = client.get(OPTIMIZE_PATH + "/" + task["task_id"], headers=headers).json()
    assert result["status"] == "SUCCEEDED", result
    assert result["result"]["validation_status"] == "valid"
    assert result["result"]["assumptions"] == [
        {"code": "ASSUMED_SCENE", "message": "已按需求推断为室内客厅"}
    ]
    assert result["result"]["needs_confirmation"] == []
    assert result["result"]["reference_plan"] == []
    assert provider.calls == 1
    assert fallback.calls == 0
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT available_credits FROM wallets WHERE user_id=%s", (user["user_id"],)
            ).fetchone()[0]
            == 7
        )


@pytest.mark.parametrize("reason", ["timeline", "json", "rate_limit"])
def test_invalid_primary_or_rate_limit_switches_to_fallback(
    client: TestClient,
    route_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    reason: str,
) -> None:
    """不同模型的请求仍归属于同一次优化和结算。"""
    user, headers = _customer(client, route_state, credits=10)
    provider = Provider()
    fallback = bind_providers(monkeypatch, provider)
    if reason == "timeline":
        provider.text = json.dumps(
            {"prompt_text": GOOD_TEXT.replace("waves.", "waves. At 00:08, stops."), "warnings": []}
        )
    elif reason == "json":
        provider.text = "broken"
    else:
        provider.failure = AnalysisProviderFailed(
            "rate limit", http_status=429, failure_phase="http"
        )
    task = client.post(OPTIMIZE_PATH, headers=headers, json=request_body()).json()
    assert run_one(route_state, tmp_path)
    result = client.get(OPTIMIZE_PATH + "/" + task["task_id"], headers=headers).json()
    assert result["status"] == "SUCCEEDED", result
    assert result["result"]["prompt_text"] == GOOD_TEXT
    assert provider.calls == fallback.calls == 1
    assert provider.payloads[0]["model"] == "gemini-4-carbon"
    assert fallback.payloads[0]["model"] == "gemini-3.8-flash"
    if reason != "rate_limit":
        correction = fallback.payloads[0]["messages"][-1]["content"][0]["text"]
        assert ("镜头时间须递增" if reason == "timeline" else "返回合法 JSON") in correction
    with psycopg.connect(route_state) as conn:
        assert (
            conn.execute(
                "SELECT available_credits FROM wallets WHERE user_id=%s", (user["user_id"],)
            ).fetchone()[0]
            == 7
        )
        assert conn.execute(
            "SELECT state,usage FROM billing_attempts WHERE service='prompt_optimize'"
        ).fetchall() == [("ACTUAL", 1)]


def test_zero_second_opening_is_normalized_and_interior_timestamp_is_preserved() -> None:
    text = GOOD_TEXT.replace(
        "[Shot 1] A person waves.", "[Shot 1] At 00:00, starts. At 00:03, waves."
    )
    result, state = validate_result(
        json.dumps({"prompt_text": text, "warnings": []}),
        snapshot={
            "context": {"mode": "T2VA", "duration_seconds": 6, "generation_assets": []},
            "prompt_text": "A person waves.",
        },
    )
    assert state == "SUCCEEDED"
    assert result["prompt_text"] == text.replace("At 00:00, ", "开场时，")


def test_ownership_and_missing_assets(client: TestClient, route_state: str, tmp_path: Any) -> None:
    _, headers = _customer(client, route_state)
    response = client.post(
        OPTIMIZE_PATH, headers=headers, json={**request_body(), "route": "replica"}
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    assert run_one(route_state, tmp_path)
    assert (
        client.get(OPTIMIZE_PATH + "/" + task_id, headers=headers).json()["status"] == "NEEDS_INPUT"
    )
    assert client.get(OPTIMIZE_PATH + "/missing", headers=headers).status_code == 404
    assert client.get(OPTIMIZE_PATH + "/" + task_id).status_code == 401
    response = client.post(
        OPTIMIZE_PATH,
        headers=headers,
        json={**request_body("foreign"), "first_frame_asset_id": "not-owned"},
    )
    assert response.status_code == 404


def test_reference_purpose_defaults_are_flagged_for_confirmation(
    client: TestClient, route_state: str
) -> None:
    """用途留空不再让整单失败：音频给默认用途、图片交给模型看图判断，都进待确认。

    旧行为（REFERENCE_PURPOSE_REQUIRED / AUDIO_DESCRIPTION_REQUIRED）会整单失败——
    用户只是没填用途，AI 却什么都不做。图片不按顺序臆造用途：模型看得到画面，
    场景图被标成「人物」会直接写错 subject_definitions。
    """
    from app.auth import CurrentUser
    from app.h3_prompts import GenerationContext, Reference
    from app.prompt_context import DEFAULT_AUDIO_PURPOSE, resolve_context

    user, _ = _customer(client, route_state)
    actor = CurrentUser(id=user["user_id"], username="alice", display_name="alice", role="customer")
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        for asset_id, kind, mime in (
            ("ctx-image-1", "material_image", "image/png"),
            ("ctx-image-2", "material_image", "image/png"),
            ("ctx-audio", "material_audio", "audio/mpeg"),
            ("ctx-image-named", "material_image", "image/png"),
        ):
            conn.execute(
                "INSERT INTO assets(id,kind,storage_uri,sha256,size_bytes,content_type,"
                "created_by_user_id) VALUES(%s,%s,%s,'hash',10,%s,%s)",
                (asset_id, kind, f"local://test/{asset_id}", mime, actor.id),
            )
        raw.commit()
        context = resolve_context(
            conn,
            actor=actor,
            request=GenerationContext(
                route="reference",
                references=[
                    Reference(asset_id="ctx-image-1", purpose="unspecified"),
                    Reference(asset_id="ctx-image-2", purpose="unspecified"),
                    Reference(asset_id="ctx-audio", purpose="unspecified"),
                    Reference(asset_id="ctx-image-named", purpose="人物、服装"),
                ],
            ),
        )

    assert [asset["purpose"] for asset in context["generation_assets"]] == [
        "unspecified",
        "unspecified",
        DEFAULT_AUDIO_PURPOSE,
        "人物、服装",
    ]
    assert [item["code"] for item in context["needs_confirmation"]] == [
        "REFERENCE_PURPOSE_UNDECLARED",
        "REFERENCE_PURPOSE_UNDECLARED",
        "AUDIO_PURPOSE_INFERRED",
    ]
    assert [item["alias"] for item in context["needs_confirmation"]] == ["@1", "@2", "@3"]
    assert context["needs_confirmation"][2]["purpose"] == DEFAULT_AUDIO_PURPOSE
    # 图片的待确认项不带 purpose：系统没有替用户定用途，正文里的角色由模型看图判断。
    assert context["needs_confirmation"][0].get("purpose") is None
    assert context["issues"] == []


@pytest.mark.parametrize("sent", [False, True])
def test_expired_lease_refunds_without_resubmitting(
    client: TestClient, route_state: str, tmp_path: Any, sent: bool
) -> None:
    from app.usage_billing import begin_source_attempt, reconcile_operations

    user, headers = _customer(client, route_state, credits=10)
    task = client.post(OPTIMIZE_PATH, headers=headers, json=request_body()).json()
    with psycopg.connect(route_state) as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "UPDATE prompt_optimization_receipts SET status='RUNNING', "
            "lease_expires_at='2000-01-01',provider_started_at=%s WHERE id=%s",
            ("2000-01-01" if sent else None, task["task_id"]),
        )
        if sent:
            begin_source_attempt(conn, task["task_id"])
        reconcile_operations(conn)
    assert not run_one(route_state, tmp_path)
    result = client.get(OPTIMIZE_PATH + "/" + task["task_id"], headers=headers).json()
    assert result["status"] == ("SUBMISSION_UNCERTAIN" if sent else "FAILED")
    with psycopg.connect(route_state) as conn:
        assert conn.execute(
            "SELECT available_credits,reserved_credits FROM wallets WHERE user_id=%s",
            (user["user_id"],),
        ).fetchone() == (10, 0)


def test_task_is_private_to_its_owner(client: TestClient, route_state: str) -> None:
    _, alice_headers = _customer(client, route_state)
    task = client.post(OPTIMIZE_PATH, headers=alice_headers, json=request_body()).json()
    _, bob_headers = _customer(client, route_state, username="bob")
    assert client.get(OPTIMIZE_PATH + "/" + task["task_id"], headers=bob_headers).status_code == 404


def test_concurrent_creation_reserves_once(client: TestClient, route_state: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    user, headers = _customer(client, route_state, credits=10)

    def submit():
        return client.post(OPTIMIZE_PATH, headers=headers, json=request_body())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: submit(), range(2)))
    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    with psycopg.connect(route_state) as conn:
        assert conn.execute("SELECT count(*) FROM prompt_optimization_receipts").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT reserved_credits FROM wallets WHERE user_id=%s", (user["user_id"],)
            ).fetchone()[0]
            == 3
        )
