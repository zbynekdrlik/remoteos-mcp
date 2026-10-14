"""Snapshot rendering in IMAGE pixels (#28) — ``remoteos.snapshot`` + Linux display parsing.

Window rects and element centers must be listed in the same coordinate space as
the image the model sees, and AnnotatedSnapshot's boxes must be drawn there too.
Real PIL images, real provider dataclasses — no mocks.
"""

from __future__ import annotations

import base64
import io

from PIL import Image

from remoteos.coords import CaptureGeometry, frame_for
from remoteos.platform.linux.desktop import WindowInfo
from remoteos.platform.linux.display import parse_xrandr_monitors
from remoteos.snapshot import draw_annotations, element_lines, encode_jpeg, fit_to_frame, window_lines


def _geo(x, y, w, h, pw=None, ph=None):
    return CaptureGeometry(x=x, y=y, width=w, height=h, pixel_width=pw or w, pixel_height=ph or h)


def _frame_2560x1600():
    return frame_for(0, 1280, _geo(0, 0, 2560, 1600))


def _el(index, left, top, right, bottom, text="OK"):
    rect = {"left": left, "top": top, "right": right, "bottom": bottom}
    return {"index": index, "class": "Button", "text": text, "rect": rect}


def test_element_lines_report_image_pixel_centers():
    # live evidence: native center (1280,796) on a 2560x1600 desktop, 1280x800 image
    lines = element_lines(_frame_2560x1600(), [_el(7, 1200, 780, 1360, 812)])
    assert lines == ["  [7] OK — center (640,398)"]


def test_element_lines_fall_back_to_class_and_mark_off_frame():
    frame = frame_for(2, 1280, _geo(2560, 0, 1920, 1080))
    lines = element_lines(frame, [_el(1, 100, 100, 200, 140, text="")])  # sits on monitor 1, not 2
    assert lines == ["  [1] Button — center (-1607,80) (off-frame)"]


def test_window_lines_report_image_pixel_rects():
    win = WindowInfo(handle=42, title="Editor", rect=(100, 50, 900, 650), visible=True)
    assert window_lines(_frame_2560x1600(), [win]) == ["  [42] Editor (400x300 at 50,25)"]


def test_window_lines_mark_window_outside_the_frame():
    win = WindowInfo(handle=9, title="Minimized", rect=(-32000, -32000, -31840, -31972), visible=True)
    (line,) = window_lines(_frame_2560x1600(), [win])
    assert line.endswith("(off-frame)")


def test_fit_to_frame_resizes_and_converts_to_rgb():
    img = Image.new("RGBA", (2560, 1600), (10, 20, 30, 255))
    out = fit_to_frame(img, _frame_2560x1600())
    assert out.size == (1280, 800)
    assert out.mode == "RGB"


def test_fit_to_frame_uses_frame_size_even_when_capture_differs():
    # macOS Retina: capture is 2880x1800 pixels, frame says image 1280x800
    frame = frame_for(0, 1280, _geo(0, 0, 1440, 900, 2880, 1800))
    out = fit_to_frame(Image.new("RGB", (2880, 1800)), frame)
    assert out.size == (1280, 800)


def test_fit_to_frame_keeps_native_image_untouched():
    frame = frame_for(0, 1280, _geo(0, 0, 1024, 768))
    img = Image.new("RGB", (1024, 768))
    assert fit_to_frame(img, frame) is img


def test_encode_jpeg_round_trips():
    b64 = encode_jpeg(Image.new("RGB", (64, 32), (255, 255, 255)), quality=65)
    decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert decoded.format == "JPEG"
    assert decoded.size == (64, 32)


def test_draw_annotations_boxes_land_in_image_pixels():
    frame = _frame_2560x1600()
    img = Image.new("RGB", frame.image_size, (255, 255, 255))
    # native rect (400,400)-(800,600) → image (200,200)-(400,300)
    lines = draw_annotations(img, [_el(3, 400, 400, 800, 600, text="Save")], frame, max_elements=30)
    assert lines == ["  [3] Save — center (300,250)"]
    r, g, b = img.getpixel((200, 250))  # left edge of the box, in image pixels
    assert r > 200 and g < 80 and b < 80
    assert img.getpixel((400, 500)) == (255, 255, 255)  # left edge of a box drawn at NATIVE coords — must stay white


def test_draw_annotations_respects_max_elements():
    frame = _frame_2560x1600()
    img = Image.new("RGB", frame.image_size, (255, 255, 255))
    els = [_el(i, 100 * i, 100, 100 * i + 50, 150) for i in range(1, 6)]
    assert len(draw_annotations(img, els, frame, max_elements=2)) == 2


# ---------------------------------------------------------------------------
# Linux display geometry parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_xrandr_monitors():
    text = (
        "Monitors: 2\n"
        " 0: +*eDP-1 2560/344x1600/215+0+0  eDP-1\n"
        " 1: +HDMI-1 1920/527x1080/296+2560+0  HDMI-1\n"
    )
    assert parse_xrandr_monitors(text) == [(0, 0, 2560, 1600), (2560, 0, 1920, 1080)]


def test_parse_xrandr_monitors_empty():
    assert parse_xrandr_monitors("Monitors: 0\n") == []
