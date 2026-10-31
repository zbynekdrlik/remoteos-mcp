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
    region_geometry,
    region_to_screen,
    screen_to_image,
    zoom_line,
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


# ---------------------------------------------------------------------------
# region_to_screen / region_geometry / zoom_line — Zoom's rectangle (#33)
# ---------------------------------------------------------------------------


def test_region_in_image_pixels_scales_to_screen():
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert region_to_screen(frame, 100, 50, 320, 200, "image") == (200, 100, 840, 500)


def test_region_keeps_the_frame_origin():
    frame = frame_for(0, 1280, _geo(-1920, -180, 3840, 2160))  # scale 3.0
    assert region_to_screen(frame, 10, 20, 100, 50, "image") == (-1890, -120, -1590, 30)


def test_region_rounds_outward_to_cover_every_pixel_asked_for():
    frame = frame_for(0, 1280, _geo(0, 0, 1440, 900, 2880, 1800))  # scale 1.125
    assert region_to_screen(frame, 100, 100, 200, 100, "image") == (112, 112, 338, 225)
    assert region_to_screen(frame, 1, 1, 1, 1, "image") == (1, 1, 3, 3)


def test_region_uses_each_axis_own_scale():
    frame = frame_for(0, 1280, _geo(0, 0, 1920, 1081))  # image 1280x721: scale_y is not scale_x
    assert region_to_screen(frame, 0, 100, 10, 100, "image") == (0, 149, 15, 300)


def test_region_in_screen_coordinates_is_taken_as_it_is():
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert region_to_screen(frame, 7, 9, 11, 13, "screen") == (7, 9, 18, 22)


@pytest.mark.parametrize(
    ("rect", "expected"),
    [
        ((1200, 700, 400, 400), (2400, 1400, 2560, 1600)),  # past the far edges: right/bottom are exclusive
        ((-10, -10, 20, 20), (0, 0, 20, 20)),  # past the near edges
        ((0, 0, 5000, 5000), (0, 0, 2560, 1600)),
    ],
)
def test_region_is_cut_to_the_frame(rect, expected):
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    assert region_to_screen(frame, *rect, "image") == expected


def test_region_of_one_native_retina_pixel_is_the_point_it_lies_in():
    frame = frame_for(0, 0, _geo(0, 0, 1440, 900, 2880, 1800))  # scale 0.5
    assert region_to_screen(frame, 11, 11, 1, 1, "image") == (5, 5, 6, 6)
    assert region_to_screen(frame, 2879, 1799, 1, 1, "image") == (1439, 899, 1440, 900)  # the last pixel


@pytest.mark.parametrize("size", [(0, 10), (10, 0), (-5, 10), (10, -5)])
def test_region_needs_a_positive_size(size):
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    with pytest.raises(ValueError, match="must be positive"):
        region_to_screen(frame, 0, 0, *size, "image")


@pytest.mark.parametrize("rect", [(1280, 0, 10, 10), (0, 800, 10, 10), (-20, 0, 10, 10), (0, -20, 10, 10)])
def test_region_outside_the_frame_is_an_error(rect):
    frame = frame_for(0, 1280, _geo(0, 0, 2560, 1600))
    with pytest.raises(ValueError, match="outside the frame"):
        region_to_screen(frame, *rect, "image")


def test_region_geometry_keeps_the_pixels_per_screen_unit():
    region = region_geometry(_geo(0, 0, 1440, 900, 2880, 1800), (113, 113, 338, 225))
    assert region == CaptureGeometry(x=113, y=113, width=225, height=112, pixel_width=450, pixel_height=224)


def test_zoom_frame_is_native_up_to_max_width_and_downscaled_above_it():
    geometry = _geo(0, 0, 2560, 1600)
    native = frame_for(0, 1280, region_geometry(geometry, (200, 100, 840, 500)))
    assert native.image_size == (640, 400) and native.scale == 1.0
    wide = frame_for(0, 1280, region_geometry(geometry, (0, 0, 2000, 1000)))
    assert wide.image_size == (1280, 640) and wide.scale == pytest.approx(1.5625)


def test_zoom_line_gives_the_screen_region_and_says_it_is_view_only():
    line = zoom_line(frame_for(0, 1280, region_geometry(_geo(0, 0, 2560, 1600), (200, 100, 840, 500))))
    assert line.startswith("Zoom image 640x400 = screen 640x400 at origin (200,100), scale 1.000; view only")
    assert 'screen = origin + round(image * scale) with coords="screen"' in line
