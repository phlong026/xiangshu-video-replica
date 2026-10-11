"""Unit coverage for the oral-compatible derivative resolution cap."""

import pytest

from app.media_tools import (
    MediaValidationFailed,
    _cap_display_dimensions,
    normalize_generated_video,
)


@pytest.mark.parametrize(
    ("width", "height", "max_long_side", "expected"),
    [
        # under the cap: untouched
        (1080, 1920, 1920, (1080, 1920)),
        (1920, 1080, 1920, (1920, 1080)),
        (720, 1280, 1920, (720, 1280)),
        # exact halving of a 4K portrait phone recording
        (2160, 3840, 1920, (1080, 1920)),
        # odd rounding must stay even for the encoder's chroma format
        (2162, 3842, 1920, (1080, 1920)),
        (3841, 2161, 1920, (1920, 1080)),
        # extreme aspect: short side clamps at the 2px floor
        (4096, 4, 1920, (1920, 2)),
    ],
)
def test_cap_display_dimensions(
    width: int, height: int, max_long_side: int, expected: tuple[int, int]
) -> None:
    assert _cap_display_dimensions(width, height, max_long_side) == expected


def test_normalize_rejects_invalid_long_side_cap() -> None:
    with pytest.raises(MediaValidationFailed):
        normalize_generated_video(
            b"\x00\x00\x00\x18ftyp",
            target_width=1080,
            target_height=1920,
            max_long_side=0,
        )
