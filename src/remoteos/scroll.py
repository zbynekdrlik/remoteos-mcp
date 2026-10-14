"""Scroll units (#29): wheel CLICKS + a direction, the same on every OS.

One click is one notch of a real mouse wheel; the direction is up / down / left /
right (the computer-use ``scroll`` action). The ``Scroll`` tool normalises its
arguments with ``normalize_request`` — the ``direction`` form, or the legacy
signed ``amount`` + ``horizontal`` (positive = up/right) — and every provider
turns ``(direction, clicks)`` into its native wheel event via ``wheel_steps``:
Windows ``SendInput`` (``clicks × WHEEL_DELTA``), macOS Quartz line events
(one line per click), Linux xdotool buttons 4/5/6/7.

Pure: no I/O, no OS imports.
"""

from __future__ import annotations

from dataclasses import dataclass

DIRECTIONS = ("up", "down", "left", "right")
MIN_CLICKS, MAX_CLICKS = 1, 100
# Windows: the wheel delta of one notch. MAX_CLICKS × WHEEL_DELTA = 12000 stays inside
# the signed 16-bit delta that WM_MOUSEWHEEL / WM_MOUSEHWHEEL carry in HIWORD(wParam).
WHEEL_DELTA = 120


@dataclass(frozen=True)
class WheelSteps:
    """``clicks`` wheel notches toward ``direction``; ``requested`` = the count asked for."""

    direction: str
    clicks: int
    requested: int

    @property
    def horizontal(self) -> bool:
        return self.direction in ("left", "right")

    @property
    def signed_clicks(self) -> int:
        """Clicks with the wheel sign: positive = up/right, negative = down/left."""
        return self.clicks if self.direction in ("up", "right") else -self.clicks

    @property
    def dy(self) -> int:
        """Vertical clicks, positive = up (0 for a horizontal scroll)."""
        return 0 if self.horizontal else self.signed_clicks

    @property
    def dx(self) -> int:
        """Horizontal clicks, positive = right (0 for a vertical scroll)."""
        return self.signed_clicks if self.horizontal else 0

    @property
    def delta(self) -> int:
        """Windows wheel delta (``mouseData``): ±clicks × WHEEL_DELTA, positive = up/right."""
        return self.signed_clicks * WHEEL_DELTA

    def summary(self) -> str:
        """The tool reply, e.g. ``Scrolled 3 clicks down`` (plus a note when clamped)."""
        unit = "click" if self.clicks == 1 else "clicks"
        text = f"Scrolled {self.clicks} {unit} {self.direction}"
        if self.clicks != self.requested:
            text += f" (requested {self.requested}, clamped to {MIN_CLICKS}..{MAX_CLICKS})"
        return text


def wheel_steps(direction: str, clicks: int) -> WheelSteps:
    """Validate ``direction`` (case/space-insensitive) and clamp ``clicks`` to 1..100."""
    name = str(direction).strip().lower()
    if name not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)} (got {direction!r})")
    requested = int(clicks)
    return WheelSteps(direction=name, clicks=min(max(requested, MIN_CLICKS), MAX_CLICKS), requested=requested)


def normalize_request(amount: int, direction: str = "", horizontal: bool = False) -> WheelSteps:
    """The ``Scroll`` tool's arguments → ``WheelSteps``.

    With ``direction``, ``amount`` is the click count: its sign and ``horizontal``
    are ignored (the explicit direction wins). Without it — the legacy form — the
    sign of ``amount`` picks the direction on the axis ``horizontal`` selects:
    positive = up/right, zero or negative = down/left (as the Linux provider
    always did).
    """
    count = int(amount)
    if direction and str(direction).strip():
        return wheel_steps(direction, abs(count))
    if horizontal:
        return wheel_steps("right" if count > 0 else "left", abs(count))
    return wheel_steps("up" if count > 0 else "down", abs(count))


def chunks(total: int, limit: int) -> list[int]:
    """Split a signed ``total`` into signed parts of at most ``limit`` (25, 10 → 10, 10, 5)."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1 (got {limit})")
    sign = -1 if total < 0 else 1
    left, parts = abs(total), []
    while left:
        part = min(left, limit)
        parts.append(sign * part)
        left -= part
    return parts
