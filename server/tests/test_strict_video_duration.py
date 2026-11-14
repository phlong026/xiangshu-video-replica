"""真实编码验证交付时长，防止只修改元数据或只裁视频而留下音轨尾包。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from app.media_tools import MediaValidationFailed, normalize_generated_video, resolve_media_binary


def _probe(path: Path, *, video_packets: bool = False) -> dict:
    return json.loads(
        subprocess.check_output(
            [
                resolve_media_binary("ffprobe"),
                "-v",
                "error",
                *(
                    [
                        "-select_streams",
                        "v:0",
                        "-show_packets",
                        "-show_entries",
                        "packet=pts,duration",
                    ]
                    if video_packets
                    else ["-show_streams", "-show_format"]
                ),
                "-of",
                "json",
                str(path),
            ]
        )
    )


def _source(path: Path, *, seconds: float, audio: bool = True, fps: int = 24) -> bytes:
    command = [
        resolve_media_binary("ffmpeg"),
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=160x96:rate={fps}",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += [
        "-t",
        str(seconds),
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    subprocess.run(command, check=True, timeout=30, capture_output=True)
    return path.read_bytes()


@pytest.mark.parametrize(
    ("audio", "fps"),
    [(True, 24), (False, 24), (True, 25), (True, 30), (True, 48), (True, 50), (True, 60)],
)
def test_oversized_result_delivers_exactly_30_seconds(tmp_path: Path, audio: bool, fps: int):
    original_path = tmp_path / "original.mp4"
    original = _source(original_path, seconds=30.675, audio=audio, fps=fps)
    before = _probe(original_path)
    assert float(before["format"]["duration"]) > 30
    if audio and fps == 24:
        assert float(before["format"]["duration"]) == 30.675
        assert int(before["streams"][0]["nb_frames"]) == 736
    normalized = normalize_generated_video(
        original,
        target_width=160,
        target_height=96,
        target_duration_seconds=30,
    )
    assert normalized.transformed
    assert normalized.duration_seconds == 30
    assert original_path.read_bytes() == original
    delivered = tmp_path / "delivered.mp4"
    delivered.write_bytes(normalized.content)
    measured = _probe(delivered)
    assert float(measured["format"]["duration"]) == 30
    video = next(s for s in measured["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == fps * 30
    assert Fraction(video["avg_frame_rate"]) == fps
    time_base = Fraction(video["time_base"])
    assert int(video["duration_ts"]) * time_base == 30
    assert video["width"] == 160 and video["height"] == 96
    assert all(float(s["duration"]) == 30 for s in measured["streams"])
    assert len(measured["streams"]) == (2 if audio else 1)
    # 容器时长不能掩盖末帧包时长丢失；每个真实视频包也必须保持恒定帧率。
    packets = _probe(delivered, video_packets=True)["packets"]
    assert len(packets) == fps * 30
    assert {int(p["duration"]) * time_base for p in packets} == {Fraction(1, fps)}
    assert max((int(p["pts"]) + int(p["duration"])) * time_base for p in packets) == 30
    # 归档重试必须复用完全相同的已校验字节。
    replay = normalize_generated_video(
        normalized.content,
        target_width=160,
        target_height=96,
        target_duration_seconds=30,
    )
    assert not replay.transformed
    assert replay.content == normalized.content


def test_short_result_is_rejected_instead_of_frozen_frame_padding(tmp_path: Path):
    original = _source(tmp_path / "short.mp4", seconds=29)
    with pytest.raises(MediaValidationFailed, match="不足目标时长"):
        normalize_generated_video(
            original,
            target_width=160,
            target_height=96,
            target_duration_seconds=30,
        )


@pytest.mark.parametrize("seconds", [29, 30.675])
def test_missing_source_track_duration_still_requires_exact_decoded_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seconds: float,
):
    from app import media_tools

    original = _source(tmp_path / "source.mp4", seconds=seconds)
    probe = media_tools._probe_generated_geometry

    def without_source_duration(*args, **kwargs):
        measured = probe(*args, **kwargs)
        return replace(measured, video_duration=None) if args[1].name == "source.mp4" else measured

    monkeypatch.setattr(media_tools, "_probe_generated_geometry", without_source_duration)
    if seconds < 30:
        with pytest.raises(MediaValidationFailed, match="时长"):
            normalize_generated_video(
                original, target_width=160, target_height=96, target_duration_seconds=30
            )
    else:
        normalized = normalize_generated_video(
            original, target_width=160, target_height=96, target_duration_seconds=30
        )
        assert normalized.transformed and normalized.duration_seconds == 30


@pytest.mark.parametrize("seconds", [True, 0, 3, 31, 30.5])
def test_invalid_target_duration_fails_before_media_io(seconds):
    with pytest.raises(MediaValidationFailed, match="整数秒"):
        normalize_generated_video(
            b"invalid",
            target_width=160,
            target_height=96,
            target_duration_seconds=seconds,
        )
