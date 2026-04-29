"""Linux desktop stubs — headless server, no display available."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

HAS_WIN32 = False  # compatibility flag — always False on Linux

_MSG = "Not available on headless Linux (no display)"


# ---------------------------------------------------------------------------
# Window info
# ---------------------------------------------------------------------------


@dataclass
class WindowInfo:
    handle: int
    title: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom
    visible: bool
    pid: int = 0

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def enumerate_windows() -> list[WindowInfo]:
    """Return empty list — no display on headless Linux."""
    return []


def get_interactive_elements() -> list[dict]:
    """Return empty list — no display on headless Linux."""
    return []


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def take_screenshot(quality: int = 65, max_width: int = 1280, monitor: int = 0) -> str:
    """Not available on headless Linux."""
    raise RuntimeError(_MSG)


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Not available on headless Linux."""
    return _MSG


def minimize_all() -> str:
    """Not available on headless Linux."""
    return _MSG


def launch_app(name: str, args: str = "") -> str:
    """Not available on headless Linux."""
    return _MSG


def resize_window(handle: int, width: int, height: int) -> str:
    """Not available on headless Linux."""
    return _MSG


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def get_clipboard() -> str:
    """Not available on headless Linux."""
    return _MSG


def set_clipboard(text: str) -> str:
    """Not available on headless Linux."""
    return _MSG


# ---------------------------------------------------------------------------
# Lock screen / Notification
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    """Not available on headless Linux."""
    return _MSG


def show_notification(title: str, message: str) -> str:
    """Not available on headless Linux."""
    return _MSG
