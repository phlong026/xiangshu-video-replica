"""长媒体的有界磁盘处理，不把原片或完整音轨读入内存。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from app.media_tools import MediaToolFailed
from app.storage import StorageAdapter


def temp_root() -> str | None:
    configured = os.environ.get("VIDEO_REPLICA_MEDIA_TEMP_ROOT", "").strip()
    if not configured:
        return None
    root = Path(configured).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def stream_source(
    storage: StorageAdapter,
    key: str,
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
    heartbeat: Callable[[], None],
) -> None:
    if not 0 < expected_size <= max_bytes:
        raise MediaToolFailed("来源文件大小超出处理限制。")
    if shutil.disk_usage(path.parent).free < expected_size + 1024**3:
        raise MediaToolFailed("媒体临时目录空间不足，请释放磁盘空间后重试。")
    digest, size, last = hashlib.sha256(), 0, time.monotonic()
    chunks = storage.iter_object(key)
    try:
        with path.open("wb") as output:
            for chunk in chunks:
                size += len(chunk)
                if size > expected_size or size > max_bytes:
                    raise MediaToolFailed("来源文件大小发生变化，请重新上传。")
                digest.update(chunk)
                output.write(chunk)
                if time.monotonic() - last >= 15:
                    heartbeat()
                    last = time.monotonic()
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise MediaToolFailed("来源文件校验失败，请重新上传。")
    heartbeat()


def extract_segment(
    ffmpeg: str,
    source: Path,
    target: Path,
    *,
    start: float,
    end: float,
    heartbeat: Callable[[], None],
) -> None:
    command = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "2",
        "-protocol_whitelist",
        "file,pipe",
        "-ss",
        str(start),
        "-i",
        str(source),
        "-t",
        str(end - start),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        str(target),
    ]
    # 不使用 PIPE，避免异常媒体连续打印诊断造成内存增长或管道死锁。
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = last_growth = last_heartbeat = time.monotonic()
    previous_size = 0
    try:
        while process.poll() is None:
            now = time.monotonic()
            size = target.stat().st_size if target.exists() else 0
            if size > previous_size:
                previous_size, last_growth = size, now
            if size > 8 * 1024**2 or now - started > 900 or now - last_growth > 120:
                raise MediaToolFailed("音轨分段处理超时或输出异常，请检查来源文件。")
            if now - last_heartbeat >= 15:
                heartbeat()
                last_heartbeat = now
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise MediaToolFailed("来源文件没有可用音轨，或音轨分段处理失败。")
        heartbeat()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def trim_video(ffmpeg: str, source: Path, target: Path, *, start: float, end: float,
               heartbeat: Callable[[], None]) -> None:
    command = [ffmpeg, "-v", "error", "-nostdin", "-y", "-threads", "2", "-max_alloc", "268435456",
               "-ss", str(start), "-i", str(source), "-t", str(end - start),
               "-vf", "scale=w=min(1920\\,iw):h=min(1920\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "8M", "-bufsize", "16M",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(target)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    started = last_growth = last_heartbeat = time.monotonic()
    previous_size = 0
    try:
        while process.poll() is None:
            now = time.monotonic()
            size = target.stat().st_size if target.exists() else 0
            if size > previous_size:
                previous_size, last_growth = size, now
            if size > 600 * 1024**2 or now - started > 600 or now - last_growth > 90:
                raise MediaToolFailed("视频选段超时或文件过大，请缩短片段后重试。")
            if now - last_heartbeat >= 15:
                heartbeat()
                last_heartbeat = now
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise MediaToolFailed("视频选段失败，请检查原视频。")
        heartbeat()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
