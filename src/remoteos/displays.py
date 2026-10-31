"""Display inventory (#33): what each ``monitor`` value of the tools means on this machine.

Providers report a :class:`DisplayInfo` per display; :func:`inventory_text` renders the
DisplayInventory reply. Pure, no OS calls. Bounds are in SCREEN coordinates, the space of
``remoteos.coords``. ``scale`` is what the OS reports: Windows effective DPI / 96, macOS
capture pixels per point, ``None`` on X11, which has no per-monitor scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from remoteos.coords import CaptureGeometry


@dataclass(frozen=True)
class DisplayInfo:
    index: int  # the tools' monitor=N
    x: int
    y: int
    width: int
    height: int
    scale: Optional[float]
    primary: bool
    name: str


def _display_line(display: DisplayInfo) -> str:
    scale = "n/a" if display.scale is None else f"{display.scale:.3f}"
    primary = ", primary" if display.primary else ""
    return (
        f"  [{display.index}] {display.name}: {display.width}x{display.height} "
        f"at origin ({display.x},{display.y}), scale {scale}{primary}"
    )


def inventory_text(monitor_zero: str, root: CaptureGeometry, displays: Iterable[DisplayInfo]) -> str:
    """The DisplayInventory reply: what ``monitor=0`` is, then one line per ``monitor=N``."""
    lines = [
        f"**monitor=0:** {monitor_zero}: {root.width}x{root.height} at origin ({root.x},{root.y})",
        "",
        "**Displays (monitor=N):**",
        *(_display_line(d) for d in displays),
    ]
    return "\n".join(lines)
