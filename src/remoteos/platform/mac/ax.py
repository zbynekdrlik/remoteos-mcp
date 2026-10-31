"""The macOS accessibility tree (#37): ElementTree, ClickElement and TypeElement over AX.

The counterpart of platform/win/uia.py and the AT-SPI part of platform/linux/windows.py. What
is listed, the bounds and the refs are the shared :mod:`remoteos.element_tree`. Coordinates are
already macOS's screen space: AX reports points, top-left origin, global, as CGDisplayBounds.

``HIServices`` (pyobjc-framework-ApplicationServices) is imported at call time, so this module
always uses the framework in ``sys.modules`` and imports anywhere. AX objects belong to no
thread, and the messaging timeout bounds each call, so a walk with a deadline is bounded
and there is no worker thread as on Windows.

An element's value is never read: it can be a password. Nothing typed is ever logged.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from remoteos.element_tree import (
    SNAPSHOT_WALK_S,
    TREE_WALK_S,
    ElementFacts,
    ElementTree,
    RefTable,
    Walk,
    expires_in,
    gone,
    listed_elements,
    marked_from,
    may_have_happened,
    one_line,
    tree_from,
    walk,
)

log = logging.getLogger("remoteos.platform.mac.ax")

_MESSAGING_TIMEOUT_S = 2.0  # the default 6 s per call would let one hung app eat a whole walk
_FOCUS_WAIT_S = 1.0  # the focus moves a moment after the attribute is set
_FOCUS_POLL_S = 0.05
_PARENT_HOPS = 40

# AXError values (HIServices/AXError.h).
_OK = 0
_INVALID_ELEMENT = -25202
_NOT_ANSWERED = frozenset({-25204, -25211})  # CannotComplete (the app is busy or hung), APIDisabled
# The app plainly refused: an illegal argument, or an attribute or action this element lacks.
_REFUSED = frozenset({-25201, -25205, -25206, -25208})

_POINT_TYPE, _SIZE_TYPE = 1, 2  # kAXValueCGPointType, kAXValueCGSizeType

# AX roles as the shared roles of element_tree.INTERACTIVE_ROLES.
_ROLES = {
    "AXButton": "Button",
    "AXMenuButton": "Button",
    "AXCheckBox": "CheckBox",
    "AXRadioButton": "RadioButton",
    "AXTextField": "Edit",
    "AXTextArea": "Edit",
    "AXComboBox": "ComboBox",
    "AXPopUpButton": "ComboBox",
    "AXMenuItem": "MenuItem",
    "AXMenuBarItem": "MenuItem",
    "AXLink": "Hyperlink",
    "AXSlider": "Slider",
    "AXIncrementor": "Spinner",
    "AXRow": "ListItem",
    "AXCell": "DataItem",
    "AXDisclosureTriangle": "TreeItem",
}
_TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})


def _hi():
    import HIServices

    return HIServices


class _Vanished(Exception):
    """The element no longer exists."""


class _NotAnswered(RuntimeError):
    """The app did not answer a read. Never "no attribute": a hung app is no empty tree."""


def _check(err: int) -> None:
    if err == _INVALID_ELEMENT:
        raise _Vanished
    if err in _NOT_ANSWERED:
        raise _NotAnswered(f"The app did not answer the Accessibility API (error {err}): try again in a moment")


def _copy(hi, element, attribute: str):
    """``attribute`` of ``element``, ``None`` when it has none; raises when the element is gone or silent."""
    err, value = hi.AXUIElementCopyAttributeValue(element, attribute, None)
    _check(err)
    return value if err == _OK else None


def _axvalue(hi, value, kind: int):
    """The CGPoint or CGSize inside an AXValue."""
    got = hi.AXValueGetValue(value, kind, None)
    return got[1] if isinstance(got, tuple) else got  # the wheel documents the value alone; older ones paired it


def _rect(hi, element) -> tuple[int, int, int, int]:
    """The element's screen rect in points, empty when it has none."""
    position, size = _copy(hi, element, "AXPosition"), _copy(hi, element, "AXSize")
    if position is None or size is None:
        return 0, 0, 0, 0
    point, extent = _axvalue(hi, position, _POINT_TYPE), _axvalue(hi, size, _SIZE_TYPE)
    left, top = int(point.x), int(point.y)
    return left, top, left + int(extent.width), top + int(extent.height)


def _name(hi, element) -> str:
    for attribute in ("AXTitle", "AXDescription", "AXPlaceholderValue"):
        value = _copy(hi, element, attribute)
        if value:
            return str(value)
    return ""


def _settable(hi, element, attribute: str) -> bool:
    err, settable = hi.AXUIElementIsAttributeSettable(element, attribute, None)
    _check(err)
    return err == _OK and bool(settable)


def _enabled(hi, element) -> bool:
    """AXEnabled; an element without the attribute is enabled."""
    value = _copy(hi, element, "AXEnabled")
    return value is None or bool(value)


