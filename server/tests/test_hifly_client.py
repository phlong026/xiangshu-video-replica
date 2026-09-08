from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError

import pytest
from cryptography.fernet import Fernet

from app.db import initialize_database
from app.db_portable import BusinessConnection
from app.hifly import (
    HIFLY_BASE_URL,
    HiflyClient,
    HiflyError,
    HiflySettingsUnavailable,
    HiflySubmissionUncertain,
    HiflyTimeoutError,
    UrllibHiflyHttpTransport,
    hifly_client_from_config,
)
from app.settings import (
    REQUIRED_PROVIDER_FIELDS,
    SettingsRepository,
    normalize_provider,
)

# ---------------------------------------------------------------------------
# Provider registration (settings registry must know hifly)
# ---------------------------------------------------------------------------


def test_hifly_is_a_registered_provider_with_api_key_requirement() -> None:
    assert normalize_provider("hifly") == "hifly"
    assert REQUIRED_PROVIDER_FIELDS["hifly"] == ("api_key",)


def seed_users(connection: sqlite3.Connection) -> None:
    # provider_settings.updated_by_user_id references users.id.
    connection.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [("admin_1", "admin_1", "Admin One", "admin")],
    )
    connection.commit()


def hifly_settings_connection(tmp_path: Path, name: str) -> sqlite3.Connection:
    connection = initialize_database(tmp_path / name)
    seed_users(connection)
    return connection


def test_hifly_provider_config_roundtrip_through_encrypted_storage(
    tmp_path: Path,
) -> None:
    fernet = Fernet(Fernet.generate_key())
    connection = hifly_settings_connection(tmp_path, "hifly-settings.db")
    try:
        repo = SettingsRepository(BusinessConnection.sqlite(connection), fernet=fernet)
        repo.save_provider_config("hifly", {"api_key": "hifly-test-key"}, actor_user_id="admin_1")
        loaded = repo.load_provider_config("hifly")
    finally:
        connection.close()
    assert loaded["api_key"] == "hifly-test-key"


# ---------------------------------------------------------------------------
# Client contract against the vendor envelope
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass
class FakeHiflyTransport:
    response_body: bytes = b'{"code": 0, "msg": "", "data": {}}'
    requests: list[RecordedRequest] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        self.requests.append(RecordedRequest(method=method, url=url, headers=headers, body=body))
        return self.response_body


def client_with(body: bytes) -> tuple[HiflyClient, FakeHiflyTransport]:
    transport = FakeHiflyTransport(response_body=body)
    return HiflyClient(api_key="hifly-test-key", transport=transport), transport


def test_create_video_by_tts_posts_bearer_auth_and_vendor_envelope() -> None:
    client, transport = client_with(b'{"code": 0, "msg": "", "data": {"task_id": "task-1"}}')

    task_id = client.create_video_by_tts(
        voice="voice-1",
        text="大家好，今天带大家看一套乡墅。",
        avatar="avatar-1",
        title="乡墅口播",
        aigc_flag=True,
    )

    assert task_id == "task-1"
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == f"{HIFLY_BASE_URL}/api/v2/hifly/video/create_by_tts"
    assert request.headers["Authorization"] == "Bearer hifly-test-key"
    payload = json.loads(request.body or b"{}")
    assert payload["voice"] == "voice-1"
    assert payload["text"] == "大家好，今天带大家看一套乡墅。"
    assert payload["avatar"] == "avatar-1"
    assert payload["title"] == "乡墅口播"
    assert payload["aigc_flag"] is True
    assert "st_show" not in payload


def test_create_video_by_tts_carries_subtitle_params_only_when_enabled() -> None:
    client, transport = client_with(b'{"code": 0, "msg": "", "data": {"task_id": "task-2"}}')

    client.create_video_by_tts(
        voice="voice-1",
        text="文案",
        avatar="avatar-1",
        title="字幕口播",
        aigc_flag=True,
        subtitle={
            "st_show": True,
            "st_font_size": 30,
            "st_primary_color": "0xFFFFFF",
        },
    )

    payload = json.loads(transport.requests[0].body or b"{}")
    assert payload["st_show"] is True
    assert payload["st_font_size"] == 30
    assert payload["st_primary_color"] == "0xFFFFFF"


def test_vendor_business_error_maps_to_read_message_and_code() -> None:
    client, _ = client_with(b'{"code": 1002, "msg": "credit not enough", "data": {}}')

    with pytest.raises(HiflyError) as excinfo:
        client.create_video_by_tts(
            voice="voice-1", text="文案", avatar="avatar-1", title="t", aigc_flag=True
        )
    assert excinfo.value.vendor_code == 1002
    assert "余额不足" in str(excinfo.value)
    # 客户可见文案不允许出现供应商名称（红线）。


