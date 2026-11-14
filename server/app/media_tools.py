"""本机媒体工具：ffmpeg/ffprobe 定位与音轨抽取。

客户版桌面部署把精简构建的 ffmpeg/ffprobe 随 NSIS 安装包分发到
``resources/ffmpeg/``（服务端启动脚本负责设置 ``VIDEO_REPLICA_FFMPEG_DIR``）。
解析顺序：环境变量目录 → PATH；两者皆失败时抛出可识别错误，任务
fail-closed，不静默降级到其它解析通道。
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal

FFMPEG_DIR_ENV = "VIDEO_REPLICA_FFMPEG_DIR"
# 探测+转码+全解码校验共享的预算。输入预算放大到 200MB 后，非 h264 源的
# 兼容转码（HEVC 4K 等）300s 可能不够，放宽到 600s；h264 源不触发转码，
# 成功路径耗时不受此上限影响。
FFMPEG_TIMEOUT_SECONDS = 600
IMAGE_DECODE_TIMEOUT_SECONDS = 15
# 规范化比单纯解码多了缩放与 PNG 编码，1 亿像素上限的原图需要更宽的预算。
IMAGE_NORMALIZE_TIMEOUT_SECONDS = 30
GENERATED_VIDEO_MAX_MB_ENV = "VIDEO_REPLICA_GENERATED_VIDEO_MAX_MB"
# 成片归档/规范化的大小上限（FIX-15）：与用户上传上限（media.MAX_UPLOAD_BYTES=200MB）
# 分离。默认 50MB 是按 1080P 成片估的，2K 高码率成片可能超；线上核实实际大小后可用
# 环境变量调高，不必等发版。
DEFAULT_GENERATED_VIDEO_MAX_MB = 50


def _generated_video_max_bytes() -> int:
    """读成片上限，非法或非正值一律回退默认。

    写成 0 或空串会让所有成片规范化直接失败（等于停服），因此这里 fail-safe
    而不是 fail-closed。
    """
    fallback = DEFAULT_GENERATED_VIDEO_MAX_MB * 1024 * 1024
    raw = os.environ.get(GENERATED_VIDEO_MAX_MB_ENV, "").strip()
    if not raw:
        return fallback
    try:
        megabytes = int(raw)
    except ValueError:
        return fallback
    return megabytes * 1024 * 1024 if megabytes > 0 else fallback


def _size_limit_label(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes // (1024 * 1024)}MB"
    return f"{num_bytes} 字节"


GENERATED_VIDEO_MAX_BYTES = _generated_video_max_bytes()
# This helper is used for customer uploads and generated results.  The byte
# contract is still enforced by each caller; one minute is not a valid global
# compatibility boundary (a perfectly normal source can be 61 seconds).
GENERATED_VIDEO_MAX_SECONDS = 30 * 60
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


@dataclass(frozen=True)
class NormalizedGeneratedVideo:
    content: bytes
    duration_seconds: float
    width: int
    height: int
    source_sample_aspect_ratio: str
    source_display_aspect_ratio: str
    sample_aspect_ratio: str
    display_aspect_ratio: str
    source_rotation_degrees: int
    transformed: bool


@dataclass(frozen=True)
class _GeneratedVideoGeometry:
    video_codec: str
    pixel_format: str
    color_transfer: str
    width: int
    height: int
    sar: Fraction
    dar: Fraction
    rotation: int
    duration: float
    audio_codecs: tuple[str, ...]
    video_duration: float | None = None
    audio_durations: tuple[float | None, ...] = ()
    frame_rate: Fraction = Fraction(0)
    frame_count: int | None = None


def _has_exact_duration(video: _GeneratedVideoGeometry, seconds: int) -> bool:
    # 容器、视频和音轨都要校验，不能只修改 MP4 头中的总时长。
    durations = (video.duration, video.video_duration, *video.audio_durations)
    return (
        all(value == seconds for value in durations)
        and video.frame_rate > 0
        and video.frame_count is not None
        and video.frame_count == seconds * video.frame_rate
    )


def normalize_generated_video(
    content: bytes,
    *,
    target_width: int,
    target_height: int,
    max_bytes: int = GENERATED_VIDEO_MAX_BYTES,
    max_duration_seconds: float | None = GENERATED_VIDEO_MAX_SECONDS,
    allow_mov: bool = False,
    target_duration_seconds: int | None = None,
) -> NormalizedGeneratedVideo:
    """Fit existing MP4 display pixels into a square-pixel canvas without cropping.

    Video is re-encoded when geometry/rotation needs correction or when it is
    not broadly playable by the customer WebView. A single timeout budget covers
    probing and full decoding. Media bytes, tool diagnostics and temporary paths
    are never logged.
    The caller owns target resolution selection, archiving and billing.
    """
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 4096 or value % 2
        for value in (target_width, target_height)
    ):
        raise MediaValidationFailed("目标视频尺寸必须为有效的偶数尺寸")
    if max_bytes <= 0 or (max_duration_seconds is not None and max_duration_seconds <= 0):
        raise MediaValidationFailed("视频规范化资源限制无效")
    if target_duration_seconds is not None and (
        isinstance(target_duration_seconds, bool)
        or not isinstance(target_duration_seconds, int)
        or not 4 <= target_duration_seconds <= 30
    ):
        raise MediaValidationFailed("目标成片时长必须为 4 至 30 的整数秒")
    # 三种拒绝原因分开报：50MB 上限挡住 2K 成片时，运维要能从日志直接看出是大小问题
    # （FIX-15），而不是笼统的「不规范」。
    if not content:
        raise MediaValidationFailed("成片内容为空")
    if len(content) > max_bytes:
        raise MediaValidationFailed(
            f"成片超过大小上限 {_size_limit_label(max_bytes)}"
            f"（可用 {GENERATED_VIDEO_MAX_MB_ENV} 调整归档上限）"
        )
    is_mp4 = content[4:8] == b"ftyp"
    # 仅管理员上传放行 QuickTime 原片；成片入口仍按原有 MP4 边界校验。
    if not is_mp4 and not (allow_mov and content[4:8] in {b"moov", b"wide", b"mdat", b"free"}):
        raise MediaValidationFailed("成片不是可识别的 MP4 容器")
    ffprobe = resolve_media_binary("ffprobe")
    ffmpeg = resolve_media_binary("ffmpeg")
    deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS
    try:
        with tempfile.TemporaryDirectory(prefix="video-replica-normalize-") as directory:
            source_path = Path(directory) / "source.mp4"
            output_path = Path(directory) / "normalized.mp4"
            source_path.write_bytes(content)
            source = _probe_generated_geometry(
                ffprobe, source_path, deadline, max_duration_seconds=max_duration_seconds
            )
            if (
                target_duration_seconds is not None
                and source.video_duration is not None
                and source.video_duration < target_duration_seconds
            ):
                # 不用静止尾帧补足短片，短片必须如实报错并保留原片。
                raise MediaValidationFailed("原片视频轨不足目标时长，无法交付完整成片")
            is_hdr = source.color_transfer in {"smpte2084", "arib-std-b67"}
            # Callers commonly only know coded dimensions.  Rotation is display
            # metadata, therefore that coded target must be rotated as well;
            # otherwise a portrait phone recording is needlessly letterboxed in
            # a landscape canvas.
            display_target_width, display_target_height = target_width, target_height
            if source.rotation in {90, 270} and (target_width, target_height) == (
                source.width,
                source.height,
            ):
                display_target_width, display_target_height = target_height, target_width
            transformed = (
                (allow_mov and (not is_mp4 or content[8:12] == b"qt  "))
                or source.width != display_target_width
                or source.height != display_target_height
                or source.sar != 1
                or source.rotation != 0
                or source.video_codec != "h264"
                or source.pixel_format != "yuv420p"
                or is_hdr
                or (bool(source.audio_codecs) and source.audio_codecs != ("aac",))
                or (
                    target_duration_seconds is not None
                    and not _has_exact_duration(source, target_duration_seconds)
                )
            )
            if transformed:
                fitted_width, fitted_height = _fit_generated_display(
                    source.dar, display_target_width, display_target_height
                )
                tone_map = (
                    "zscale=t=linear:npl=100,format=gbrpf32le,"
                    "tonemap=tonemap=hable:desat=0,"
                    "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p,"
                    if is_hdr
                    else ""
                )
                filters = tone_map + (
                    f"scale={fitted_width}:{fitted_height}:flags=lanczos,setsar=1,"
                    f"pad={display_target_width}:{display_target_height}:(ow-iw)/2:(oh-ih)/2:color=black"
                )
                duration_args: list[str] = []
                if target_duration_seconds is not None:
                    # 整秒与整帧对齐，同时重置时间戳，防止 AAC 尾包把容器再次撑长。
                    fps = source.frame_rate if source.frame_rate in {24, 25, 30, 48, 50, 60} else 24
                    frames = int(fps * target_duration_seconds)
                    filters += (
                        f",setpts=PTS-STARTPTS,fps={fps},trim=end_frame={frames},"
                        f"setpts=N/({fps}*TB)"
                    )
                    # 输出也需声明恒定帧率，否则部分封装器会丢失末帧时长。
                    duration_args = [
                        "-r",
                        str(fps),
                        "-fps_mode",
                        "cfr",
                        "-t",
                        str(target_duration_seconds),
                    ]
                    if source.audio_codecs:
                        duration_args += [
                            "-af",
                            f"apad,atrim=duration={target_duration_seconds},asetpts=PTS-STARTPTS",
                        ]
                _run_generated_tool(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-nostdin",
                        "-xerror",
                        "-threads",
                        "2",
                        "-filter_threads",
                        "1",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(source_path),
                        "-map",
                        "0:v:0",
                        # The first/default audio track is the user-facing
                        # speech track for this product.  Keeping one explicit
                        # track avoids an ambiguous two-AAC output and makes the
                        # browser contract deterministic.
                        "-map",
                        "0:a:0?",
                        "-vf",
                        filters,
                        "-c:v",
                        "libx264",
                        "-threads",
                        "2",
                        "-preset",
                        "medium",
                        "-crf",
                        "18",
                        "-pix_fmt",
                        "yuv420p",
                        "-color_primaries",
                        "bt709",
                        "-color_trc",
                        "bt709",
                        "-colorspace",
                        "bt709",
                        "-c:a",
                        "aac",
                        "-metadata:s:v:0",
                        "rotate=0",
                        "-movflags",
                        "+faststart",
                        *duration_args,
                        "-fs",
                        str(max_bytes + 1),
                        str(output_path),
                    ],
                    deadline,
                )
                if not output_path.is_file() or not 0 < output_path.stat().st_size <= max_bytes:
                    raise MediaValidationFailed("规范化成片大小不合规")
                result = _probe_generated_geometry(
                    ffprobe, output_path, deadline, max_duration_seconds=max_duration_seconds
                )
                if (
                    result.width != display_target_width
                    or result.height != display_target_height
                    or result.sar != 1
                    or result.rotation != 0
                    or result.dar != Fraction(display_target_width, display_target_height)
                    or (
                        not _has_exact_duration(result, target_duration_seconds)
                        if target_duration_seconds is not None
                        else abs(result.duration - source.duration) > 0.05
                    )
                    or result.video_codec != "h264"
                    or result.pixel_format != "yuv420p"
                    or result.color_transfer in {"smpte2084", "arib-std-b67"}
                    or (bool(source.audio_codecs) and result.audio_codecs != ("aac",))
                ):
                    raise MediaValidationFailed("规范化成片的画幅、时长或音轨校验失败")
                selected_path = output_path
            else:
                result = source
                selected_path = source_path
            # Probe success alone does not detect truncated packets or decode failures.
            _run_generated_tool(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-xerror",
                    "-threads",
                    "2",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(selected_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-f",
                    "null",
                    "-",
                ],
                deadline,
            )
            return NormalizedGeneratedVideo(
                content=selected_path.read_bytes() if transformed else content,
                duration_seconds=result.duration,
                width=result.width,
                height=result.height,
                source_sample_aspect_ratio=_aspect_text(source.sar),
                source_display_aspect_ratio=_aspect_text(source.dar),
                sample_aspect_ratio=_aspect_text(result.sar),
                display_aspect_ratio=_aspect_text(result.dar),
                source_rotation_degrees=source.rotation,
                transformed=transformed,
            )
    except OSError as exc:
        raise MediaToolFailed("成片规范化临时文件操作失败") from exc


def _aspect_text(value: Fraction) -> str:
    return f"{value.numerator}:{value.denominator}"


def _fit_generated_display(dar: Fraction, width: int, height: int) -> tuple[int, int]:
    # Round less than two pixels down for the encoder's even chroma dimensions.
    if dar >= Fraction(width, height):
        fitted = width, int(Fraction(width, 1) / dar) // 2 * 2
    else:
        fitted = int(height * dar) // 2 * 2, height
    if min(fitted) < 2:
        raise MediaValidationFailed("成片显示比例无法适配目标尺寸")
    return fitted


def _run_generated_tool(command: list[str], deadline: float, *, probe: bool = False) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaToolFailed("成片规范化处理超时")
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE if probe else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise MediaToolFailed("成片规范化工具执行失败或超时") from exc
    if completed.returncode != 0:
        raise MediaValidationFailed("成片无法完整解码或规范化")
    return completed.stdout if probe else b""


def _probe_generated_geometry(
    ffprobe: str, path: Path, deadline: float, *, max_duration_seconds: float | None
) -> _GeneratedVideoGeometry:
    output = _run_generated_tool(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt,color_transfer,width,height,sample_aspect_ratio,"
            "duration,avg_frame_rate,nb_frames:"
            "stream_tags=rotate:stream_side_data=rotation:format=duration,format_name",
            "-of",
            "json",
            str(path),
        ],
        deadline,
        probe=True,
    )
    try:
        payload = json.loads(output)
        streams = payload["streams"]
        if not isinstance(streams, list) or len(streams) > 8:
            raise ValueError("invalid streams")
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        if video.get("codec_name") not in {"h264", "hevc", "av1", "vp9", "mpeg4"}:
            raise ValueError("unsupported video stream")
        width, height = int(video["width"]), int(video["height"])
        if not (2 <= width <= 8192 and 2 <= height <= 8192 and width * height <= 33554432):
            raise ValueError("invalid dimensions")
        duration = float(payload["format"]["duration"])
        if (
            not math.isfinite(duration)
            or duration <= 0
            or (max_duration_seconds is not None and duration > max_duration_seconds)
        ):
            raise ValueError("invalid duration")
        sar_text = video.get("sample_aspect_ratio", "1:1")
        sar = (
            Fraction(1) if sar_text in {"N/A", "0:1"} else Fraction(str(sar_text).replace(":", "/"))
        )
        if not Fraction(1, 100) <= sar <= 100:
            raise ValueError("invalid sample aspect ratio")
        rotation_value = video.get("tags", {}).get("rotate", 0)
        for side_data in video.get("side_data_list", []):
            if "rotation" in side_data:
                rotation_value = side_data["rotation"]
        rotation_float = float(rotation_value)
        if not math.isfinite(rotation_float) or rotation_float % 90:
            raise ValueError("unsupported rotation")
        rotation = int(rotation_float) % 360
        dar = Fraction(width, height) * sar
        if rotation in {90, 270}:
            dar = 1 / dar
        audio_codecs = tuple(
            str(s.get("codec_name")) for s in streams if s.get("codec_type") == "audio"
        )
        pixel_format = str(video.get("pix_fmt") or "")
        if not pixel_format:
            raise ValueError("missing pixel format")

        def stream_duration(stream: dict[str, object]) -> float | None:
            value = stream.get("duration")
            if value in (None, "N/A"):
                return None
            number = float(str(value))
            if not math.isfinite(number) or number <= 0:
                raise ValueError("invalid stream duration")
            return number

        frame_rate_text = video.get("avg_frame_rate")
        frame_rate = (
            Fraction(str(frame_rate_text))
            if frame_rate_text not in (None, "N/A", "0/0")
            else Fraction(0)
        )
        frame_count_text = video.get("nb_frames")
        return _GeneratedVideoGeometry(
            str(video["codec_name"]),
            pixel_format,
            str(video.get("color_transfer") or "unknown").lower(),
            width,
            height,
            sar,
            dar,
            rotation,
            duration,
            audio_codecs,
            stream_duration(video),
            tuple(stream_duration(s) for s in streams if s.get("codec_type") == "audio"),
            frame_rate,
            int(frame_count_text) if frame_count_text not in (None, "N/A") else None,
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ZeroDivisionError,
        StopIteration,
    ) as exc:
        raise MediaValidationFailed("成片画幅或时长信息无效") from exc


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
    local_input_only: bool = False,
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
        ]
        if local_input_only:
            command.extend(["-protocol_whitelist", "file,pipe"])
        command.append(str(media_path))
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


def normalize_audio_to_mp3(
    content: bytes,
    *,
    suffix: str,
    min_duration_seconds: float | None = None,
    max_duration_seconds: float | None = None,
) -> bytes:
    """Decode one audio track and return a provider-safe mono MP3 sample."""
    inspect_media_bytes(
        content,
        suffix=suffix,
        expected_type="audio",
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        local_input_only=True,
    )
    ffmpeg_path = resolve_media_binary("ffmpeg")
    safe_suffix = suffix if suffix.startswith(".") and suffix[1:].isalnum() else ".bin"
    with tempfile.TemporaryDirectory(prefix="video-replica-audio-normalize-") as temp_dir:
        source_path = Path(temp_dir) / f"source{safe_suffix}"
        output_path = Path(temp_dir) / "normalized.mp3"
        try:
            source_path.write_bytes(content)
        except OSError as exc:
            raise MediaToolFailed("音频临时文件写入失败") from exc
        # 不加 -xerror：手机实录音普遍带少量损坏包，逐包跳过即可产出干净
        # MP3；-xerror 会把首个解码错误升级成致命退出，把可救的录音判死。
        # 产物随后仍经完整探测与时长校验兜底，不会静默交付坏文件。
        try:
            _run(
                [
                    ffmpeg_path,
                    "-v",
                    "error",
                    "-y",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "44100",
                    "-c:a",
                    "mp3",
                    "-b:a",
                    "128k",
                    str(output_path),
                ]
            )
        except MediaToolFailed as exc:
            # 走到这里说明源音频连宽容解码都过不了，属文件问题而非工具故障；
            # 归为校验失败，避免把"换一段录音"误报成"媒体校验服务暂不可用"。
            raise MediaValidationFailed("音频无法转码，请重新录制或更换文件") from exc
        try:
            normalized = output_path.read_bytes()
        except OSError as exc:
            raise MediaToolFailed("标准 MP3 读取失败") from exc
    inspect_media_bytes(
        normalized,
        suffix=".mp3",
        expected_type="audio",
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        local_input_only=True,
    )
    return normalized


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


# 按 EXIF 规范把「存储方向」变换成「显示方向」。部署用的发行版 ffmpeg 版本不一，
# 老版本不会按 JPEG 的 EXIF 方向自动旋转，所以统一关掉自动旋转、由这里显式处理，
# 新老版本才能得到同一结果且不会被转两次。
_EXIF_ORIENTATION_FILTERS = {
    1: "",
    2: "hflip",
    3: "hflip,vflip",
    4: "vflip",
    5: "transpose=0",
    6: "transpose=1",
    7: "transpose=3",
    8: "transpose=2",
}


def normalize_photo_to_png(
    ffmpeg_path: str,
    content: bytes,
    *,
    exif_orientation: int | None,
    max_edge: int,
    max_pixels: int,
) -> bytes:
    """把一张照片摆正、限制长边，并输出 8 位 RGB/RGBA 非隔行 PNG。

    ``exif_orientation`` 为 None 表示方向由容器自身的变换属性描述（AVIF 的
    irot/imir），此时交给 ffmpeg 自动旋转，而不是按 EXIF 显式处理。
    """
    filters = [
        _EXIF_ORIENTATION_FILTERS.get(exif_orientation or 1, ""),
        (
            f"scale=w='min({max_edge},iw)':h='min({max_edge},ih)'"
            ":force_original_aspect_ratio=decrease:flags=lanczos"
        ),
        # 下游纯 Python PNG 解码只认 8 位 RGB/RGBA；透明图保留 alpha 交给出图服务。
        "format=pix_fmts=rgb24|rgba",
    ]
    try:
        # TIFF 的 IFD 常写在像素数据之后，管道输入无法回跳读取，所以落临时文件。
        with tempfile.TemporaryDirectory(prefix="video-replica-photo-") as directory:
            source = Path(directory) / "source"
            source.write_bytes(content)
            command = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-threads",
                "1",
                *(["-noautorotate"] if exif_orientation is not None else []),
                # 解码器在分配帧之前按像素上限拒图，签名校验之外的解压炸弹兜底。
                "-max_pixels",
                str(max_pixels),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                ",".join(item for item in filters if item),
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=IMAGE_NORMALIZE_TIMEOUT_SECONDS,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("ffmpeg photo normalization failed to run: %s", type(exc).__name__)
        raise MediaToolFailed("ffmpeg 图片规范化执行失败") from exc
    if completed.returncode != 0 or not completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise MediaToolFailed("ffmpeg 无法规范化图片")
    return completed.stdout


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
