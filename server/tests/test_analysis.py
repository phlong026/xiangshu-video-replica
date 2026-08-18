from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.analysis import (
    APILIO_DEFAULT_BASE_URL,
    HTTP_FAILURE_PHASE,
    NETWORK_FAILURE_PHASE,
    RESPONSE_FAILURE_PHASE,
    AnalysisProviderFailed,
    ApilioGemini,
    FakeGemini,
    ProviderResponse,
    UrllibApilioChatTransport,
    analyze_video,
    parse_analysis_response,
)
from app.analysis_routes import get_video_analysis_provider, signed_video_url_for_provider
from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app
from app.settings import SETTINGS_KEY_ENV, SettingsRepository
from app.storage import FakeStorageAdapter


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "analysis.db"
    with initialize_database(db_path) as conn:
        seed_data(conn)
    yield db_path


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_data(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO users (id, username, display_name, role)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO projects (id, owner_user_id, name)
        VALUES (?, ?, ?)
        """,
        [
            ("project_owned", "employee_1", "Owned Project"),
            ("project_other", "employee_2", "Other Project"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO assets (
            id,
            project_id,
            kind,
            storage_uri,
            sha256,
            size_bytes,
            content_type,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "asset_owned",
                "project_owned",
                "reference_video",
                "local://owned.mp4",
                "sha-owned",
                12,
                "video/mp4",
                "employee_1",
            ),
            (
                "asset_other",
                "project_other",
                "reference_video",
                "local://other.mp4",
                "sha-other",
                12,
                "video/mp4",
                "employee_2",
            ),
        ],
    )
    conn.commit()


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def valid_analysis_payload() -> dict[str, object]:
    return {
        "summary": "短视频拆解",
        "duration_seconds": 10,
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 30,
        "theme": "人物口播",
        "visual_style": "写实",
        "pace": "快",
        "camera_language": "近景轻微推进",
        "original_script": "你好",
        "shots": [
            {
                "shot_id": "S01",
                "start_time": 0,
                "end_time": 5,
                "shot_type": "近景",
                "composition": "人物居中",
                "camera_motion": "轻微推进",
                "subject": "主讲人",
                "action": "看向镜头讲话",
                "scene": "室内",
                "spoken_text": "你好",
                "transition": "硬切",
                "motion": {
                    "subject_motion_state": "GESTURING_ONLY",
                    "subject_direction": "in_place",
                    "subject_displacement": "无位移",
                    "hand_action": "双手做自然讲解手势",
                    "camera_motion": "PUSH_IN",
                    "relative_motion": "镜头缓慢推近，人物站位不变",
                },
            },
            {
                "shot_id": "S02",
                "start_time": 5,
                "end_time": 10,
                "shot_type": "中景",
                "composition": "三分法",
                "camera_motion": "固定",
                "subject": "产品",
                "action": "展示产品",
                "scene": "桌面",
                "spoken_text": "再见",
                "transition": "淡出",
                "motion": {
                    "subject_motion_state": "OBJECT_MOTION",
                    "subject_direction": "none",
                    "subject_displacement": "无位移",
                    "hand_action": "无人物出镜",
                    "camera_motion": "STATIC",
                    "relative_motion": "固定机位拍摄产品",
                },
            },
        ],
    }


def test_fake_gemini_analysis_repairs_invalid_json_once() -> None:
    provider = FakeGemini(
        analysis_json='{"summary":',
        repair_json=json.dumps(valid_analysis_payload()),
    )

    result = analyze_video(
        video_uri="local://owned.mp4",
        video_duration_seconds=10,
        provider=provider,
    )

    assert provider.repair_calls == 1
    assert result.analysis.summary == "短视频拆解"
    assert result.provider_response_ref["stored_as"] == "versions.payload_json"
    assert result.provider_response_ref["raw"]["text"] == '{"summary":'
    assert "video_uri" not in result.provider_response_ref["raw"]


def test_analysis_rejects_shots_without_motion_and_repairs_once() -> None:
    """缺 motion 的拆解结果必须触发 repair；修复后仍缺则整体失败。"""
    payload_without_motion = valid_analysis_payload()
    for shot in payload_without_motion["shots"]:
        del shot["motion"]

    provider = FakeGemini(
        analysis_json=json.dumps(payload_without_motion),
        repair_json=json.dumps(valid_analysis_payload()),
    )
    result = analyze_video(
        video_uri="local://owned.mp4",
        video_duration_seconds=10,
        provider=provider,
    )

    assert provider.repair_calls == 1
    assert all(shot.motion is not None for shot in result.analysis.shots)

    provider_still_missing = FakeGemini(
        analysis_json=json.dumps(payload_without_motion),
        repair_json=json.dumps(payload_without_motion),
    )
    with pytest.raises(AnalysisProviderFailed, match="even after a repair attempt"):
        analyze_video(
            video_uri="local://owned.mp4",
            video_duration_seconds=10,
            provider=provider_still_missing,
        )


class RecordedApilioTransport:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, str], bytes]] = []

    def post(
        self, url: str, *, headers: dict[str, str], body: bytes
    ) -> tuple[bytes, dict[str, str]]:
        self.requests.append((url, headers, body))
        return self.response, {"content-type": "application/json"}


def test_apilio_gemini_uses_signed_video_url_and_records_no_secret_or_url() -> None:
    response_text = json.dumps(valid_analysis_payload())
    transport = RecordedApilioTransport(
        json.dumps({"id": "chat-1", "choices": [{"message": {"content": response_text}}]}).encode()
    )
    provider = ApilioGemini(api_key="analysis-secret", transport=transport)

    response = provider.analyze(
        video_uri="https://storage.example/video.mp4?signature=secret",
        duration_seconds=10,
    )

    assert response.text == response_text
    assert response.raw == {
        "provider": "apilio_gemini",
        "model": "gemini-3.1-pro-preview",
        "response_id": "chat-1",
    }
    url, headers, body = transport.requests[0]
    assert url == "https://api.apilio.ai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer analysis-secret"
    request = json.loads(body)
    assert request["model"] == "gemini-3.1-pro-preview"
    assert request["messages"][0]["content"][1]["image_url"]["url"].startswith("https://")


def test_apilio_gemini_rejects_non_https_video_urls() -> None:
    provider = ApilioGemini(api_key="analysis-secret", transport=RecordedApilioTransport(b"{}"))

    with pytest.raises(AnalysisProviderFailed, match="HTTPS signed video URL"):
        provider.analyze(video_uri="local://private/video.mp4", duration_seconds=10)


def test_video_analysis_uses_the_fixed_apilio_origin_when_legacy_base_url_exists(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(SETTINGS_KEY_ENV, key)
    with connect_database(db_path) as conn:
        SettingsRepository(conn, fernet=Fernet(key.encode("ascii"))).save_provider_config(
            "apilio",
            {
                "analysis_api_key": "analysis-secret",
                "base_url": "https://untrusted.example",
            },
            actor_user_id="admin_1",
        )
        provider = get_video_analysis_provider(conn)

    assert isinstance(provider, ApilioGemini)
    assert provider.base_url == APILIO_DEFAULT_BASE_URL


def test_real_video_analysis_refuses_a_non_https_storage_download_intent() -> None:
    storage = FakeStorageAdapter(provider="cos", bucket="private-video")

    with pytest.raises(HTTPException) as exc_info:
        signed_video_url_for_provider(storage, asset_uri="cos://private-video/reference.mp4")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "ANALYSIS_VIDEO_URL_UNAVAILABLE"
    assert exc_info.value.detail["message"] == (
        "当前视频分析模型只能读取 HTTPS 视频；本地存储无法用于真实视频拆解。"
        "请在设置中切换至腾讯云 COS 后重新上传。"
    )


def test_analysis_schema_rejects_overlap_and_unknown_fields() -> None:
    payload = valid_analysis_payload()
    payload["unexpected"] = "reject me"

    with pytest.raises(ValidationError):
        parse_analysis_response(json.dumps(payload), duration_seconds=10)

    overlap = valid_analysis_payload()
    shots = overlap["shots"]
    assert isinstance(shots, list)
    shots[1]["start_time"] = 4

    with pytest.raises(ValidationError):
        parse_analysis_response(json.dumps(overlap), duration_seconds=10)


def test_project_owner_can_create_and_read_analysis_version(client: TestClient) -> None:
    response = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "analysis"
    assert body["version_number"] == 1
    assert body["payload"]["analysis"]["shots"][0]["shot_id"] == "S01"
    assert body["payload"]["provider_response_ref"]["stored_as"] == "versions.payload_json"

    fetched = client.get(f"/api/analysis/{body['id']}", headers=auth_headers("employee_1"))

    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


def test_analysis_can_restore_duration_from_completed_asset_metadata(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE assets SET metadata_json = ? WHERE id = ?",
            (json.dumps({"duration_seconds": 8}), "asset_owned"),
        )
        conn.commit()

    response = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned"},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    assert response.json()["payload"]["analysis"]["duration_seconds"] == 8


def test_analysis_recovery_reuses_the_existing_version(
    client: TestClient,
    db_path: Path,
) -> None:
    first = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )
    recovered = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned"},
        headers=auth_headers("employee_1"),
    )

    assert first.status_code == 200
    assert recovered.status_code == 200
    assert recovered.json()["id"] == first.json()["id"]
    assert recovered.json()["version_number"] == 1

    with connect_database(db_path) as conn:
        version_count = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE project_id = ? AND kind = ?",
            ("project_owned", "analysis"),
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT action FROM audit_logs WHERE action = ?",
            ("analysis.recover_existing",),
        ).fetchone()

    assert version_count == 1
    assert audit is not None


def test_concurrent_analysis_recovery_creates_only_one_version(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE assets SET metadata_json = ? WHERE id = ?",
            (json.dumps({"duration_seconds": 8}), "asset_owned"),
        )
        conn.commit()

    barrier = threading.Barrier(2)
    calls_lock = threading.Lock()

    class BarrierFakeGemini(FakeGemini):
        analysis_calls = 0

        def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
            with calls_lock:
                self.analysis_calls += 1
            barrier.wait(timeout=5)
            return super().analyze(video_uri=video_uri, duration_seconds=duration_seconds)

    provider = BarrierFakeGemini()
    monkeypatch.setattr(
        "app.analysis_routes.get_video_analysis_provider",
        lambda _conn: provider,
    )
    responses = []
    responses_lock = threading.Lock()

    def recover() -> None:
        response = client.post(
            "/api/projects/project_owned/analysis",
            json={"asset_id": "asset_owned"},
            headers=auth_headers("employee_1"),
        )
        with responses_lock:
            responses.append(response)

    threads = [threading.Thread(target=recover), threading.Thread(target=recover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [response.status_code for response in responses] == [200, 200]
    assert provider.analysis_calls == 2
    assert len({response.json()["id"] for response in responses}) == 1
    with connect_database(db_path) as conn:
        version_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM versions
            WHERE project_id = ? AND asset_id = ? AND kind = ?
            """,
            ("project_owned", "asset_owned", "analysis"),
        ).fetchone()[0]
    assert version_count == 1


