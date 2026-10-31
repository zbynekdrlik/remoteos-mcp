"""Linux input: mouse and keyboard through xdotool, the clipboard through xclip."""

from __future__ import annotations

import logging

from remoteos.errors import DesktopUnavailable
from remoteos.platform.linux import session
from remoteos.platform.linux.session import _unavailable, _xdotool, get_session
from remoteos.scroll import wheel_steps
from remoteos.text_entry import enter_text

log = logging.getLogger("remoteos.platform.linux.desktop")  # one logger for the whole provider


# ---------------------------------------------------------------------------
# Input (mouse / keyboard) — xdotool
# ---------------------------------------------------------------------------

_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def click(x: int, y: int, button: str = "left", action: str = "click") -> str:
    """Mouse click / double-click / hover at screen coordinates."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    btn = _BUTTONS.get(button, 1)
    if action == "hover":
        _xdotool(["mousemove", str(x), str(y)])
        return f"Hovered at ({x},{y})"
    if action == "double":
        _xdotool(["mousemove", str(x), str(y), "click", "--repeat", "2", "--delay", "120", str(btn)])
        return f"Double-clicked {button} at ({x},{y})"
    _xdotool(["mousemove", str(x), str(y), "click", str(btn)])
    return f"Clicked {button} at ({x},{y})"


def type_unicode(text: str) -> int:
    """Type ``text`` at the keyboard focus with ``xdotool type`` (Unicode-safe, #30).

    Returns the number of characters handed to xdotool; a failing xdotool raises.
    """
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    _xdotool(["type", "--delay", "20", "--", text])
    return len(text)


def type_text(
    text: str,
    x: int | None = None,
    y: int | None = None,
    clear: bool = False,
    press_enter: bool = False,
    method: str = "keys",
) -> str:
    """Type text, optionally clicking screen point (x, y) first / clearing / pressing Enter.

    Text goes through ``remoteos.text_entry``: ``method="keys"`` = ``type_unicode``,
    ``"paste"`` = the clipboard (xclip + Ctrl+V), restoring its previous text (#30).
    """
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    if x is not None and y is not None:
        _xdotool(["mousemove", str(x), str(y), "click", "1"])
    if clear:
        _xdotool(["key", "--clearmodifiers", "ctrl+a"])
        _xdotool(["key", "Delete"])
    reply = enter_text(
        text,
        method,
        type_unicode=type_unicode,
        get_clipboard=get_clipboard,
        set_clipboard=set_clipboard,
        press_paste=lambda: _xdotool(["key", "--clearmodifiers", "ctrl+v"]),
    )
    if press_enter:
        _xdotool(["key", "Return"])
    return reply


_WHEEL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}  # X11 wheel buttons


def scroll(direction: str, clicks: int, x: int | None = None, y: int | None = None) -> str:
    """Turn the wheel ``clicks`` notches toward ``direction`` (#29): X11 button 4/5/6/7 × clicks."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    steps = wheel_steps(direction, clicks)
    if x is not None and y is not None:
        _xdotool(["mousemove", str(x), str(y)])
    _xdotool(["click", "--repeat", str(steps.clicks), _WHEEL_BUTTONS[steps.direction]])
    return steps.summary()


def move(
    x: int,
    y: int,
    drag: bool = False,
    start_x: int | None = None,
    start_y: int | None = None,
    duration: float = 0.3,
) -> str:
    """Move the mouse, or drag to the target from screen point (start_x, start_y) or the current position.

    A start point is used only when it ``is not None``: x=0 / y=0 is a real screen column/row (#31).
    """
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    if drag:
        if start_x is not None and start_y is not None:
            _xdotool(["mousemove", str(start_x), str(start_y)])
        _xdotool(["mousedown", "1"])
        _xdotool(["mousemove", str(x), str(y)])
        _xdotool(["mouseup", "1"])
        return f"Dragged to ({x},{y})"
    _xdotool(["mousemove", str(x), str(y)])
    return f"Moved to ({x},{y})"


# Map pyautogui-style key names to the X keysym names xdotool expects.
_KEY_ALIASES = {
    "win": "super",
    "cmd": "super",
    "command": "super",
    "super": "super",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "del": "Delete",
    "delete": "Delete",
    "ins": "Insert",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
}


def _map_key(part: str) -> str:
    p = part.strip().lower()
    return _KEY_ALIASES.get(p, part.strip())


def shortcut(keys: str) -> str:
    """Execute a keyboard shortcut, e.g. ``ctrl+c`` / ``alt+Tab`` / ``super+e``."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    combo = "+".join(_map_key(k) for k in keys.split("+") if k.strip())
    if not combo:
        raise ValueError("No keys given")
    _xdotool(["key", combo])
    return f"Executed shortcut: {keys}"


# ---------------------------------------------------------------------------
# Clipboard — xclip (a failure raises, #32)
# ---------------------------------------------------------------------------


def get_clipboard() -> str:
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    try:
        r = session._run_x(["xclip", "-selection", "clipboard", "-o"])
    except RuntimeError as e:
        # xclip -o exits 1 with "target STRING not available" when the clipboard is empty or
        # holds no text (an image): that is "no text", not a failure
        if "not available" in str(e):
            log.debug("clipboard holds no text")
            return ""
        raise
    return (r.stdout or b"").decode("utf-8", "replace")


def set_clipboard(text: str) -> str:
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    # xclip forks to stay resident serving the selection — discard_output so
    # the inherited pipe doesn't keep subprocess.run blocked until timeout.
    session._run_x(
        ["xclip", "-selection", "clipboard"],
        input_bytes=text.encode("utf-8"),
        discard_output=True,
    )
    return "Clipboard set"
