"""图片尺寸解析的共享实现。

2026-10-31 前有三份各自维护的 JPEG SOF 扫描（资产质检 / 人物源图 / 首帧），
遍历细节（段跳步、SOS 处理）与失败契约（ValueError / 422 / None）各不相同；
其中两份扫描会越过 SOS 进入熵编码数据，存在读到假 SOF 的隐患。此处提供
唯一扫描实现，调用方用薄适配层保留各自的失败契约与校验行为。
"""

from __future__ import annotations

import struct


def scan_jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    """扫描 JPEG 的 SOF 段返回 ``(宽, 高)``；无法可靠解析时返回 ``None``。

    采用最严格的遍历：校验 SOI 魔数、按段长逐段跳步、SOS/EOI 即停——
    尺寸段必然位于熵编码数据之前，越过 SOS 继续扫描只会命中假标记。
    """
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 <= len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker in {0xD9, 0xDA}:
            return None
        segment_length = struct.unpack(">H", content[offset + 2 : offset + 4])[0]
        if segment_length < 2 or offset + 2 + segment_length > len(content):
            return None
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            height, width = struct.unpack(">HH", content[offset + 5 : offset + 9])
            return width, height
        offset += 2 + segment_length
    return None