def test_concurrent_idempotent_analysis_starts_create_only_one_version(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    calls_lock = threading.Lock()

    class BarrierFakeGemini(FakeGemini):
        analysis_calls = 0

        def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
            with calls_lock:
                self.analysis_calls += 1
            barrier.wait(timeout=5)
            return super().analyze(video_uri=video_uri, duration_seconds=duration_seconds)

    provider = BarrierFakeGemini()
    monkeypatch.setattr(
        "app.analysis_routes.get_video_analysis_provider",
        lambda _conn: provider,
    )
    responses = []
    responses_lock = threading.Lock()

    def start() -> None:
        response = client.post(
            "/api/projects/project_owned/analysis",
            json={
                "asset_id": "asset_owned",
                "duration_seconds": 10,
                "reuse_existing": True,
            },
            headers=auth_headers("employee_1"),
        )
        with responses_lock:
            responses.append(response)

    threads = [threading.Thread(target=start), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [response.status_code for response in responses] == [200, 200]
    assert provider.analysis_calls == 2
    assert len({response.json()["id"] for response in responses}) == 1
    with connect_database(db_path) as conn:
        version_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM versions
            WHERE project_id = ? AND asset_id = ? AND kind = ?
            """,
            ("project_owned", "asset_owned", "analysis"),
        ).fetchone()[0]
    assert version_count == 1


def test_project_owner_can_read_the_latest_analysis_version(client: TestClient) -> None:
    first = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )
    second = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )
    latest = client.get(
        "/api/projects/project_owned/analysis/latest",
        headers=auth_headers("employee_1"),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert latest.status_code == 200
    assert latest.json()["id"] == second.json()["id"]
    assert latest.json()["version_number"] == 2


def test_analysis_routes_require_existing_rbac(client: TestClient) -> None:
    auditor = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("auditor_1"),
    )
    other_owner = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_2"),
    )

    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert other_owner.status_code == 403
    assert other_owner.json()["detail"]["code"] == "PROJECT_FORBIDDEN"


def test_analysis_rejects_pending_or_non_reference_assets(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE assets SET sha256 = ?, size_bytes = ? WHERE id = ?",
            ("", 0, "asset_owned"),
        )
        conn.execute(
            "UPDATE assets SET kind = ? WHERE id = ?",
            ("video", "asset_other"),
        )
        conn.execute(
            "UPDATE assets SET project_id = ? WHERE id = ?",
            ("project_owned", "asset_other"),
        )
        conn.commit()

    pending = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )
    wrong_kind = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_other", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )

    assert pending.status_code == 409
    assert pending.json()["detail"]["code"] == "REFERENCE_VIDEO_NOT_READY"
    assert wrong_kind.status_code == 422
    assert wrong_kind.json()["detail"]["code"] == "ANALYSIS_ASSET_NOT_REFERENCE_VIDEO"


def test_manual_shot_card_version_is_not_overwritten_by_new_analysis(
    client: TestClient,
    db_path: Path,
) -> None:
    first = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    ).json()

    manual_shots = first["payload"]["analysis"]["shots"]
    manual_shots[0]["action"] = "人工修订后的动作"
    manual = client.put(
        f"/api/analysis/{first['id']}/shots",
        json={"shots": manual_shots},
        headers=auth_headers("employee_1"),
    )

    second = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )

    assert manual.status_code == 200
    assert manual.json()["kind"] == "shot_card"
    assert manual.json()["version_number"] == 1

    latest_shot_card = client.get(
        "/api/projects/project_owned/shot-cards/latest",
        headers=auth_headers("employee_1"),
    )
    assert latest_shot_card.status_code == 200
    assert latest_shot_card.json()["id"] == manual.json()["id"]

    assert second.status_code == 200
    assert second.json()["version_number"] == 2

    with connect_database(db_path) as conn:
        rows = conn.execute(
            """
            SELECT kind, version_number, payload_json
            FROM versions
            WHERE project_id = ?
            ORDER BY kind, version_number
            """,
            ("project_owned",),
        ).fetchall()

    assert [(row["kind"], row["version_number"]) for row in rows] == [
        ("analysis", 1),
        ("analysis", 2),
        ("shot_card", 1),
    ]
    shot_card_payload = json.loads(rows[2]["payload_json"])
    assert shot_card_payload["shots"][0]["action"] == "人工修订后的动作"


def test_analysis_accepts_a_duration_within_the_upload_rounding_tolerance(
    client: TestClient,
    db_path: Path,
) -> None:
    """Uploads tolerate 15.1s, so analysis must not reject 15.01–15.10s as invalid input."""
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE assets SET metadata_json = ? WHERE id = ?",
            (json.dumps({"duration_seconds": 15.05}), "asset_owned"),
        )
        conn.commit()

    response = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 15.05, "reuse_existing": True},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    assert response.json()["payload"]["analysis"]["duration_seconds"] == 15.05


def test_urllib_transport_marks_a_network_failure_as_retryable_without_leaking_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_url_error(*_args: object, **_kwargs: object) -> object:
        raise URLError("failed to reach https://storage.example/video.mp4?signature=secret")

    monkeypatch.setattr("app.analysis.urlopen", raise_url_error)
    transport = UrllibApilioChatTransport()

    with pytest.raises(AnalysisProviderFailed) as exc_info:
        transport.post(
            "https://api.apilio.ai/v1/chat/completions",
            headers={"Authorization": "Bearer analysis-secret"},
            body=b"{}",
        )

    failure = exc_info.value
    assert failure.failure_phase == NETWORK_FAILURE_PHASE
    assert failure.retryable is True
    assert "URLError" in str(failure)
    assert "signature=secret" not in str(failure)
    assert "analysis-secret" not in str(failure)


def test_urllib_transport_marks_upstream_server_errors_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_http_error(*_args: object, **_kwargs: object) -> object:
        raise HTTPError("https://api.apilio.ai", 503, "unavailable", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("app.analysis.urlopen", raise_http_error)
    transport = UrllibApilioChatTransport()

    with pytest.raises(AnalysisProviderFailed) as exc_info:
        transport.post("https://api.apilio.ai", headers={}, body=b"{}")

    assert exc_info.value.failure_phase == HTTP_FAILURE_PHASE
    assert exc_info.value.retryable is True
    assert exc_info.value.http_status == 503


def test_urllib_transport_marks_client_errors_as_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_http_error(*_args: object, **_kwargs: object) -> object:
        raise HTTPError("https://api.apilio.ai", 401, "unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("app.analysis.urlopen", raise_http_error)
    transport = UrllibApilioChatTransport()

    with pytest.raises(AnalysisProviderFailed) as exc_info:
        transport.post("https://api.apilio.ai", headers={}, body=b"{}")

    assert exc_info.value.failure_phase == HTTP_FAILURE_PHASE
    assert exc_info.value.retryable is False


def test_apilio_gemini_marks_an_unreadable_response_as_a_response_failure() -> None:
    provider = ApilioGemini(
        api_key="analysis-secret",
        transport=RecordedApilioTransport(b'{"choices":[]}'),
    )

    with pytest.raises(AnalysisProviderFailed) as exc_info:
        provider.analyze(video_uri="https://storage.example/video.mp4", duration_seconds=10)

    assert exc_info.value.failure_phase == RESPONSE_FAILURE_PHASE
    assert exc_info.value.retryable is False


class FailingProvider:
    requires_https_video_url = False

    def __init__(self, failure: AnalysisProviderFailed) -> None:
        self.failure = failure

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
        raise self.failure

    def repair_json(self, *, invalid_json: str, error: str) -> ProviderResponse:
        raise self.failure


def start_analysis_with_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: AnalysisProviderFailed,
) -> object:
    monkeypatch.setattr(
        "app.analysis_routes.get_video_analysis_provider",
        lambda _conn: FailingProvider(failure),
    )
    return client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )


def test_analysis_maps_a_network_failure_to_a_retryable_unreachable_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = start_analysis_with_failure(
        client,
        monkeypatch,
        AnalysisProviderFailed(
            "Apilio video analysis request failed (URLError)",
            failure_phase=NETWORK_FAILURE_PHASE,
            retryable=True,
        ),
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "ANALYSIS_PROVIDER_UNREACHABLE"
    assert detail["retryable"] is True
    assert detail["failure_phase"] == NETWORK_FAILURE_PHASE
    assert detail["message"].strip()
    assert "URLError" not in response.text


def test_analysis_maps_an_invalid_provider_response_to_a_non_retryable_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = start_analysis_with_failure(
        client,
        monkeypatch,
        AnalysisProviderFailed(
            "Provider returned invalid JSON even after a repair attempt",
            failure_phase=RESPONSE_FAILURE_PHASE,
            retryable=False,
        ),
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "ANALYSIS_PROVIDER_FAILED"
    assert detail["retryable"] is False
    assert detail["message"].strip()


def test_analysis_maps_upstream_rate_limiting_to_a_retryable_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = start_analysis_with_failure(
        client,
        monkeypatch,
        AnalysisProviderFailed(
            "Apilio returned HTTP 429",
            http_status=429,
            failure_phase=HTTP_FAILURE_PHASE,
            retryable=True,
        ),
    )

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == "ANALYSIS_PROVIDER_RATE_LIMITED"
    assert detail["retryable"] is True
    assert detail["message"].strip()