def test_invalid_token_error_is_reported_as_configuration_problem() -> None:
    client, _ = client_with(b'{"code": 2003, "msg": "token invalid", "data": {}}')

    with pytest.raises(HiflyError) as excinfo:
        client.account_credit()
    assert excinfo.value.vendor_code == 2003
    assert "未正确配置" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b'{"code": null, "data": {}}',
        b'{"code": "ERROR", "data": {}}',
        b'{"code": false, "data": {"credit": 88}}',
        b'{"code": 0.5, "data": {"credit": 88}}',
        b'{"code": 1e309, "data": {"credit": 88}}',
        b'{"code": ' + (b"9" * 4301) + b', "data": {"credit": 88}}',
        b'{"code": "' + (b"9" * 4301) + b'", "data": {"credit": 88}}',
        b'{"code": 0, "data": {"credit": true}}',
        b'{"code": 0, "data": {"credit": -1}}',
        b'{"code": 0, "data": {"credit": 9007199254740992}}',
        b'{"code": 0, "data": {"credit": ' + (b"9" * 310) + b"}}",
    ],
)
def test_account_credit_rejects_malformed_vendor_payloads(payload: bytes) -> None:
    client, _ = client_with(payload)

    with pytest.raises(HiflyError):
        client.account_credit()


def test_urllib_transport_preserves_timeout_as_a_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr("app.hifly.urlopen", timeout)

    with pytest.raises(HiflyTimeoutError):
        UrllibHiflyHttpTransport(timeout_seconds=0.01).request(
            "GET",
            f"{HIFLY_BASE_URL}/api/v2/hifly/account/credit",
            headers={"Authorization": "Bearer dummy-hifly-token"},
        )


def test_urllib_transport_preserves_http_status_for_auth_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unauthorized(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            f"{HIFLY_BASE_URL}/api/v2/hifly/account/credit",
            401,
            "Unauthorized",
            {},  # type: ignore[arg-type]
            None,
        )

    monkeypatch.setattr("app.hifly.urlopen", unauthorized)

    with pytest.raises(HiflyError) as caught:
        UrllibHiflyHttpTransport().request(
            "GET",
            f"{HIFLY_BASE_URL}/api/v2/hifly/account/credit",
            headers={"Authorization": "Bearer dummy-hifly-token"},
        )

    assert caught.value.http_status == 401


def test_avatar_task_normalizes_vendor_status() -> None:
    client, _ = client_with(b'{"code": 0, "msg": "", "data": {"status": 3, "avatar_id": "av-9"}}')

    snapshot = client.avatar_task("task-9")

    assert snapshot.status == "DONE"
    assert snapshot.avatar_id == "av-9"
    assert snapshot.raw["status"] == 3


def test_unknown_vendor_task_status_is_fail_safe() -> None:
    client, _ = client_with(b'{"code": 0, "msg": "", "data": {"status": 99, "avatar_id": "av-9"}}')

    snapshot = client.avatar_task("task-9")

    assert snapshot.status == "UNKNOWN"


def test_video_task_reads_temporary_video_url() -> None:
    body = json.dumps(
        {
            "code": 0,
            "msg": "",
            "data": {
                "status": 3,
                "video_Url": "https://tmp.example/v.mp4",
                "duration": 32,
            },
        }
    ).encode()
    client, transport = client_with(body)

    snapshot = client.video_task("task-3")

    assert snapshot.status == "DONE"
    assert snapshot.video_url == "https://tmp.example/v.mp4"
    assert snapshot.duration == 32
    request = transport.requests[0]
    assert request.url == f"{HIFLY_BASE_URL}/api/v2/hifly/video/task?task_id=task-3"


def test_create_upload_url_returns_target_and_file_id() -> None:
    body = json.dumps(
        {
            "code": 0,
            "msg": "",
            "data": {
                "upload_url": "https://up.example",
                "content_type": "audio/mpeg",
                "file_id": "file-1",
            },
        }
    ).encode()
    client, transport = client_with(body)

    target = client.create_upload_url("mp3")

    assert target.upload_url == "https://up.example"
    assert target.content_type == "audio/mpeg"
    assert target.file_id == "file-1"
    payload = json.loads(transport.requests[0].body or b"{}")
    assert payload == {"file_extension": "mp3"}


def test_account_credit_returns_left_credits() -> None:
    client, _ = client_with(b'{"code": 0, "msg": "", "data": {"credit": 42}}')

    assert client.account_credit() == 42


