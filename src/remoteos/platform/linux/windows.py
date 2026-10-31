"""Linux windows: the wmctrl list, the AT-SPI element tree, focus, launch, resize."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from remoteos.errors import DesktopUnavailable
from remoteos.platform.linux import session
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
# Interactive elements — AT-SPI2 accessibility tree (issue #7)
# ---------------------------------------------------------------------------
#
# On Windows/macOS the interactive-element list comes from the platform
# accessibility API (UIAutomation / AX). The Linux equivalent is **AT-SPI2**,
# reached in-process via GObject-Introspection (``gi.repository.Atspi``).
#
# Coverage is app-dependent: GTK apps expose a rich tree; many Qt / Electron /
# legacy-X apps expose little or nothing. We therefore DEGRADE GRACEFULLY —
# return whatever AT-SPI exposes (possibly few or zero elements), never crash,
# never fabricate. An empty list is an honest "the focused app exposed nothing".

# Roles worth surfacing as click/interaction targets. Compared case-folded
# against ``Atspi.Accessible.get_role_name()``.
_ACTIONABLE_ROLES = frozenset(
    {
        "push button",
        "toggle button",
        "check box",
        "radio button",
        "menu item",
        "check menu item",
        "radio menu item",
        "menu",
        "menu bar",
        "text",
        "entry",
        "password text",
        "combo box",
        "list item",
        "table cell",
        "link",
        "slider",
        "spin button",
        "page tab",
        "tab",
        "icon",
        "list box",
        "tree item",
        "button",
    }
)


class _AtspiAdapter:
    """Thin adapter over a real ``Atspi.Accessible`` node.

    Isolates every call into the EXTERNAL AT-SPI library behind a tiny duck-typed
    surface (``children`` / ``role_name`` / ``name`` / ``is_showing`` / ``extents``)
    so the pure walk in :func:`_collect_actionable` can be unit-tested with
    synthetic nodes and never touches the library. Every method is defensive:
    one malformed node must not abort the whole walk.

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
            return [node.get_child_at_index(i) for i in range(node.get_child_count())]
        except Exception:  # pragma: no cover - defensive (live lib only)
            return []

    def role_name(self, node) -> str:
        try:
            return (node.get_role_name() or "").strip().lower()
        except Exception:  # pragma: no cover
            return ""

    def name(self, node) -> str:
        try:
            return (node.get_name() or "").strip()
        except Exception:  # pragma: no cover
            return ""

    def is_showing(self, node) -> bool:
        try:
            return bool(node.get_state_set().contains(self._a.StateType.SHOWING))
        except Exception:  # pragma: no cover
            return True  # don't over-filter when state is unreadable

    def extents(self, node):
        try:
            coord_type = self._a.CoordType.WINDOW if self._coord == "window" else self._a.CoordType.SCREEN
            r = node.get_extents(coord_type)
            if r is None:
                return None
            return (
                int(r.x) + self._ox,
                int(r.y) + self._oy,
                int(r.x + r.width) + self._ox,
                int(r.y + r.height) + self._oy,
            )
        except Exception:  # pragma: no cover
            return None


def _collect_actionable(root, adapter, *, max_elements: int = 60, max_nodes: int = 4000) -> list[dict]:
    """Breadth-first walk collecting actionable elements (PURE — no AT-SPI here).

    ``adapter`` supplies ``children`` / ``role_name`` / ``name`` / ``is_showing`` /
    ``extents``; in production it wraps ``Atspi.Accessible``, in tests it wraps a
    synthetic tree. Returns element dicts in the SAME shape as the Windows/macOS
    provider (``index`` / ``class`` / ``text`` / ``rect``) so the shared
    ``AnnotatedSnapshot`` tool layer treats every platform identically.

    Bounded by ``max_elements`` and ``max_nodes`` so a pathological tree can't
    hang the walk. Elements with no positive on-screen area are dropped (this also
    discards the ``(-1,-1)`` application node AT-SPI reports).
    """
    from collections import deque

    out: list[dict] = []
    seen = 0
    queue = deque([root])
    while queue and len(out) < max_elements and seen < max_nodes:
        node = queue.popleft()
        seen += 1
        role = adapter.role_name(node)
        if role in _ACTIONABLE_ROLES and adapter.is_showing(node):
            ext = adapter.extents(node)
            if ext and ext[2] > ext[0] and ext[3] > ext[1] and ext[2] > 0 and ext[3] > 0:
                out.append(
                    {
                        "index": len(out) + 1,
                        "class": role,
                        "text": adapter.name(node),
                        "rect": {"left": ext[0], "top": ext[1], "right": ext[2], "bottom": ext[3]},
                    }
                )
        for child in adapter.children(node):
            if child is not None:
                queue.append(child)
    return out


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


