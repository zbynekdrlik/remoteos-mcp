"""Linux windows: the wmctrl list, the AT-SPI element tree, focus, launch, resize."""

from __future__ import annotations

import logging
import os
import subprocess
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
from remoteos.errors import DesktopUnavailable
from remoteos.platform.linux import session, x11_input
from remoteos.platform.linux.session import _build_invocation, _unavailable, _xdotool, get_session

log = logging.getLogger("remoteos.platform.linux.desktop")  # one logger for the whole provider


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


# ---------------------------------------------------------------------------
# Window enumeration / management
# ---------------------------------------------------------------------------


def _parse_wmctrl_windows(text: str) -> list[WindowInfo]:
    """Parse ``wmctrl -lpG`` output into WindowInfo list (pure).

    Columns: ``winid desktop pid x y w h host title...``.
    """
    results: list[WindowInfo] = []
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        try:
            handle = int(parts[0], 16)
            pid = int(parts[2])
            x, y, w, h = int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6])
        except ValueError:
            continue
        title = parts[8] if len(parts) >= 9 else ""
        results.append(WindowInfo(handle=handle, title=title, rect=(x, y, x + w, y + h), visible=True, pid=pid))
    return results


def list_windows() -> list[WindowInfo]:
    """The open top-level windows; a wmctrl failure raises, for a caller that polls (#34)."""
    r = session._run_x(["wmctrl", "-lpG"])
    return _parse_wmctrl_windows((r.stdout or b"").decode("utf-8", "replace"))