def _facts(hi, element) -> Optional[ElementFacts]:
    try:
        ax_role = _copy(hi, element, "AXRole")
        if ax_role is None:
            return None
        ax_role = str(ax_role)
        role = _ROLES.get(ax_role, ax_role.removeprefix("AX"))
        if _copy(hi, element, "AXSubrole") == "AXTabButton":
            role = "TabItem"  # a tab is a radio button with a subrole
        return ElementFacts(
            role=role,
            name=_name(hi, element),
            enabled=_enabled(hi, element),
            rect=_rect(hi, element),
            focusable=_settable(hi, element, "AXFocused"),
        )
    except _Vanished:
        log.debug("an element vanished during the walk")
        return None


def _children(hi, element) -> list:
    try:
        return list(_copy(hi, element, "AXChildren") or [])
    except _Vanished:
        log.debug("a branch vanished during the walk")
        return []


@dataclass
class _Entry:
    """What a ref keeps: the element, and the role and name that prove later it is the same one."""

    element: Any
    ax_role: str
    name: str


@dataclass(frozen=True)
class _Window:
    element: Any
    title: str


def _system_wide(hi):
    system = hi.AXUIElementCreateSystemWide()
    hi.AXUIElementSetMessagingTimeout(system, _MESSAGING_TIMEOUT_S)  # on this element: all of them
    return system


def _front_window(hi) -> _Window:
    app = _copy(hi, _system_wide(hi), "AXFocusedApplication")
    if app is None:
        raise LookupError("No foreground window")
    window = _copy(hi, app, "AXFocusedWindow") or _copy(hi, app, "AXMainWindow")
    if window is None:
        windows = _copy(hi, app, "AXWindows") or []
        window = windows[0] if windows else None
    if window is None:
        raise LookupError("The foreground app has no window")
    return _Window(window, str(_copy(hi, window, "AXTitle") or ""))


def _window_of(hi, pid: int, title: str) -> _Window:
    """The window of process ``pid`` with this title, else its focused or first one."""
    _system_wide(hi)
    app = hi.AXUIElementCreateApplication(pid)
    windows = list(_copy(hi, app, "AXWindows") or [])
    for window in windows:
        if str(_copy(hi, window, "AXTitle") or "") == title:
            return _Window(window, title)
    window = _copy(hi, app, "AXFocusedWindow") or (windows[0] if windows else None)
    if window is None:
        raise LookupError("That window's app shows no window to the Accessibility API")
    return _Window(window, str(_copy(hi, window, "AXTitle") or ""))


_refs = RefTable()
_tree_lock = threading.Lock()  # one reader of the refs: a tool can run beside AnnotatedSnapshot


def _walk(hi, window: _Window, expired: Callable[[], bool], **bounds: int) -> Walk:
    return walk(window.element, lambda e: _facts(hi, e), lambda e: _children(hi, e), expired=expired, **bounds)


def _entry_of(hi) -> Callable[[Any, ElementFacts], Optional[_Entry]]:
    def entry(element, facts: ElementFacts) -> Optional[_Entry]:
        try:
            return _Entry(element, str(_copy(hi, element, "AXRole")), one_line(facts.name.strip()))
        except _Vanished:
            log.debug("a listed element vanished")
            return None

    return entry


def read_tree(pid: Optional[int] = None, title: str = "") -> ElementTree:
    """The element tree of the window of process ``pid`` titled ``title``, or of the front one, with new refs."""
    hi = _hi()
    with _tree_lock:
        window = _window_of(hi, pid, title) if pid else _front_window(hi)
        result = _walk(hi, window, expires_in(TREE_WALK_S))
        tree = tree_from(_refs, one_line(window.title), result, _entry_of(hi))
    log.debug("AX: %d elements, truncated=%s", len(tree.items), bool(tree.truncated))
    return tree


def read_elements(limit: int) -> list[dict]:
    """Up to ``limit`` elements of the front window for Snapshot. Existing refs stay valid."""
    hi = _hi()
    with _tree_lock:
        return listed_elements(_walk(hi, _front_window(hi), expires_in(SNAPSHOT_WALK_S), limit=limit))


def read_marked(limit: int) -> tuple[list[dict], str]:
    """AnnotatedSnapshot's elements, each with a ref: a new tree, on Snapshot's short budget (#36)."""
    hi = _hi()
    with _tree_lock:
        result = _walk(hi, _front_window(hi), expires_in(SNAPSHOT_WALK_S), limit=limit)
        return marked_from(_refs, result, _entry_of(hi))


def _live(hi, ref: str) -> _Entry:
    """The entry behind ``ref``, proven to be that same element, still there and enabled."""
    entry = _refs.lookup(ref)
    try:
        role = _copy(hi, entry.element, "AXRole")
        name = one_line(_name(hi, entry.element).strip())
        enabled = _enabled(hi, entry.element)
    except _Vanished:
        raise gone(ref, entry.name) from None
    if str(role) != entry.ax_role or name != entry.name:  # a reused table row keeps its element and role
        raise gone(ref, entry.name)
    if not enabled:
        raise RuntimeError(f"Element {ref} ({entry.name!r}) is disabled")
    return entry


