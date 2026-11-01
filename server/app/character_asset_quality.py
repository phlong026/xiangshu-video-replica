from __future__ import annotations

import hashlib
import struct

from app.character_contracts import RequiredCharacterViewType
from app.image_dimensions import scan_jpeg_dimensions

CHARACTER_QUALITY_SCHEMA_VERSION = "character-quality.v1"


def inspect_character_asset(
    content: bytes,
    *,
    view_type: RequiredCharacterViewType,
) -> dict[str, object]:
    """产出确定性的自动质检信号；simulated 标记如实标注这不是真实语义质检。

    评分由内容哈希派生，同一批候选的结果稳定可复现；尺寸一项按真实字节解析，
    网关可能返回 PNG/JPEG/WebP 三种格式，三种都要能通过，不能只认 PNG。
    """
    width, height = image_dimensions(content)
    digest = hashlib.sha256(content).digest()
    identity_score = round(0.9 + digest[0] / 2550, 3)
    costume_score = round(0.88 + digest[1] / 2550, 3)
    checks = {
        "body_proportions": "PASS",
        "dimensions": "PASS" if width >= 1024 and height >= 1024 else "WARN",
        "limb_integrity": "PASS",
        "person_count": "PASS",
        "sharpness": "PASS",
        "text_or_watermark": "PASS",
        "truncation": "PASS",
        "view_type_match": "PASS",
    }
    return {
        "blocking_issue_codes": [],
        "checks": checks,
        "dimensions": {"height": height, "width": width},
        "inspector": {"model": "fake-quality-v1", "provider": "fake_character_quality"},
        "schema_version": CHARACTER_QUALITY_SCHEMA_VERSION,
        "scores": {
            "costume_consistency": costume_score,
            "identity_consistency": identity_score,
        },
        "simulated": True,
        "view_type": view_type,
    }


def image_dimensions(content: bytes) -> tuple[int, int]:
    """按魔数分派尺寸解析；无法识别的字节抛 ValueError，由调用方转无效响应。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return png_dimensions(content)
    if content.startswith(b"\xff\xd8"):
        return jpeg_dimensions(content)
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return webp_dimensions(content)
    raise ValueError("unsupported image content")


def png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        raise ValueError("invalid PNG content")
    return struct.unpack(">II", content[16:24])


def jpeg_dimensions(content: bytes) -> tuple[int, int]:
    """SOF 尺寸扫描走共享实现（image_dimensions），失败维持 ValueError 契约。"""
    dimensions = scan_jpeg_dimensions(content)
    if dimensions is None:
        raise ValueError("invalid JPEG content")
    return dimensions


def webp_dimensions(content: bytes) -> tuple[int, int]:
    """解析 WebP 三种容器的画布尺寸：VP8X 扩展 / VP8L 无损 / VP8 有损。"""
    if len(content) < 25:
        raise ValueError("invalid WebP content")
    chunk = content[12:16]
    if chunk == b"VP8X":
        if len(content) < 30:
            raise ValueError("invalid WebP content")
        width = int.from_bytes(content[24:27], "little") + 1
        height = int.from_bytes(content[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L":
        if content[20] != 0x2F:
            raise ValueError("invalid WebP content")
        bits = struct.unpack("<I", content[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        if len(content) < 30 or content[23:26] != b"\x9d\x01\x2a":
            raise ValueError("invalid WebP content")
        width = struct.unpack("<H", content[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", content[28:30])[0] & 0x3FFF
        return width, height
    raise ValueError("unsupported WebP content")
