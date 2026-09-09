"""本机媒体工具：ffmpeg/ffprobe 定位与音轨抽取。

客户版桌面部署把精简构建的 ffmpeg/ffprobe 随 NSIS 安装包分发到
``resources/ffmpeg/``（服务端启动脚本负责设置 ``VIDEO_REPLICA_FFMPEG_DIR``）。
解析顺序：环境变量目录 → PATH；两者皆失败时抛出可识别错误，任务
fail-closed，不静默降级到其它解析通道。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FFMPEG_DIR_ENV = "VIDEO_REPLICA_FFMPEG_DIR"
FFMPEG_TIMEOUT_SECONDS = 300
IMAGE_DECODE_TIMEOUT_SECONDS = 15
logger = logging.getLogger(__name__)


class MediaToolUnavailable(RuntimeError):
    """ffmpeg/ffprobe binary could not be located."""


class MediaToolFailed(RuntimeError):
    """ffmpeg/ffprobe ran but returned a non-zero exit."""


class MediaValidationFailed(RuntimeError):
    """Uploaded or provider-returned bytes are not decodable media."""


@dataclass(frozen=True)
class MediaInspection:
    media_type: Literal["image", "video", "audio"]
    duration_seconds: float | None
    width: int | None = None
    height: int | None = None


def resolve_media_binary(tool: str) -> str:
    """Locate ``ffmpeg``/``ffprobe``; env dir wins, then PATH."""
    env_dir = os.environ.get(FFMPEG_DIR_ENV, "").strip()
    if env_dir:
        for suffix in (".exe", ""):
            candidate = Path(env_dir) / f"{tool}{suffix}"
            if candidate.is_file():
                return str(candidate)
    located = shutil.which(tool)
    if located:
        return located
    raise MediaToolUnavailable(f"未找到 {tool}，请确认安装包完整或配置 {FFMPEG_DIR_ENV}。")


def extract_audio(ffmpeg_path: str, video_path: Path, audio_path: Path) -> None:
    """单声道 16kHz 低码率 AAC 音轨，足够转写且远小于原视频。"""
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        str(audio_path),
    ]
    _run(command)


def probe_duration_seconds(ffprobe_path: str, media_path: Path) -> float | None:
    """容器时长（秒）；探测失败返回 None，由 provider 自行选择模式。"""
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        output = subprocess.run(
            command,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=True,
        ).stdout
        duration = json.loads(output.decode("utf-8"))["format"]["duration"]
        return float(duration)
    except (subprocess.SubprocessError, KeyError, ValueError, OSError):
        return None


def inspect_media_bytes(
    content: bytes,
    *,
    suffix: str,
    expected_type: Literal["image", "video", "audio"],
    min_duration_seconds: float | None = None,
    max_duration_seconds: float | None = None,
) -> MediaInspection:
    """Decode-probe untrusted media bytes and enforce the expected stream type."""
    if not content:
        raise MediaValidationFailed("媒体文件为空")
    ffprobe_path = resolve_media_binary("ffprobe")
    with tempfile.TemporaryDirectory(prefix="video-replica-probe-") as temp_dir:
        media_path = Path(temp_dir) / f"input{suffix}"
        try:
            media_path.write_bytes(content)
        except OSError as exc:
            raise MediaToolFailed("媒体临时文件写入失败") from exc
        command = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,duration:format=duration,format_name",
            "-of",
            "json",
            str(media_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise MediaToolFailed(f"ffprobe 执行失败：{type(exc).__name__}") from exc
    if completed.returncode != 0:
        logger.warning("media decode validation failed: expected=%s", expected_type)
        raise MediaValidationFailed("媒体文件无法解码")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            raise ValueError("invalid streams")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MediaValidationFailed("媒体探测结果无效") from exc

    typed_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == expected_type
    ]
    if expected_type in {"image", "video"}:
        video_streams = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ]
        image_codecs = {"apng", "bmp", "gif", "mjpeg", "png", "tiff", "webp"}
        if expected_type == "image":
            typed_streams = [
                stream for stream in video_streams if stream.get("codec_name") in image_codecs
            ]
        else:
            typed_streams = [
                stream for stream in video_streams if stream.get("codec_name") not in image_codecs
            ]
    if not typed_streams:
        raise MediaValidationFailed("媒体流类型不匹配")

    duration = _media_duration(payload, streams)
    if min_duration_seconds is not None and (duration is None or duration < min_duration_seconds):
        raise MediaValidationFailed("媒体时长过短")
    if max_duration_seconds is not None and (duration is None or duration > max_duration_seconds):
        raise MediaValidationFailed("媒体时长过长")
    first = typed_streams[0]
    width = _positive_int(first.get("width"))
    height = _positive_int(first.get("height"))
    if expected_type == "image" and (width is None or height is None):
        raise MediaValidationFailed("图片尺寸无效")
    return MediaInspection(
        media_type=expected_type,
        duration_seconds=duration,
        width=width,
        height=height,
    )


def normalize_image_to_png(content: bytes) -> bytes:
    """Decode one provider image frame and return a canonical PNG."""
    ffmpeg_path = resolve_media_binary("ffmpeg")
    command = [
        ffmpeg_path,
        "-v",
        "error",
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            input=content,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise MediaToolFailed(f"ffmpeg 执行失败：{type(exc).__name__}") from exc
    if completed.returncode != 0 or not completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        logger.warning("provider image decode validation failed")
        raise MediaValidationFailed("图片无法解码")
    return completed.stdout


def _media_duration(payload: object, streams: list[object]) -> float | None:
    candidates: list[object] = []
    if isinstance(payload, dict):
        format_payload = payload.get("format")
        if isinstance(format_payload, dict):
            candidates.append(format_payload.get("duration"))
    candidates.extend(stream.get("duration") for stream in streams if isinstance(stream, dict))
    parsed = [_positive_float(value) for value in candidates]
    values = [value for value in parsed if value is not None]
    return max(values) if values else None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validate_image_decodable(ffmpeg_path: str, content: bytes) -> None:
    """Decode one image frame in an isolated ffmpeg process without writing output."""
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-threads",
        "1",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            input=content,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=IMAGE_DECODE_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("ffmpeg image validation failed to run: %s", type(exc).__name__)
        raise MediaToolFailed("ffmpeg 图片解码校验执行失败") from exc
    if completed.returncode != 0:
        raise MediaToolFailed("ffmpeg 无法解码图片")


def _run(command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise MediaToolFailed(f"{Path(command[0]).name} 执行失败：{type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise MediaToolFailed(f"{Path(command[0]).name} 返回非零退出码")
