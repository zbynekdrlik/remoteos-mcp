"""Snapshot rendering in IMAGE pixels (#28) — shared by every desktop provider.

The providers capture; this module turns a capture into what the model sees and
keeps every coordinate it prints in the SAME space as that image (the frame from
:mod:`remoteos.coords`): fit the capture to the frame, JPEG-encode it, list window
rects / element centers in image pixels, and draw AnnotatedSnapshot's numbered
boxes there. No OS calls.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

from remoteos.coords import CaptureGeometry, Frame, in_image, screen_to_image

log = logging.getLogger("remoteos.snapshot")

_OFF_FRAME = " (off-frame)"


def fit_to_frame(img: Image.Image, frame: Frame, geometry: Optional[CaptureGeometry] = None) -> Image.Image:
    """Return ``img`` as RGB at exactly ``frame.image_size``.

    The size always comes from the frame (computed from display geometry), never
    from the raw capture, so the image the model sees matches the frame the input
    tools recompute at click time — even on macOS Retina (capture in pixels,
    frame in points). A capture whose size differs from ``geometry``'s pixel size
    is unexpected (the geometry query and the grabber disagree about the screen),
    so it is still fitted but logged as a warning, never silently.
    """
    if geometry is not None and img.size != (geometry.pixel_width, geometry.pixel_height):
        log.warning(
            "capture is %dx%d but the display geometry says %dx%d — fitting it to the frame anyway",
            img.width,
            img.height,
            geometry.pixel_width,
            geometry.pixel_height,
        )
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.size != frame.image_size:
        img = img.resize(frame.image_size, resample=Image.LANCZOS)
    return img


def encode_jpeg(img: Image.Image, quality: int) -> str:
    """Base64 JPEG of ``img``."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _rect_to_image(frame: Frame, left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int]:
    x1, y1 = screen_to_image(frame, left, top)
    x2, y2 = screen_to_image(frame, right, bottom)
    return x1, y1, x2, y2


def _element_rect(frame: Frame, el: dict) -> tuple[int, int, int, int]:
    r = el["rect"]
    return _rect_to_image(frame, r["left"], r["top"], r["right"], r["bottom"])


def _element_center(frame: Frame, el: dict) -> tuple[int, int]:
    r = el["rect"]
    return screen_to_image(frame, (r["left"] + r["right"]) // 2, (r["top"] + r["bottom"]) // 2)


def _element_line(frame: Frame, el: dict) -> str:
    cx, cy = _element_center(frame, el)
    label = el["text"] or el["class"]
    marker = "" if in_image(frame, cx, cy) else _OFF_FRAME
    return f"  [{el['index']}] {label} — center ({cx},{cy}){marker}"


def element_lines(frame: Frame, elements: Iterable[dict]) -> list[str]:
    """``[index] label — center (x,y)`` per element, centers in image pixels."""
    return [_element_line(frame, el) for el in elements]


def window_lines(frame: Frame, windows: Iterable) -> list[str]:
    """``[handle] title (WxH at X,Y)`` per window, rects in image pixels.

    A window that does not overlap the frame at all (another monitor, a
    minimized window parked at -32000) is still listed, marked ``(off-frame)``.
    """
    lines = []
    for w in windows:
        x1, y1, x2, y2 = _rect_to_image(frame, *w.rect)
        overlaps = x1 < frame.image_width and x2 > 0 and y1 < frame.image_height and y2 > 0
        marker = "" if overlaps else _OFF_FRAME
        lines.append(f"  [{w.handle}] {w.title} ({x2 - x1}x{y2 - y1} at {x1},{y1}){marker}")
    return lines


def _label_font():
    for name in ("arial.ttf", "DejaVuSans.ttf"):  # Windows / Linux
        try:
            return ImageFont.truetype(name, 14)
        except (OSError, ImportError):  # font file absent / FreeType not built in
            continue
    return ImageFont.load_default()


def draw_annotations(img: Image.Image, elements: list[dict], frame: Frame, max_elements: int) -> list[str]:
    """Draw numbered red boxes on ``img`` (in place, image pixels); return the text lines.

    ``elements`` are the providers' shared ``{index, class, text, rect}`` dicts
    with ``rect`` in screen coordinates; ``img`` must already be fitted to
    ``frame``.
    """
    draw = ImageDraw.Draw(img)
    font = _label_font()
    lines: list[str] = []
    for el in elements[:max_elements]:
        x1, y1, x2, y2 = _element_rect(frame, el)
        draw.rectangle([x1, y1, x2, y2], outline="red", width=2)
        label = str(el["index"])
        bbox = font.getbbox(label)
        lw = bbox[2] - bbox[0] + 6
        lh = bbox[3] - bbox[1] + 4
        draw.rectangle([x1, y1 - lh - 2, x1 + lw, y1 - 2], fill="red")
        draw.text((x1 + 3, y1 - lh - 1), label, fill="white", font=font)
        lines.append(_element_line(frame, el))
    return lines
