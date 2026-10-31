"""Key and mouse-action names shared by the input tools and the providers (#34).

Pure except for :func:`held_keys`, which only calls a provider's ``key_down`` / ``key_up``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger("remoteos.input_keys")

BUTTONS = ("left", "right", "middle")
CLICK_ACTIONS = ("click", "double", "triple", "hover", "down", "up")
MODIFIERS = ("ctrl", "shift", "alt", "super")

MAX_HOLD_S = 30.0
MAX_REPEAT = 100

_ALIASES = {
    "control": "ctrl",
    "option": "alt",
    "win": "super",
    "cmd": "super",
    "command": "super",
    "meta": "super",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
}


def canonical(key: str) -> str:
    """The one name of a modifier or an alias. Any other key keeps its case: X keysyms are case-sensitive."""
    name = key.strip()
    lower = name.lower()
    return _ALIASES.get(lower, lower if lower in MODIFIERS else name)


def split_keys(keys: str) -> list[str]:
    """``"ctrl+shift"`` → ``["ctrl", "shift"]``. An empty part raises: a typo must not hold fewer keys."""
    if not keys.strip():
        return []
    parts = [canonical(part) for part in keys.split("+")]
    if not all(parts):
        raise ValueError(f"Bad key combination: {keys!r}")
    return parts


def check_modifiers(modifiers: str) -> list[str]:
    """The modifier keys of ``modifiers`` (``""`` = none). Any other key raises before input is sent."""
    keys = split_keys(modifiers)
    unknown = [key for key in keys if key not in MODIFIERS]
    if unknown:
        raise ValueError(f"Not a modifier: {', '.join(unknown)} (use ctrl, shift, alt, win/cmd)")
    return keys


def check_click(button: str, action: str) -> None:
    """Raise on a button or action that does not exist, before any input is sent."""
    if button not in BUTTONS:
        raise ValueError(f"Unknown button {button!r} (use {', '.join(BUTTONS)})")
    if action not in CLICK_ACTIONS:
        raise ValueError(f"Unknown action {action!r} (use {', '.join(CLICK_ACTIONS)})")


def check_repeat(repeat: int) -> int:
    """``repeat`` when it is 1..MAX_REPEAT; anything else raises instead of pressing a surprising count."""
    if not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat must be 1..{MAX_REPEAT}, got {repeat}")
    return repeat


def hold_seconds(seconds: float) -> float:
    """``seconds`` capped at MAX_HOLD_S. Not positive (NaN included) raises."""
    if not seconds > 0:
        raise ValueError(f"seconds must be above 0, got {seconds}")
    return min(seconds, MAX_HOLD_S)


def _release(keys: list[str], desktop) -> list[str]:
    """Release ``keys`` in reverse order, every one even when another fails; returns the failed ones."""
    stuck = []
    for key in reversed(keys):
        try:
            desktop.key_up(key)
        except Exception as e:
            log.error("key release failed (%s)", type(e).__name__)
            stuck.append(key)
    return stuck


@contextmanager
def held_keys(keys: list[str], desktop) -> Iterator[None]:
    """Hold ``keys`` on ``desktop`` for the body; each pressed key is released, also on any failure.

    A failed release after a good body raises: a key left down would drive every later input.
    After a failed body the body's error stays the one reported, the release failure is logged.
    """
    pressed: list[str] = []
    for key in keys:
        desktop.check_key(key)  # a combination with an unknown key presses nothing
    try:
        for key in keys:
            desktop.key_down(key)
            pressed.append(key)
        yield
    except BaseException:
        _release(pressed, desktop)
        raise
    stuck = _release(pressed, desktop)
    if stuck:
        raise RuntimeError(f"could not release {', '.join(stuck)}: the key may still be held down")