def enumerate_windows() -> list[WindowInfo]:
    """List open top-level windows (via wmctrl). Empty list when not on X11."""
    if get_session().mode != "x11":
        return []
    try:
        return list_windows()
    except RuntimeError as e:
        log.warning("enumerate_windows failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# The element tree: AT-SPI2 (#7, #38)
# ---------------------------------------------------------------------------
#
# The Linux counterpart of UI Automation (platform/win/uia.py) and AX (platform/mac/ax.py),
# reached in-process via GObject-Introspection (``gi.repository.Atspi``). What is listed, the
# bounds and the refs are the shared remoteos.element_tree. D-Bus calls have a method timeout, so
# a walk with a deadline is bounded and there is no worker thread as on Windows.
#
# Coverage is app-dependent: GTK apps expose a rich tree; Qt and Chromium/Electron expose theirs
# only when asked (see _enable_hint); legacy X apps expose nothing. Snapshot's list degrades to
# empty; ElementTree reports a failure and says how to enable an app that shows nothing.

# AT-SPI role names (never localized) as the shared roles of element_tree.INTERACTIVE_ROLES.
_ROLES = {
    "push button": "Button",
    "toggle button": "Button",
    "button": "Button",
    "switch": "CheckBox",
    "check box": "CheckBox",
    "radio button": "RadioButton",
    "menu item": "MenuItem",
    "check menu item": "MenuItem",
    "radio menu item": "MenuItem",
    "menu": "MenuItem",
    "entry": "Edit",
    "password text": "Edit",
    "text box": "Edit",
    "combo box": "ComboBox",
    "list item": "ListItem",
    "table cell": "DataItem",
    "table column header": "HeaderItem",
    "column header": "HeaderItem",
    "link": "Hyperlink",
    "slider": "Slider",
    "spin button": "Spinner",
    "page tab": "TabItem",
    "tab": "TabItem",
    "tree item": "TreeItem",
}

# The action that means "operate it", best first. Never just the first action: Qt's list holds
# SetFocus and ShowMenu too, and GTK 2's "press" is only half of "click".
_PRESS_ACTIONS = ("click", "activate", "press", "toggle", "jump")

_FOCUS_WAIT_S = 1.0  # focus changes reach AT-SPI and the window manager a moment after the call
_FOCUS_POLL_S = 0.05

_QT_HINT = "a Qt app shows them when started with QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1"
# Both halves are needed: with the flag alone Chrome never joined the a11y bus (real Chrome, #38).
_CHROMIUM_HINT = (
    "a Chromium or Electron app shows them when started with ACCESSIBILITY_ENABLED=1 in its environment and"
    " --force-renderer-accessibility"
)


def _enable_hint(toolkit: str) -> str:
    """What would make an app that lists nothing show its controls ("" when its toolkit has no switch)."""
    name = toolkit.lower()
    if "gtk" in name:
        return ""
    if "qt" in name:
        return f"this app shows no controls to AT-SPI: {_QT_HINT}"
    if "chrom" in name or "electron" in name:
        return f"this app shows no controls to AT-SPI: {_CHROMIUM_HINT}"
    return f"this app shows no controls to AT-SPI: {_QT_HINT}; {_CHROMIUM_HINT}"


class _AtspiAdapter:
    """Thin adapter over a real ``Atspi.Accessible`` node, for the shared walk.

    A node that fails under the walk went away: ``facts`` is then ``None``, and the walk
    skips it. Nothing else of the library is reached outside this class and the actions.

    ``coord`` selects how ``extents`` maps to screen pixels. GTK4 apps report
    ``CoordType.SCREEN`` extents as ``(0, 0)`` (a gtk-at-spi limitation), so the
    default is ``"window"``: read the reliable WINDOW-relative extents and add the
    window's screen ``offset`` (its content origin — see :func:`_content_origin`),
    which lands boxes exactly on the elements. ``"screen"`` reads SCREEN extents
    directly (fallback when the window geometry can't be resolved).
    """

    def __init__(self, atspi, coord: str = "window", offset: tuple[int, int] = (0, 0)):
        self._a = atspi
        self._coord = coord
        self._ox, self._oy = offset

    def children(self, node) -> list:
        try:
            return [c for c in (node.get_child_at_index(i) for i in range(node.get_child_count())) if c is not None]
        except Exception as e:
            log.debug("a branch vanished during the walk (%s)", type(e).__name__)
            return []

    def extents(self, node) -> tuple[int, int, int, int]:
        """The screen rect, empty when the node has none (not every node has a Component)."""
        try:
            coord_type = self._a.CoordType.WINDOW if self._coord == "window" else self._a.CoordType.SCREEN
            r = node.get_extents(coord_type)
            left, top = int(r.x) + self._ox, int(r.y) + self._oy
            return left, top, left + int(r.width), top + int(r.height)
        except Exception as e:
            log.debug("an element has no extents (%s)", type(e).__name__)
            return 0, 0, 0, 0

    def facts(self, node) -> Optional[ElementFacts]:
        try:
            states = node.get_state_set()
            role_name = (node.get_role_name() or "").strip().lower()
            name = node.get_name() or ""
        except Exception as e:
            log.debug("an element vanished during the walk (%s)", type(e).__name__)
            return None
        state = self._a.StateType
        if states.contains(state.DEFUNCT):
            return None
        role = _ROLES.get(role_name, role_name.title().replace(" ", ""))
        if role_name == "text" and states.contains(state.EDITABLE):
            role = "Edit"  # the same role names static text in many toolkits
        return ElementFacts(
            role=role,
            name=name.strip(),
            enabled=states.contains(state.ENABLED),
            rect=self.extents(node),
            focusable=states.contains(state.FOCUSABLE),
            offscreen=not states.contains(state.SHOWING),
        )


def _load_atspi():
    """Import the ``Atspi`` GI module with the session's a11y bus reachable.

    In-process AT-SPI reads ``DBUS_SESSION_BUS_ADDRESS`` / ``DISPLAY`` / ... from
    THIS process's environment to find the user's accessibility bus, so we inject
    the resolved-session values first (a systemd service usually starts with none)
    and force the a11y-bus connection to be established NOW, while they are set.
    ``Atspi`` (via libdbus) caches that connection process-wide, so the later tree
    walk keeps working after we RESTORE the original environment in the ``finally``.

    Restoring is essential: leaking these keys into the global ``os.environ`` would
    poison every later ``Shell`` tool call (which inherits the process env) AND make
    a root-service force-redetect misclassify itself as a uid-0 env session (which
    drops the ``sudo -u <user>`` wrap and breaks clipboard/notifications). Raises on
    any failure; callers degrade to an empty element list.
    """
    s = get_session()
    keys = ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    overrides = {
        "DBUS_SESSION_BUS_ADDRESS": s.dbus_address,
        "DISPLAY": s.display,
        "XAUTHORITY": s.xauthority,
        "XDG_RUNTIME_DIR": s.runtime_dir,
    }
    try:
        for k, v in overrides.items():
            if v:
                os.environ[k] = v
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        # Establish (and let Atspi/libdbus cache) the a11y-bus connection while the
        # session env is live, so the tree walk still works once we restore it.
        Atspi.get_desktop(0)
        return Atspi
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _frames(app) -> list:
    """The top-level windows of an AT-SPI application."""
    try:
        return [f for f in (app.get_child_at_index(i) for i in range(app.get_child_count())) if f is not None]
    except Exception as e:
        log.debug("an application vanished (%s)", type(e).__name__)
        return []


def _apps(atspi) -> list:
    """The applications on the a11y bus."""
    return _frames(atspi.get_desktop(0))


def _has_state(atspi, node, state) -> bool:
    try:
        return bool(node.get_state_set().contains(state))
    except Exception as e:
        log.debug("a node has no readable state (%s)", type(e).__name__)
        return False


def _atspi_active_frame(atspi):
    """Return the ACTIVE top-level window (frame), or the first SHOWING one.

    Iterates the AT-SPI desktop's applications and their window children. Prefers
    a frame in the ACTIVE state (the focused window); falls back to the first
    SHOWING frame; returns ``None`` when nothing qualifies.
    """
    fallback = None
    for app in _apps(atspi):
        for win in _frames(app):
            if _has_state(atspi, win, atspi.StateType.ACTIVE):
                return win
            if fallback is None and _has_state(atspi, win, atspi.StateType.SHOWING):
                fallback = win
    return fallback


def _app_of_pid(atspi, pid: int):
    """The AT-SPI application of process ``pid``, or ``None``: an app that exposes nothing is not on the bus."""
    for app in _apps(atspi):
        try:
            if app.get_process_id() == pid:
                return app
        except Exception as e:
            log.debug("an application vanished (%s)", type(e).__name__)
    return None


def _frame_titled(frames: list, title: str):
    for frame in frames:
        try:
            if (frame.get_name() or "") == title:
                return frame
        except Exception as e:
            log.debug("a window vanished (%s)", type(e).__name__)
    return None


def _atspi_root(atspi, target: Optional[WindowInfo]):
    """Where to walk for ``target``, or ``None`` when AT-SPI knows nothing of it.

    Application-scoped: the X window's process names the AT-SPI application, because a Qt or
    Electron frame often never reports ACTIVE or SHOWING. Inside it, the frame of that title, then
    the active or a showing frame. An application with frames none of which say so is walked
    whole. Without a process id only an exact title can name the window: another app's
    active frame would be a list of the wrong window.
    """
    if target is None:
        return _atspi_active_frame(atspi)
    if not target.pid:
        return _frame_titled([f for app in _apps(atspi) for f in _frames(app)], target.title)
    app = _app_of_pid(atspi, target.pid)
    if app is None:
        return None
    frames = _frames(app)
    titled = _frame_titled(frames, target.title)
    if titled is not None:
        return titled
    for state in (atspi.StateType.ACTIVE, atspi.StateType.SHOWING):
        for frame in frames:
            if _has_state(atspi, frame, state):
                return frame
    return app


def _content_origin(win_geo: tuple[int, int, int, int], frame_w: int, frame_h: int) -> tuple[int, int]:
    """Screen origin of the app's content area (PURE).

    ``win_geo`` is the X toplevel ``(x, y, w, h)`` (from xdotool — for a GTK
    client-side-decorated window that includes the invisible resize/shadow
    margin). The AT-SPI frame reports only the ``frame_w x frame_h`` content, so
    the content's top-left sits half the size difference inside the toplevel.
    For a server-decorated window (no shadow) the difference is ~0 and the origin
    is just ``(x, y)``. Adding this origin to WINDOW-relative element extents
    yields correct screen coordinates.
    """
    x, y, w, h = win_geo
    return x + max(0, (w - frame_w) // 2), y + max(0, (h - frame_h) // 2)


def _active_handle() -> Optional[int]:
    """The focused X window, or ``None``."""
    try:
        return int(_xdotool(["getactivewindow"]).strip())
    except (RuntimeError, ValueError) as e:
        log.debug("no active window (%s)", type(e).__name__)
        return None


def _window_geometry(handle: int) -> Optional[tuple[int, int, int, int]]:
    """Window ``handle``'s ``(x, y, w, h)`` via xdotool, or ``None``."""
    try:
        out = _xdotool(["getwindowgeometry", "--shell", str(handle)])
    except RuntimeError:
        return None
    g: dict[str, int] = {}
    for line in out.splitlines():
        key, _, val = line.partition("=")
        val = val.strip()
        if val.lstrip("-").isdigit():
            g[key.strip()] = int(val)
    if all(k in g for k in ("X", "Y", "WIDTH", "HEIGHT")):
        return g["X"], g["Y"], g["WIDTH"], g["HEIGHT"]
    return None


def _atspi_coord_strategy(atspi, frame, geo: Optional[tuple[int, int, int, int]]) -> tuple[str, tuple[int, int]]:
    """Decide how to map AT-SPI extents to screen pixels for ``frame`` in the X window at ``geo``.

    Returns ``("window", (ox, oy))`` — read WINDOW-relative extents and add the
    content origin ``(ox, oy)`` — whenever the window geometry is resolvable (the
    reliable path; GTK4 reports SCREEN as ``(0,0)``). Falls back to ``("screen",
    (0, 0))`` when the frame already reports a real (non-origin) SCREEN position,
    else best-effort raw WINDOW coords.
    """
    try:
        fw = frame.get_extents(atspi.CoordType.WINDOW)
        fs = frame.get_extents(atspi.CoordType.SCREEN)
    except Exception:  # an application root has no Component
        fw = fs = None
    if geo is not None and fw is not None:
        return "window", _content_origin(geo, int(fw.width), int(fw.height))
    if fs is not None and (int(fs.x) > 0 or int(fs.y) > 0):
        return "screen", (0, 0)  # toolkit reports real SCREEN coords
    return "window", (0, 0)  # last resort: window-relative positions only


@dataclass
class _Entry:
    """What a ref keeps: the node, what proves it later, and its window for the click."""

    node: Any
    role_name: str
    name: str
    handle: int
    root: Any


@dataclass
class _Reading:
    """One walk's place: a window, the node to walk, how its coordinates map."""

    atspi: Any
    target: Optional[WindowInfo]
    root: Any
    adapter: Optional[_AtspiAdapter]

    def walk(self, expired: Callable[[], bool], **bounds: int) -> Walk:
        if self.root is None:
            return Walk([])
        return walk(self.root, self.adapter.facts, self.adapter.children, expired=expired, **bounds)

    def entry(self, node, facts: ElementFacts) -> _Entry:
        handle = self.target.handle if self.target else 0
        try:
            role_name = (node.get_role_name() or "").strip().lower()
        except Exception as e:
            log.debug("a listed element vanished (%s)", type(e).__name__)
            return None
        return _Entry(node, role_name, one_line(facts.name), handle, self.root)

    def hint(self) -> str:
        if self.root is None:
            return _enable_hint("")
        try:
            return _enable_hint(self.root.get_toolkit_name() or "")
        except Exception as e:
            log.debug("no toolkit name (%s)", type(e).__name__)
            return _enable_hint("")


_refs = RefTable()
_tree_lock = threading.Lock()  # one reader of the refs and of libatspi: a tool can run beside AnnotatedSnapshot


def _target(window: str) -> Optional[WindowInfo]:
    """The window named by part of a title, or the active one; ``None`` when none can be told."""
    if window:
        handle = _window_by_title(window)
    else:
        handle = _active_handle()
        if handle is None:
            return None
    for w in list_windows():
        if w.handle == handle:
            return w
    return WindowInfo(handle=handle, title="", rect=(0, 0, 0, 0), visible=True)


def _reading(window: str = "") -> _Reading:
    atspi = _load_atspi()
    target = _target(window)
    root = _atspi_root(atspi, target)
    if root is None:
        log.info("AT-SPI: nothing known of the target window")
        return _Reading(atspi, target, None, None)
    geo = _window_geometry(target.handle) if target else None
    coord, offset = _atspi_coord_strategy(atspi, root, geo)
    log.debug("AT-SPI coordinates: %s, offset %s", coord, offset)
    return _Reading(atspi, target, root, _AtspiAdapter(atspi, coord=coord, offset=offset))


def get_interactive_elements(limit: int = 300) -> list[dict]:
    """Up to ``limit`` interactive elements of the focused window for Snapshot; existing refs stay valid.

    Empty on a non-X11 box (headless contract — never shells out / imports gi), when AT-SPI is
    unavailable, or when the app exposes nothing: a failure costs the list, never the image.
    Coordinates are screen pixels (see :func:`_atspi_coord_strategy`).
    """
    if get_session().mode != "x11":
        return []
    try:
        with _tree_lock:
            result = _reading().walk(expires_in(SNAPSHOT_WALK_S), limit=limit)
            return listed_elements(result)
    except Exception as e:
        log.warning("no AT-SPI element list (%s)", type(e).__name__)
        return []


def marked_elements(limit: int = 300) -> tuple[list[dict], str]:
    """:func:`get_interactive_elements` with a ref on each, and why the list is cut, if it is (#36).

    It starts a new ref tree, so it ends the refs of an earlier ElementTree. A failure costs
    the list and leaves those refs alone.
    """
    if get_session().mode != "x11":
        return [], ""
    try:
        with _tree_lock:
            reading = _reading()
            elements, cut = marked_from(_refs, reading.walk(expires_in(SNAPSHOT_WALK_S), limit=limit), reading.entry)
            return elements, cut or ("" if elements else reading.hint())
    except Exception as e:
        log.warning("no AT-SPI element list (%s)", type(e).__name__)
        return [], ""


def element_tree(window: str = "") -> ElementTree:
    """The element tree of the window whose title matches ``window``, or of the focused one, with new refs."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    with _tree_lock:
        reading = _reading(window)
        result = reading.walk(expires_in(TREE_WALK_S))
        title = one_line(reading.target.title if reading.target else "")
        tree = tree_from(_refs, title, result, reading.entry)
        log.info("AT-SPI: %d elements, truncated=%s", len(tree.items), bool(tree.truncated))
        if not tree.items and not tree.truncated:
            return ElementTree(title=title, items=(), hint=reading.hint())
        return tree


def _live(atspi, ref: str) -> _Entry:
    """The entry behind ``ref``, proven to be that same element, still there and enabled."""
    entry = _refs.lookup(ref)
    stale = gone(ref, entry.name)
    try:
        states = entry.node.get_state_set()
        role_name = (entry.node.get_role_name() or "").strip().lower()
    except Exception:
        raise stale from None
    if states.contains(atspi.StateType.DEFUNCT) or role_name != entry.role_name:
        raise stale
    if entry.handle and all(w.handle != entry.handle for w in list_windows()):  # a closed window's node may answer
        raise stale
    if not states.contains(atspi.StateType.ENABLED):
        raise RuntimeError(f"Element {ref} ({entry.name!r}) is disabled")
    return entry


def _interfaces(node) -> list:
    try:
        return list(node.get_interfaces())
    except Exception as e:
        log.debug("a node has no interface list (%s)", type(e).__name__)
        return []


def _press_action(node) -> Optional[int]:
    """The index of the action that operates ``node``, or ``None``."""
    if "Action" not in _interfaces(node):
        return None
    try:
        names = [(node.get_action_name(i) or "").lower() for i in range(node.get_n_actions())]
    except Exception as e:
        log.debug("no action list (%s)", type(e).__name__)
        return None
    for wanted in _PRESS_ACTIONS:
        if wanted in names:
            return names.index(wanted)
    return None


def _center(atspi, ref: str, entry: _Entry) -> tuple[int, int]:
    """Where the mouse may click ``entry``, or a failure: a click never lands somewhere else.

    AT-SPI has no reliable hit test across toolkits, so the element's own window must be the
    active one: that is what rules out a dialog or another window on top of it.
    """
    if not _has_state(atspi, entry.node, atspi.StateType.SHOWING):
        raise RuntimeError(f"Element {ref} ({entry.name!r}) is off-screen: bring it into view first")
    if _active_handle() != entry.handle:
        raise RuntimeError(
            f"Something else is on top of element {ref} ({entry.name!r}): a window or a dialog."
            " FocusWindow or close it first"
        )
    coord, offset = _atspi_coord_strategy(atspi, entry.root, _window_geometry(entry.handle))  # it may have moved
    left, top, right, bottom = _AtspiAdapter(atspi, coord=coord, offset=offset).extents(entry.node)
    if right <= left or bottom <= top:
        raise RuntimeError(f"Element {ref} ({entry.name!r}) is off-screen: bring it into view first")
    return (left + right) // 2, (top + bottom) // 2


def click_element(ref: str) -> str:
    """Do the element's own press action, else click its centre with the left button.

    A returned ``False`` is the app's refusal, so the mouse may try. An exception is a call
    that may have reached the app (a modal dialog blocks it until D-Bus gives up), and only
    an error saying so is safe: the mouse would press a second time.
    """
    with _tree_lock:
        atspi = _load_atspi()
        entry = _live(atspi, ref)
        action = _press_action(entry.node)
        if action is not None:
            try:
                if entry.node.do_action(action):
                    return f"Invoked {ref} ({entry.name!r})"
            except Exception as e:
                log.debug("the action did not answer (%s)", type(e).__name__)
                raise may_have_happened(ref, entry.name) from None
            log.debug("the action was refused: clicking instead")
        x, y = _center(atspi, ref, entry)
    return f"{x11_input.click(x, y)}: {ref} ({entry.name!r}) has no usable action"


def _focused_in(atspi, node, depth: int = 3) -> bool:
    """True when ``node`` or a part of it (a wrapper's inner edit) has the focus."""
    if _has_state(atspi, node, atspi.StateType.FOCUSED):
        return True
    if depth == 0:
        return False
    return any(_focused_in(atspi, child, depth - 1) for child in _AtspiAdapter(atspi).children(node))


def _give_focus(atspi, ref: str, entry: _Entry) -> None:
    """Make ``entry`` the keyboard's target: its window active and its focus inside it, or raise."""
    focus_window(handle=entry.handle)
    try:
        entry.node.grab_focus()
    except Exception as e:
        log.debug("grab_focus failed (%s)", type(e).__name__)
    deadline = time.monotonic() + _FOCUS_WAIT_S
    while True:
        if _active_handle() == entry.handle and _focused_in(atspi, entry.node):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Element {ref} ({entry.name!r}) did not take the keyboard focus: nothing was typed")
        time.sleep(_FOCUS_POLL_S)


def type_element(ref: str, text: str) -> str:
    """Replace the element's text through EditableText, else give it the focus and type at its caret."""
    with _tree_lock:
        atspi = _load_atspi()
        entry = _live(atspi, ref)
        if "EditableText" in _interfaces(entry.node):
            if not _has_state(atspi, entry.node, atspi.StateType.EDITABLE):
                raise RuntimeError(f"Element {ref} ({entry.name!r}) is read-only")
            try:
                if entry.node.set_text_contents(text):
                    return f"Set the value of {ref} ({entry.name!r}) to {len(text)} chars"
            except Exception as e:
                log.debug("set_text_contents did not answer (%s)", type(e).__name__)
                raise may_have_happened(ref, entry.name) from None
            log.debug("set_text_contents was refused: typing instead")
        _give_focus(atspi, ref, entry)
    return f"{x11_input.type_text(text)} into {ref} ({entry.name!r}), at its caret"


def _window_by_title(title: str) -> int:
    """The handle of the window whose title best matches ``title``; raises when none does."""
    best_score, win_id = 0, 0
    for w in enumerate_windows():
        try:
            from thefuzz import fuzz

            score = fuzz.partial_ratio(title.lower(), w.title.lower())
        except Exception:
            score = 100 if title.lower() in w.title.lower() else 0
        if score > best_score:
            best_score = score
            win_id = w.handle
    if best_score < 50:
        raise LookupError(f"No window matching '{title}' (best score {best_score})")
    return win_id


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Bring a window to the foreground. Fuzzy-match on title if given."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    win_id: Optional[int] = handle or (_window_by_title(title) if title else None)
    if not win_id:
        raise LookupError("No window found")
    try:
        session._run_x(["wmctrl", "-i", "-a", hex(win_id)])
    except RuntimeError as e:
        raise RuntimeError(f"Failed to focus: {e}") from e
    return f"Focused window handle={win_id}"


def minimize_all() -> str:
    """Show the desktop (minimize all windows) via wmctrl."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    session._run_x(["wmctrl", "-k", "on"])
    return "Minimized all windows"


def _spawn_x(cmd: list[str], settle: float = 1.2) -> Optional[int]:
    """Start a command DETACHED and return its exit code, or ``None`` if it is
    still running after ``settle`` seconds.

    Launching an app is fire-and-forget: a GUI process runs indefinitely, so we
    must NOT wait for it to exit (that is what made ``_run_x`` hang on
    ``gnome-text-editor``). We start it in its own session with output discarded,
    wait briefly to catch an immediate failure, and treat "still running" (a live
    GUI) or "exited 0" (a launcher that forked the app) as success.
    """
    s = get_session()
    argv, env = _build_invocation(s, cmd)
    log.debug("x-spawn: %s", argv)
    try:
        p = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127
    try:
        return p.wait(timeout=settle)
    except subprocess.TimeoutExpired:
        return None  # still running → a live GUI app → success


def launch_app(name: str, args: str = "") -> str:
    """Launch an application: gtk-launch (.desktop id), then xdg-open, then exec.

    Each attempt is spawned detached so a long-running GUI process never blocks.
    """
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    arg_list = args.split() if args else []
    desktop_id = name[:-8] if name.endswith(".desktop") else name
    last_rc: Optional[int] = None
    for attempt in (["gtk-launch", desktop_id, *arg_list], ["xdg-open", name], [name, *arg_list]):
        rc = _spawn_x(attempt)
        if rc is None or rc == 0:
            return f"Launched {name} (via {attempt[0]})"
        log.debug("launch attempt %s exited rc=%s", attempt[0], rc)
        last_rc = rc
    raise RuntimeError(f"Failed to launch {name} (last rc={last_rc})")


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize a window by handle (via xdotool)."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    _xdotool(["windowsize", str(handle), str(width), str(height)])
    return f"Resized {handle} to {width}x{height}"