def _atspi_active_frame(atspi):
    """Return the ACTIVE top-level window (frame), or the first SHOWING one.

    Iterates the AT-SPI desktop's applications and their window children. Prefers
    a frame in the ACTIVE state (the focused window); falls back to the first
    SHOWING frame; returns ``None`` when nothing qualifies.
    """
    desktop = atspi.get_desktop(0)
    fallback = None
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            win_count = app.get_child_count()
        except Exception:  # pragma: no cover - defensive
            continue
        for j in range(win_count):
            win = app.get_child_at_index(j)
            if win is None:
                continue
            try:
                states = win.get_state_set()
            except Exception:  # pragma: no cover
                continue
            if states.contains(atspi.StateType.ACTIVE):
                return win
            if fallback is None and states.contains(atspi.StateType.SHOWING):
                fallback = win
    return fallback


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


def _active_window_geometry() -> Optional[tuple[int, int, int, int]]:
    """Return the focused window's ``(x, y, w, h)`` via xdotool, or ``None``."""
    try:
        out = _xdotool(["getactivewindow", "getwindowgeometry", "--shell"])
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


def _atspi_coord_strategy(atspi, frame) -> tuple[str, tuple[int, int]]:
    """Decide how to map AT-SPI extents to screen pixels for ``frame``.

    Returns ``("window", (ox, oy))`` — read WINDOW-relative extents and add the
    content origin ``(ox, oy)`` — whenever the focused-window geometry is
    resolvable (the reliable path; GTK4 reports SCREEN as ``(0,0)``). Falls back
    to ``("screen", (0, 0))`` when the frame already reports a real (non-origin)
    SCREEN position, else best-effort raw WINDOW coords.
    """
    geo = _active_window_geometry()
    try:
        fw = frame.get_extents(atspi.CoordType.WINDOW)
        fs = frame.get_extents(atspi.CoordType.SCREEN)
    except Exception:  # pragma: no cover - defensive
        fw = fs = None
    if geo is not None and fw is not None:
        return "window", _content_origin(geo, int(fw.width), int(fw.height))
    if fs is not None and (int(fs.x) > 0 or int(fs.y) > 0):
        return "screen", (0, 0)  # toolkit reports real SCREEN coords
    return "window", (0, 0)  # last resort: window-relative positions only


def get_interactive_elements(limit: int = 300) -> list[dict]:
    """Interactive UI elements of the focused app via the AT-SPI accessibility tree.

    ``limit`` is what Windows needs to stop its walk early; the callers cut this list themselves.

    Returns an empty list on a non-X11 box (headless contract — never shells out /
    imports gi), when AT-SPI is unavailable, or when the focused app exposes no
    accessible tree (common for Qt/Electron/legacy apps). Never crashes, never
    fabricates — a partial-but-honest list beats a failure.

    Coordinates are screen pixels: WINDOW-relative AT-SPI extents plus the focused
    window's content origin (GTK4 reports SCREEN extents as ``(0,0)``, so the
    window offset is required — see :func:`_atspi_coord_strategy`).
    """
    if get_session().mode != "x11":
        return []
    try:
        atspi = _load_atspi()
    except Exception as e:
        log.warning("AT-SPI unavailable (%s) — no interactive elements", e)
        return []
    try:
        frame = _atspi_active_frame(atspi)
        if frame is None:
            log.info("AT-SPI: no active/showing frame found")
            return []
        coord, offset = _atspi_coord_strategy(atspi, frame)
        adapter = _AtspiAdapter(atspi, coord=coord, offset=offset)
        elements = _collect_actionable(frame, adapter)
        log.info(
            "AT-SPI: collected %d interactive elements (coord=%s, offset=%s)",
            len(elements),
            coord,
            offset,
        )
        return elements
    except Exception as e:
        log.warning("AT-SPI walk failed (%s) — returning no elements", e)
        return []


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    """Bring a window to the foreground. Fuzzy-match on title if given."""
    if get_session().mode != "x11":
        raise DesktopUnavailable(_unavailable())
    win_id: Optional[int] = handle
    if not win_id and title:
        best_score = 0
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
