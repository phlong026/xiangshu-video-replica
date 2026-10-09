"""上传时长门禁按用途分流（裁剪爆款 4–15s / 提取文案放开 15s 上限）。

>15s 的视频唯一可用的下游场景是提取文案（复刻被 15 秒门禁拦住），所以
`purpose=script` 的上传在探测期只卡下限；复刻侧的时长守护由分析入口
（`analysis_duration_for_asset`）兜底，本文件不重复覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.media import (
    PreparedUploadCompletion,
    asset_kind_for_content_type,
    probe_upload_completion,
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
    ("duration", "allow_long", "expect_error"),
    [
        (10.0, False, False),
        # 15s + 0.1s 容差以内仍放行，与复刻分析入口的上限一致。
        (15.05, False, False),
        (15.2, False, True),
        (3.8, False, True),
        # 提取文案：上限放开，下限保留（太短的素材没有转写价值，也防损坏文件）。
        (60.0, True, False),
        (600.0, True, False),
        (3.8, True, True),
    ],
)
def test_validate_duration_splits_by_purpose(
    duration: float, allow_long: bool, expect_error: bool
) -> None:
    if not expect_error:
        validate_duration(duration, allow_long=allow_long)
        return
    with pytest.raises(HTTPException) as failure:
        validate_duration(duration, allow_long=allow_long)
    assert failure.value.detail["code"] == "VIDEO_DURATION_OUT_OF_RANGE"


def test_validate_duration_error_copy_matches_scenario() -> None:
    with pytest.raises(HTTPException) as replica_failure:
        validate_duration(20.0)
    assert "4–15 秒" in replica_failure.value.detail["message"]

    with pytest.raises(HTTPException) as script_failure:
        validate_duration(2.0, allow_long=True)
    # 放开上限后报错只谈下限，不得再出现 15 秒字样误导用户。
    assert "15" not in script_failure.value.detail["message"]
    assert "不少于 4 秒" in script_failure.value.detail["message"]


@pytest.mark.parametrize(
    ("purpose", "content_type", "filename", "expect_error"),
    [
        ("replica", "video/mp4", "a.mp4", False),
        ("script", "video/mp4", "a.mp4", False),
        # 复刻通道不收音频；提取文案通道收（桌面端本地抽出的 m4a 也走这里）。
        ("replica", "audio/mp4", "a.m4a", True),
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
