"""飞影 V2 文档上限对齐：口播素材分档大小（纯常量断言，无数据库依赖）。

文档约定：
- 视频分身 create_by_video ≤500MB
- 音频驱动 create_by_audio ≤100MB
- 图片分身按图片素材 10MB
- 上传阶段 oral_audio 用途 ≤100MB（与创建层同值，避免"上传成功却建任务失败"）
"""

from __future__ import annotations

from app.materials import ORAL_AUDIO_UPLOAD_LIMIT, VOICE_CLONE_UPLOAD_LIMIT
from app.oral import ORAL_SOURCE_MAX_BYTES

_MB = 1024 * 1024


def test_oral_source_max_bytes_follows_vendor_doc_tiers() -> None:
    assert ORAL_SOURCE_MAX_BYTES["video"] == 500 * _MB
    assert ORAL_SOURCE_MAX_BYTES["audio"] == 100 * _MB
    assert ORAL_SOURCE_MAX_BYTES["image"] == 10 * _MB


def test_oral_audio_upload_limit_matches_creation_tier() -> None:
    """上传层与创建层同值：不会出现上传通过但建任务被拒的大小区间。"""
    assert ORAL_AUDIO_UPLOAD_LIMIT == ORAL_SOURCE_MAX_BYTES["audio"] == 100 * _MB


def test_voice_clone_limit_unchanged_at_twenty_megabytes() -> None:
    assert VOICE_CLONE_UPLOAD_LIMIT == 20 * _MB