def test_account_credit_accepts_the_javascript_safe_integer_boundary() -> None:
    client, _ = client_with(b'{"code": 0, "msg": "", "data": {"credit": 9007199254740991}}')

    assert client.account_credit() == 9_007_199_254_740_991


def test_list_end_points_pass_pagination_and_kind() -> None:
    client, transport = client_with(b'{"code": 0, "msg": "", "data": {"list": []}}')

    client.list_avatars(page=2, size=10)
    client.list_voices(page=1, size=20, kind=1)

    assert transport.requests[0].url == (
        f"{HIFLY_BASE_URL}/api/v2/hifly/avatar/list?page=2&size=10&kind=2"
    )
    assert transport.requests[1].url == (
        f"{HIFLY_BASE_URL}/api/v2/hifly/voice/list?page=1&size=20&kind=1"
    )


def test_local_parameter_validation_rejects_obvious_misuse() -> None:
    client, transport = client_with(b'{"code": 0, "msg": "", "data": {"task_id": "t"}}')

    with pytest.raises(ValueError, match="20"):
        client.create_avatar_by_video(
            title="x" * 21, video_url="https://v.example/a.mp4", aigc_flag=True
        )
    with pytest.raises(ValueError, match="video_url"):
        client.create_avatar_by_video(title="标题", aigc_flag=True)
    with pytest.raises(ValueError, match="10000"):
        client.create_video_by_tts(
            voice="v", text="x" * 10_001, avatar="a", title="t", aigc_flag=True
        )
    with pytest.raises(ValueError, match="file_extension"):
        client.create_upload_url("")
    # 校验失败不允许发出请求
    assert transport.requests == []


def test_client_from_config_requires_api_key() -> None:
    with pytest.raises(HiflySettingsUnavailable):
        hifly_client_from_config({})
    with pytest.raises(HiflySettingsUnavailable):
        hifly_client_from_config({"api_key": "   "})
    assert hifly_client_from_config({"api_key": "k"}).api_key == "k"


def test_transport_failure_raises_hifly_error() -> None:
    class BrokenTransport:
        def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes | None = None,
        ) -> bytes:
            raise OSError("connection refused")

    client = HiflyClient(api_key="k", transport=BrokenTransport())
    with pytest.raises(HiflyError, match="网络异常"):
        client.account_credit()


def test_creation_transport_failure_is_submission_uncertain() -> None:
    class BrokenTransport:
        def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes | None = None,
        ) -> bytes:
            raise OSError("connection reset after POST")

    client = HiflyClient(api_key="k", transport=BrokenTransport())

    with pytest.raises(HiflySubmissionUncertain, match="网络异常"):
        client.create_video_by_tts(
            voice="voice", text="文案", avatar="avatar", title="t", aigc_flag=True
        )


def test_known_creation_rejection_is_not_submission_uncertain() -> None:
    client, _ = client_with(b'{"code": 1002, "msg": "credit", "data": {}}')

    with pytest.raises(HiflyError) as excinfo:
        client.create_video_by_tts(
            voice="voice", text="文案", avatar="avatar", title="t", aigc_flag=True
        )

    assert not isinstance(excinfo.value, HiflySubmissionUncertain)
    assert excinfo.value.vendor_code == 1002


def test_settings_repository_loading_wires_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.hifly import hifly_client_from_settings

    # hifly_client_from_settings builds its repository from the environment
    # key, so both the save and the load sides must share it.
    settings_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_key)
    fernet = Fernet(settings_key.encode())
    connection = hifly_settings_connection(tmp_path, "hifly-settings-wiring.db")
    try:
        conn = BusinessConnection.sqlite(connection)
        with pytest.raises(HiflySettingsUnavailable):
            hifly_client_from_settings(conn)
        SettingsRepository(conn, fernet=fernet).save_provider_config(
            "hifly", {"api_key": "configured-key"}, actor_user_id="admin_1"
        )
        client = hifly_client_from_settings(conn)
    finally:
        connection.close()
    assert client.api_key == "configured-key"


def test_customer_visible_messages_never_leak_the_vendor_name() -> None:
    """红线：客户 UI 任何文案不允许出现 API 供应商名称。"""
    client, _ = client_with(b'{"code": 1002, "msg": "credit", "data": {}}')
    with pytest.raises(HiflyError) as excinfo:
        client.create_video_by_tts(voice="v", text="文案", avatar="a", title="t", aigc_flag=True)
    assert "飞影" not in str(excinfo.value)
    assert "hifly" not in str(excinfo.value).lower()
