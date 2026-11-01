"""上传时长门禁按用途分流（裁剪爆款 4–15s / 提取文案放开上限 / 长视频提示词 4–180s）。

>15s 的视频可用的下游场景是提取文案与长视频复刻提示词（复刻被 15 秒门禁拦住），
所以 `purpose=script` 的上传在探测期只卡下限，`purpose=replica_prompt` 卡 4–180 秒；
复刻侧的时长守护由分析入口（`analysis_duration_for_asset`）兜底，长视频侧由
replica_prompt 任务入口兜底，本文件不重复覆盖。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastapi import HTTPException

from app.media import (
    PreparedUploadCompletion,
    asset_kind_for_content_type,
    probe_upload_completion,
    storage_key_from_uri,
    validate_duration,
    validate_upload_request,
)
from app.storage import FakeStorageAdapter


class _FixedProbe:
    """返回固定时长的假探测器：宽高为空，模拟纯音频/无视频流输入。"""

    def __init__(self, duration_seconds: float) -> None:
        self.duration_seconds = duration_seconds

    def probe(self, content: bytes, *, filename: str) -> Any:
        from app.media import VideoMetadata

        return VideoMetadata(duration_seconds=self.duration_seconds)


def _prepared(purpose: str, storage: FakeStorageAdapter) -> PreparedUploadCompletion:
    key = "projects/p-1/uploads/asset-1/source.mp4"
    storage.put_object(key, b"fake-media-bytes", content_type="video/mp4")
    return PreparedUploadCompletion(
        asset_id="asset-1",
        project_id="p-1",
        storage_uri=f"fake://test-bucket/{key}",
        storage_key=key,
        content_type="video/mp4",
        purpose=purpose,
    )


@pytest.mark.parametrize(
    ("duration", "purpose", "expect_error"),
    [
        (10.0, "replica", False),
        # 30s + 0.1s 容差以内仍放行，与复刻分析入口的上限一致。
        (30.05, "replica", False),
        (30.2, "replica", True),
        (3.8, "replica", True),
        # 提取文案：上限放开，下限保留（太短的素材没有转写价值，也防损坏文件）。
        (60.0, "script", False),
        (600.0, "script", False),
        (3.8, "script", True),
        # 长视频复刻提示词：4–180 秒（+0.1s 容差）。
        (60.0, "replica_prompt", False),
        (180.05, "replica_prompt", False),
        (180.2, "replica_prompt", True),
        (3.8, "replica_prompt", True),
    ],
)
def test_validate_duration_splits_by_purpose(
    duration: float, purpose: str, expect_error: bool
) -> None:
    if not expect_error:
        validate_duration(duration, purpose=purpose)
        return
    with pytest.raises(HTTPException) as failure:
        validate_duration(duration, purpose=purpose)
    assert failure.value.detail["code"] == "VIDEO_DURATION_OUT_OF_RANGE"


def test_validate_duration_error_copy_matches_scenario() -> None:
    with pytest.raises(HTTPException) as replica_failure:
        validate_duration(40.0)
    assert "4–30 秒" in replica_failure.value.detail["message"]

    with pytest.raises(HTTPException) as script_failure:
        validate_duration(2.0, purpose="script")
    # 放开上限后报错只谈下限，不得再出现 15 秒字样误导用户。
    assert "15" not in script_failure.value.detail["message"]
    assert "不少于 4 秒" in script_failure.value.detail["message"]

    with pytest.raises(HTTPException) as prompt_failure:
        validate_duration(200.0, purpose="replica_prompt")
    assert "4–180 秒" in prompt_failure.value.detail["message"]


def test_upload_byte_budget_follows_purpose() -> None:
    from app.media import upload_byte_budget

    assert upload_byte_budget("replica") == upload_byte_budget("script")
    assert upload_byte_budget("replica_prompt") > upload_byte_budget("replica")


def test_validate_upload_request_rejects_generic_budget_for_replica() -> None:
    with pytest.raises(HTTPException) as failure:
        validate_upload_request(
            filename="a.mp4",
            content_type="video/mp4",
            size_bytes=300 * 1024 * 1024,
            purpose="replica",
        )
    assert failure.value.detail["code"] == "MEDIA_TOO_LARGE"
    assert "200MB" in failure.value.detail["message"]


def test_validate_upload_request_allows_prompt_budget_for_replica_prompt() -> None:
    validate_upload_request(
        filename="a.mp4",
        content_type="video/mp4",
        size_bytes=300 * 1024 * 1024,
        purpose="replica_prompt",
    )


@pytest.mark.parametrize(
    ("purpose", "content_type", "filename", "expect_error"),
    [
        ("replica", "video/mp4", "a.mp4", False),
        ("script", "video/mp4", "a.mp4", False),
        ("replica_prompt", "video/mp4", "a.mp4", False),
        # 复刻通道不收音频；提取文案通道收（桌面端本地抽出的 m4a 也走这里）。
        ("replica", "audio/mp4", "a.m4a", True),
        ("replica_prompt", "audio/mp4", "a.m4a", True),
        ("script", "audio/mp4", "a.m4a", False),
        ("script", "audio/mpeg", "a.mp3", False),
        ("script", "audio/x-m4a", "a.m4a", False),
        # 音频后缀必须配音频类型，防止把任意二进制改名为 .m4a 混进来。
        ("script", "video/mp4", "a.m4a", True),
    ],
)
def test_validate_upload_request_accepts_audio_only_for_script(
    purpose: str, content_type: str, filename: str, expect_error: bool
) -> None:
    kwargs = {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": 1024,
    }
    if expect_error:
        with pytest.raises(HTTPException) as failure:
            validate_upload_request(purpose=purpose, **kwargs)
        assert failure.value.detail["code"] == "UNSUPPORTED_MEDIA_TYPE"
        return
    validate_upload_request(purpose=purpose, **kwargs)


def test_asset_kind_follows_content_type() -> None:
    assert asset_kind_for_content_type("audio/mp4") == "reference_audio"
    assert asset_kind_for_content_type("video/mp4") == "reference_video"
    assert asset_kind_for_content_type(None) == "reference_video"


def test_probe_completion_allows_long_video_for_script(monkeypatch) -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="test-bucket")
    monkeypatch.setattr("app.media.store_video_thumbnail", lambda *args, **kwargs: None)
    probed = probe_upload_completion(
        _prepared("script", storage),
        storage=storage,
        probe=_FixedProbe(45.0),
    )
    assert probed.metadata.duration_seconds == 45.0


def test_probe_completion_still_rejects_long_video_for_replica(monkeypatch) -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="test-bucket")
    monkeypatch.setattr("app.media.store_video_thumbnail", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as failure:
        probe_upload_completion(
            _prepared("replica", storage),
            storage=storage,
            probe=_FixedProbe(45.0),
        )
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == "VIDEO_DURATION_OUT_OF_RANGE"


def test_probe_completion_accepts_long_video_for_replica_prompt(monkeypatch) -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="test-bucket")
    monkeypatch.setattr("app.media.store_video_thumbnail", lambda *args, **kwargs: None)
    probed = probe_upload_completion(
        _prepared("replica_prompt", storage),
        storage=storage,
        probe=_FixedProbe(120.0),
    )
    assert probed.metadata.duration_seconds == 120.0


def test_probe_completion_rejects_over_limit_video_for_replica_prompt(monkeypatch) -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="test-bucket")
    monkeypatch.setattr("app.media.store_video_thumbnail", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as failure:
        probe_upload_completion(
            _prepared("replica_prompt", storage),
            storage=storage,
            probe=_FixedProbe(200.0),
        )
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == "VIDEO_DURATION_OUT_OF_RANGE"
    assert "4–180 秒" in failure.value.detail["message"]


def test_probe_completion_streams_oversized_object_via_temp_file(monkeypatch, tmp_path) -> None:
    """>200MB 的对象必须走临时文件流式路径：哈希正确、缩略图失败不阻塞、落 verified 键."""
    from app import media as media_module

    # 把通用预算压小，让一个 256 字节的假对象就能触发大文件分支，避免测试真的造 200MB 数据。
    monkeypatch.setattr(media_module, "MAX_UPLOAD_BYTES", 64)
    monkeypatch.setattr(media_module, "REPLICA_PROMPT_MAX_UPLOAD_BYTES", 4096)
    monkeypatch.setattr(
        media_module, "store_video_thumbnail_from_file", lambda *args, **kwargs: None
    )

    storage = FakeStorageAdapter(provider="fake", bucket="test-bucket")
    key = "projects/p-1/uploads/asset-big/source.mp4"
    payload = b"x" * 256
    storage.put_object(key, payload, content_type="video/mp4")
    prepared = PreparedUploadCompletion(
        asset_id="asset-big",
        project_id="p-1",
        storage_uri=f"fake://test-bucket/{key}",
        storage_key=key,
        content_type="video/mp4",
        purpose="replica_prompt",
    )
    probed = probe_upload_completion(prepared, storage=storage, probe=_FixedProbe(90.0))
    assert probed.metadata.duration_seconds == 90.0
    assert probed.size_bytes == len(payload)
    assert probed.sha256 == hashlib.sha256(payload).hexdigest()
    assert "/verified-uploads/" in probed.storage_uri
    # 流式路径与字节路径写入同一 verified 键空间：对象确实被复制了一份。
    assert storage.get_object(storage_key_from_uri(probed.storage_uri)) == payload
