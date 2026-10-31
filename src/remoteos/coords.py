"""Coordinate contract between Snapshot images and the input tools (#28).

A *frame* is the mapping between the (possibly downscaled) screenshot image the
model sees and the OS input coordinate space ("screen"): physical pixels on
Windows (per-monitor-v2 DPI aware) and Linux/X11, **points** on macOS. A frame is
fully determined by ``(monitor, max_width)`` plus the current display geometry,
so the tool layer recomputes it at click time — no per-session state, which fits
the stateless HTTP transport::

    screen = origin + round(image * scale)      (clamped to the frame)
    image  = round((screen - origin) / scale)

Pure arithmetic, no I/O. Desktop providers report a :class:`CaptureGeometry` for
a monitor; :func:`frame_for` turns it into a :class:`Frame`; providers resize the
capture to ``frame.image_size`` and the tool layer maps input points with
:func:`image_to_screen`. Rounding is round-half-up everywhere (never Python's
banker's ``round()``), so the resize and both mapping directions agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

COORD_SPACES = ("image", "screen")

Point = tuple[int, int]
Bbox = tuple[int, int, int, int]  # left, top, right, bottom in screen coordinates; right/bottom exclusive


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


@dataclass(frozen=True)
class CaptureGeometry:
    """One capture target as the OS sees it.

    ``x, y, width, height`` are the target's bounds in SCREEN (input)
    coordinates — the space pyautogui / xdotool take. ``pixel_width`` /
    ``pixel_height`` are the dimensions of the raw capture; they equal the bounds
    except on a macOS Retina display, where the capture is in physical pixels
    while input works in points.
    """

    x: int
    y: int
    width: int
    height: int
    pixel_width: int
    pixel_height: int


@dataclass(frozen=True)
class Frame:
    """A Snapshot image ↔ screen mapping (see the module docstring)."""

    monitor: int
    max_width: int
    x: int
    y: int
    width: int
    height: int
    image_width: int
    image_height: int

    @property
    def origin(self) -> Point:
        return self.x, self.y

    @property
    def image_size(self) -> Point:
        return self.image_width, self.image_height

    @property
    def scale_x(self) -> float:
        return self.width / self.image_width

    @property
    def scale_y(self) -> float:
        return self.height / self.image_height

    @property
    def scale(self) -> float:
        """Screen units per image pixel (horizontal; vertical differs only by rounding)."""
        return self.scale_x


def image_size_for(pixel_width: int, pixel_height: int, max_width: int) -> Point:
    """Size of the Snapshot image for a capture of ``pixel_width x pixel_height``.

    Downscaled to ``max_width`` (aspect kept, height rounded half-up) when the
    capture is wider; ``max_width <= 0`` or a narrower capture keeps it native.
    """
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError(f"empty capture size {pixel_width}x{pixel_height}")
    if max_width > 0 and pixel_width > max_width:
        return max_width, max(1, _round_half_up(pixel_height * max_width / pixel_width))
    return pixel_width, pixel_height


def frame_for(monitor: int, max_width: int, geometry: CaptureGeometry) -> Frame:
    """The frame a Snapshot of ``monitor`` at ``max_width`` produces on ``geometry``."""
    if geometry.width <= 0 or geometry.height <= 0:
        raise ValueError(f"empty screen bounds {geometry.width}x{geometry.height}")
    image_width, image_height = image_size_for(geometry.pixel_width, geometry.pixel_height, max_width)
    return Frame(
        monitor=monitor,
        max_width=max_width,
        x=geometry.x,
        y=geometry.y,
        width=geometry.width,
        height=geometry.height,
        image_width=image_width,
        image_height=image_height,
    )


def image_to_screen(frame: Frame, x: int, y: int) -> Point:
    """Map an image pixel to the screen point under it, clamped to the frame."""
    sx = frame.x + _round_half_up(x * frame.scale_x)
    sy = frame.y + _round_half_up(y * frame.scale_y)
    return (
        _clamp(sx, frame.x, frame.x + frame.width - 1),
        _clamp(sy, frame.y, frame.y + frame.height - 1),
    )


def screen_to_image(frame: Frame, x: int, y: int) -> Point:
    """Map a screen point to image pixels. NOT clamped: off-frame stays off-frame."""
    return (
        _round_half_up((x - frame.x) / frame.scale_x),
        _round_half_up((y - frame.y) / frame.scale_y),
    )


def region_to_screen(frame: Frame, x: int, y: int, width: int, height: int, space: str) -> Bbox:
    """Zoom's rectangle as ``(left, top, right, bottom)`` in screen units, cut to ``frame``.

    ``space`` is ``"image"`` (pixels of the parent frame) or ``"screen"``. Right and
    bottom are exclusive, so they may reach the frame's far edge. Image pixels round
    OUTWARD, in exact integers, unlike a point: the capture must cover every pixel
    asked for, and a pixel of a native Retina frame is half a point. Nothing of it
    inside the frame is an error: a capture of nothing would look like a black screen.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"region size must be positive, got {width}x{height}")
    if space == "image":
        left = frame.x + x * frame.width // frame.image_width
        top = frame.y + y * frame.height // frame.image_height
        right = frame.x - (-(x + width) * frame.width // frame.image_width)
        bottom = frame.y - (-(y + height) * frame.height // frame.image_height)
    else:
        left, top, right, bottom = x, y, x + width, y + height
    far_x, far_y = frame.x + frame.width, frame.y + frame.height
    bbox = (
        _clamp(left, frame.x, far_x),
        _clamp(top, frame.y, far_y),
        _clamp(right, frame.x, far_x),
        _clamp(bottom, frame.y, far_y),
    )
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError(f"region {width}x{height} at ({x},{y}) is outside the frame")
    return bbox


def capture_box_to_screen(box: Sequence[float], image_size: Point, bbox: Bbox) -> Bbox:
    """A box in pixels of a capture of ``bbox`` as screen coordinates.

    The scale comes from the capture itself: OCR reads a macOS Retina capture in pixels,
    twice the points of ``bbox``.
    """
    scale_x = (bbox[2] - bbox[0]) / image_size[0]
    scale_y = (bbox[3] - bbox[1]) / image_size[1]
    left, top, right, bottom = box
    return (
        bbox[0] + _round_half_up(left * scale_x),
        bbox[1] + _round_half_up(top * scale_y),
        bbox[0] + _round_half_up(right * scale_x),
        bbox[1] + _round_half_up(bottom * scale_y),
    )


def region_geometry(geometry: CaptureGeometry, bbox: Bbox) -> CaptureGeometry:
    """Capture geometry of ``bbox``, a part of ``geometry``: same pixels per screen unit."""
    left, top, right, bottom = bbox
    width, height = right - left, bottom - top
    return CaptureGeometry(
        x=left,
        y=top,
        width=width,
        height=height,
        pixel_width=max(1, _round_half_up(width * geometry.pixel_width / geometry.width)),
        pixel_height=max(1, _round_half_up(height * geometry.pixel_height / geometry.height)),
    )


def in_image(frame: Frame, x: int, y: int) -> bool:
    """Whether image point ``(x, y)`` lies inside the frame's image."""
    return 0 <= x < frame.image_width and 0 <= y < frame.image_height


def check_space(coords: str) -> str:
    """Validate an input tool's ``coords`` argument; returns ``"image"`` or ``"screen"``."""
    space = str(coords).strip().lower()
    if space not in COORD_SPACES:
        raise ValueError(f"coords must be 'image' or 'screen', got {coords!r}")
    return space


def contract_line(frame: Frame) -> str:
    """The one-line coordinate contract Snapshot / AnnotatedSnapshot print."""
    return (
        f"Image {frame.image_width}x{frame.image_height} = screen {frame.width}x{frame.height} "
        f"at origin ({frame.x},{frame.y}), scale {frame.scale:.3f}; "
        f"input tools take IMAGE pixels of this frame (monitor={frame.monitor}, max_width={frame.max_width})"
    )


def zoom_line(frame: Frame) -> str:
    """The reply line of Zoom: where its image is on the screen. The tool is view-only."""
    return (
        f"Zoom image {frame.image_width}x{frame.image_height} = screen {frame.width}x{frame.height} "
        f"at origin ({frame.x},{frame.y}), scale {frame.scale:.3f}; view only: input tools do NOT take "
        f'pixels of this image. Pass screen = origin + round(image * scale) with coords="screen"'
    )


def mapping_note(frame: Frame, image_points: Sequence[Point], screen_points: Sequence[Point]) -> str:
    """Suffix for an input tool's reply naming every image → screen mapping it applied."""
    pairs = ", ".join(
        f"image ({ix},{iy}) → screen ({sx},{sy})" for (ix, iy), (sx, sy) in zip(image_points, screen_points)
    )
    return f" [{pairs}; scale {frame.scale:.3f}]"