def _is_or_inside(hi, core_foundation, found, element) -> bool:
    """True when ``found`` is the element, or a part of it (a wrapper's inner field)."""
    try:
        for _ in range(_PARENT_HOPS):
            if found is None:
                return False
            if core_foundation.CFEqual(found, element):
                return True
            found = _copy(hi, found, "AXParent")
    except _Vanished:
        log.debug("an element vanished on the way up")
    return False


def _center(hi, ref: str, entry: _Entry) -> tuple[int, int]:
    """Where the mouse may click ``entry``, or a failure: a click never lands somewhere else."""
    import CoreFoundation

    try:
        left, top, right, bottom = _rect(hi, entry.element)
    except _Vanished:
        raise gone(ref, entry.name) from None
    if right <= left or bottom <= top:
        raise RuntimeError(f"Element {ref} ({entry.name!r}) is off-screen: bring it into view first")
    x, y = (left + right) // 2, (top + bottom) // 2
    err, hit = hi.AXUIElementCopyElementAtPosition(_system_wide(hi), float(x), float(y), None)
    if err != _OK or not _is_or_inside(hi, CoreFoundation, hit, entry.element):
        raise RuntimeError(
            f"Something else is on top of element {ref} ({entry.name!r}): a window, a dialog or a part of the page."
            " FocusWindow or close it first"
        )
    return x, y


def _press(hi, ref: str, entry: _Entry) -> bool:
    """AXPress ``entry``: True when it was accepted, False when the mouse may try.

    Any error but a plain refusal may be a press that happened: AX reports a modal dialog's
    blocked call as "cannot complete", and a button that closed its window as "invalid element".
    """
    err, names = hi.AXUIElementCopyActionNames(entry.element, None)
    if err != _OK or "AXPress" not in (names or ()):
        return False
    err = hi.AXUIElementPerformAction(entry.element, "AXPress")
    if err == _OK:
        return True
    if err in _REFUSED:
        log.debug("AXPress refused (%s): clicking instead", err)
        return False
    log.debug("AXPress did not answer (%s)", err)
    raise may_have_happened(ref, entry.name)


def click_element(ref: str, click: Callable[[int, int], str]) -> str:
    """AXPress the element, or click its centre with ``click(x, y)``."""
    hi = _hi()
    with _tree_lock:
        entry = _live(hi, ref)
        if _press(hi, ref, entry):
            return f"Invoked {ref} ({entry.name!r})"
        x, y = _center(hi, ref, entry)
    return f"{click(x, y)}: {ref} ({entry.name!r}) has no usable AXPress"


def _settable_or_gone(hi, ref: str, entry: _Entry, attribute: str) -> bool:
    try:
        return _settable(hi, entry.element, attribute)
    except _Vanished:
        raise gone(ref, entry.name) from None


def _set_value(hi, ref: str, entry: _Entry, text: str) -> bool:
    """Replace the element's AXValue: True when it is set, False when the caller types."""
    if not _settable_or_gone(hi, ref, entry, "AXValue"):
        if entry.ax_role in _TEXT_ROLES:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) is read-only")
        return False
    err = hi.AXUIElementSetAttributeValue(entry.element, "AXValue", text)
    if err == _OK:
        return True
    if err in _REFUSED:
        log.debug("AXValue refused (%s): typing instead", err)
        return False
    log.debug("AXValue did not answer (%s)", err)
    raise may_have_happened(ref, entry.name)


def _focused_element(hi):
    try:
        return _copy(hi, _system_wide(hi), "AXFocusedUIElement")
    except _Vanished:
        log.debug("the focused element vanished")
        return None


def _give_focus(hi, ref: str, entry: _Entry) -> None:
    """Bring the element's app to the front and the focus onto it, or raise: keys go to the focus."""
    import CoreFoundation

    err, pid = hi.AXUIElementGetPid(entry.element, None)
    if err == _OK:
        hi.AXUIElementSetAttributeValue(hi.AXUIElementCreateApplication(pid), "AXFrontmost", True)
    hi.AXUIElementSetAttributeValue(entry.element, "AXFocused", True)
    deadline = time.monotonic() + _FOCUS_WAIT_S
    while True:
        focused = _focused_element(hi)
        if focused is not None and _is_or_inside(hi, CoreFoundation, focused, entry.element):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) did not take the keyboard focus: nothing was typed")
        time.sleep(_FOCUS_POLL_S)


def type_element(ref: str, text: str, type_keys: Callable[[str], str]) -> str:
    """Set the element's value, or give it the focus and type with ``type_keys(text)``."""
    hi = _hi()
    with _tree_lock:
        entry = _live(hi, ref)
        if _set_value(hi, ref, entry, text):
            return f"Set the value of {ref} ({entry.name!r}) to {len(text)} chars"
        _give_focus(hi, ref, entry)
    return f"{type_keys(text)} into {ref} ({entry.name!r}), at its caret"
