"""Pure coordinate-contract tests (#28) — ``remoteos.coords``.

A frame maps the (possibly downscaled) Snapshot image to the OS input coordinate
space: ``screen = origin + round(image * scale)``, clamped to the frame. These are
table tests on the pure functions — no display, no mocks.
"""

from __future__ import annotations

import pytest

from remoteos.coords import (
    CaptureGeometry,
    check_space,
    contract_line,
    frame_for,
    image_size_for,
    image_to_screen,
    mapping_note,
    screen_to_image,
)


def _geo(x: int, y: int, w: int, h: int, pw: int | None = None, ph: int | None = None) -> CaptureGeometry:
    return CaptureGeometry(x=x, y=y, width=w, height=h, pixel_width=pw or w, pixel_height=ph or h)


# ---------------------------------------------------------------------------
# frame_for — image size + scale + origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "geometry,max_width,image,scale,origin",
    [
        # the live win-resolume-pp case: 2560x1600 desktop at the default max_width
        (_geo(0, 0, 2560, 1600), 1280, (1280, 800), 2.0, (0, 0)),
        (_geo(0, 0, 1920, 1080), 1280, (1280, 720), 1.5, (0, 0)),
        # native == max and native < max → no resize
        (_geo(0, 0, 1280, 720), 1280, (1280, 720), 1.0, (0, 0)),
        (_geo(0, 0, 1024, 768), 1280, (1024, 768), 1.0, (0, 0)),
        # max_width=0 → native resolution
        (_geo(0, 0, 2560, 1600), 0, (2560, 1600), 1.0, (0, 0)),
        # multi-monitor virtual screen: 1920 monitor LEFT of a 2560 primary
        (_geo(-1920, 0, 4480, 1600), 1280, (1280, 457), 3.5, (-1920, 0)),
        # macOS Retina: input in points 1440x900, capture in pixels 2880x1800
        (_geo(0, 0, 1440, 900, 2880, 1800), 1280, (1280, 800), 1.125, (0, 0)),
        (_geo(0, 0, 1440, 900, 2880, 1800), 0, (2880, 1800), 0.5, (0, 0)),
    ],
)
def test_frame_for_table(geometry, max_width, image, scale, origin):
    frame = frame_for(0, max_width, geometry)
    assert frame.image_size == image
    assert frame.scale == pytest.approx(scale)
    assert frame.origin == origin
    assert (frame.width, frame.height) == (geometry.width, geometry.height)


def test_image_size_rounds_height_to_nearest():
    # 768 * 1280/1366 = 719.65 → 720 (nearest, not truncated to 719)
    assert image_size_for(1366, 768, 1280) == (1280, 720)


def test_frame_rejects_empty_geometry():
    with pytest.raises(ValueError):
        frame_for(0, 1280, _geo(0, 0, 0, 1080))


# ---------------------------------------------------------------------------
# image_to_screen / screen_to_image
# ---------------------------------------------------------------------------


def test_image_to_screen_scale_2():
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert image_to_screen(frame, 640, 400) == (1280, 800)
    assert image_to_screen(frame, 0, 0) == (0, 0)


def test_live_element_center_round_trips():
    # win-resolume-pp 2026-10-14: element listed at native (1280,796) in a 1280x800 image
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert screen_to_image(frame, 1280, 796) == (640, 398)
    assert image_to_screen(frame, 640, 398) == (1280, 796)


def test_image_to_screen_scale_1_5_edge_rounding():
    frame = frame_for(0, 1280, _geo(0, 0, 1920, 1080))
    assert image_to_screen(frame, 1, 1) == (2, 2)  # 1.5 rounds half-up
    assert image_to_screen(frame, 3, 3) == (5, 5)  # 4.5 rounds half-up too (no banker's rounding)
    # last image pixel → last screen pixel
    assert image_to_screen(frame, 1279, 719) == (1919, 1079)


def test_image_to_screen_non_integer_scale_last_pixel():
    frame = frame_for(0, 1280, _geo(0, 0, 1366, 768))
    assert image_to_screen(frame, 1279, 719) == (1365, 767)


def test_image_to_screen_negative_virtual_origin():
    frame = frame_for(0, 1280, _geo(-1920, 0, 4480, 1600))
    assert image_to_screen(frame, 0, 0) == (-1920, 0)
    assert image_to_screen(frame, 640, 228) == (320, 798)
    assert screen_to_image(frame, -1920, 0) == (0, 0)


def test_image_to_screen_monitor_offset():
    # monitor 2 sits right of a 2560-wide primary
    frame = frame_for(2, 1280, _geo(2560, 0, 1920, 1080))
    assert image_to_screen(frame, 640, 360) == (3520, 540)
    assert screen_to_image(frame, 3520, 540) == (640, 360)


def test_image_to_screen_retina_points():
    frame = frame_for(0, 1280, _geo(0, 0, 1440, 900, 2880, 1800))
    assert image_to_screen(frame, 640, 400) == (720, 450)
    assert image_to_screen(frame, 1279, 799) == (1439, 899)
    native = frame_for(0, 0, _geo(0, 0, 1440, 900, 2880, 1800))
    assert image_to_screen(native, 1440, 900) == (720, 450)


@pytest.mark.parametrize(
    "point,expected",
    [
        ((5000, 5000), (2559, 1599)),  # beyond bottom-right → last pixel
        ((-10, -10), (0, 0)),  # negative → frame origin
        ((1280, 0), (2559, 0)),  # one past the right edge
    ],
)
def test_image_to_screen_clamps_to_frame(point, expected):
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert image_to_screen(frame, *point) == expected


def test_image_to_screen_clamps_negative_origin_frame():
    frame = frame_for(0, 1280, _geo(-1920, 0, 4480, 1600))
    assert image_to_screen(frame, -50, -50) == (-1920, 0)
    assert image_to_screen(frame, 99999, 99999) == (2559, 1599)


# ---------------------------------------------------------------------------
# coordinate space selector + human-readable contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [("image", "image"), ("screen", "screen"), (" Screen ", "screen")])
def test_check_space_accepts_image_and_screen(value, expected):
    assert check_space(value) == expected


def test_check_space_rejects_unknown():
    with pytest.raises(ValueError, match="coords must be 'image' or 'screen'"):
        check_space("pixels")


def test_contract_line_exact_format():
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert contract_line(frame) == (
        "Image 1280x800 = screen 2560x1600 at origin (0,0), scale 2.000; "
        "input tools take IMAGE pixels of this frame (monitor=0, max_width=1280)"
    )


def test_contract_line_negative_origin_and_monitor():
    frame = frame_for(2, 0, _geo(-1920, -200, 1920, 1080))
    assert contract_line(frame) == (
        "Image 1920x1080 = screen 1920x1080 at origin (-1920,-200), scale 1.000; "
        "input tools take IMAGE pixels of this frame (monitor=2, max_width=0)"
    )


def test_mapping_note_lists_every_point():
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    note = mapping_note(frame, [(10, 20), (640, 400)], [(20, 40), (1280, 800)])
    assert note == " [image (10,20) → screen (20,40), image (640,400) → screen (1280,800); scale 2.000]"
