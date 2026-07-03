"""Linux desktop provider.

Runtime session detection decides behavior:

* **X11 session** (a graphical seat is active)  → real implementations backed by
  ``xdotool`` / ``scrot`` / ``wmctrl`` / ``xclip`` / ``notify-send`` / ``loginctl``.
* **Wayland session**                           → not yet implemented (separate tier);
  every function returns an honest "unavailable" message and never crashes.
* **Headless** (no graphical session, e.g. cam1..4) → the original stub behavior,
  byte-for-byte unchanged, so headless boxes see zero regression.

The MCP service may run either as *root* (borrowing the logged-in user's X session)
or as the *session user itself* (the default systemd unit ``User=`` on our targets).
Both are handled: when running as root we re-run each X command via ``sudo -u <user>``
with the session's ``DISPLAY`` / ``XAUTHORITY`` / D-Bus address; when already running
as the session user we simply inject those variables into the environment.

``loginctl show-session`` frequently reports an **empty** ``Display=`` for X11
sessions, so the reliable source of ``DISPLAY`` + ``XAUTHORITY`` is a scan of
``/proc/<pid>/environ`` for a process owned by the session user (e.g. the shell /
window manager). That is what :func:`_scan_proc_for_x_env` does.
"""

from __future__ import annotations

import logging
import os
import pwd
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("remoteos.platform.linux.desktop")

HAS_WIN32 = False  # compatibility flag — always False on Linux

# Stub message kept byte-for-byte identical to the original headless provider so
# the headless regression contract (cam1..4) never changes.
_MSG = "Not available on headless Linux (no display)"

# Default per-command timeout for the little X helper subprocesses.
_TIMEOUT = 15


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
# Session detection
# ---------------------------------------------------------------------------


@dataclass
class SessionInfo:
    """Resolved graphical-session context.

    ``mode`` is one of ``"x11"``, ``"wayland"``, ``"headless"``.
    """

    mode: str
    display: Optional[str] = None
    xauthority: Optional[str] = None
    uid: Optional[int] = None
    user: Optional[str] = None
    session_id: Optional[str] = None
    runtime_dir: Optional[str] = None
    dbus_address: Optional[str] = None


def _mode_from_env(env: dict) -> Optional[str]:
    """Classify the graphical mode from an environment mapping (pure function).

    Returns ``"x11"`` / ``"wayland"`` when the environment already advertises a
    display, or ``None`` when it does not (caller must then consult loginctl).
    A Wayland session is reported as ``"wayland"`` even when ``DISPLAY`` is also
    set (XWayland), because the X11 backend is not the right tool there.
    """
    session_type = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session_type == "wayland" or (env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY")):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    if session_type == "x11":
        return "x11"
    return None


def _parse_show_session(text: str) -> dict:
    """Parse ``loginctl show-session`` ``key=value`` output into a dict (pure)."""
    props: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    return props


def _choose_session(sessions: list[dict]) -> Optional[dict]:
    """Pick the active graphical *user* session from a list of prop dicts (pure).

    Prefers an X11 user session; skips tty sessions and display-manager greeters
    and anything not currently active. Returns ``None`` when none qualify.
    """

    def _active(p: dict) -> bool:
        return (p.get("Active", "").lower() == "yes") or (p.get("State", "").lower() == "active")

    def _candidate(p: dict, want_type: str) -> bool:
        return p.get("Type", "").lower() == want_type and p.get("Class", "").lower() == "user" and _active(p)

    for want in ("x11", "wayland"):
        for p in sessions:
            if _candidate(p, want):
                return p
    return None


def _scan_proc_for_x_env(uid: int) -> tuple[Optional[str], Optional[str]]:
    """Return ``(DISPLAY, XAUTHORITY)`` read from a process owned by ``uid``.

    ``loginctl`` often reports an empty ``Display`` for X11 sessions, so the
    authoritative source is the live environment of one of the session's own
    processes (window manager / shell). Requires permission to read
    ``/proc/<pid>/environ`` — satisfied when we run as ``uid`` itself or as root.
    """
    fallback_display: Optional[str] = None
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None, None
    for pid in pids:
        base = f"/proc/{pid}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(f"{base}/environ", "rb") as fh:
                raw = fh.read()
        except (OSError, PermissionError):
            continue
        penv: dict[str, str] = {}
        for part in raw.split(b"\0"):
            if b"=" in part:
                k, _, v = part.partition(b"=")
                penv[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        disp = penv.get("DISPLAY")
        xauth = penv.get("XAUTHORITY")
        if disp and xauth:
            return disp, xauth
        if disp and fallback_display is None:
            fallback_display = disp
    return fallback_display, None


def _guess_xauthority(uid: int, user: Optional[str]) -> Optional[str]:
    """Best-effort XAUTHORITY guesses when the /proc scan didn't find one."""
    candidates = [f"/run/user/{uid}/gdm/Xauthority", f"/run/user/{uid}/.Xauthority"]
    if user:
        try:
            home = pwd.getpwnam(user).pw_dir
            candidates.append(f"{home}/.Xauthority")
        except KeyError:
            pass
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _run_capture(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a helper command, returning ``(rc, stdout, stderr)``; never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"
    except Exception as e:  # pragma: no cover - defensive
        return 1, "", f"{cmd[0]}: {e}"


def _detect() -> SessionInfo:
    """Detect the current graphical session (does real subprocess/proc work)."""
    # 1) Environment already carries a display (interactive run inside a session).
    env_mode = _mode_from_env(os.environ)
    if env_mode in ("x11", "wayland"):
        uid = os.getuid()
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = None
        info = SessionInfo(
            mode=env_mode,
            display=os.environ.get("DISPLAY"),
            xauthority=os.environ.get("XAUTHORITY") or _guess_xauthority(uid, user),
            uid=uid,
            user=user,
            runtime_dir=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"),
            dbus_address=os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"),
        )
        log.debug("session detected from env: %s", info)
        return info

    # 2) systemd-service context: resolve the active graphical session via loginctl.
    rc, out, err = _run_capture(["loginctl", "list-sessions", "--no-legend"])
    if rc != 0:
        log.info("loginctl unavailable (rc=%s: %s) — treating as headless", rc, err.strip())
        return SessionInfo(mode="headless")

    session_ids = [line.split()[0] for line in out.splitlines() if line.split()]
    props_list: list[dict] = []
    for sid in session_ids:
        srrc, srout, _ = _run_capture(
            [
                "loginctl",
                "show-session",
                sid,
                "-p",
                "Id",
                "-p",
                "User",
                "-p",
                "Name",
                "-p",
                "Type",
                "-p",
                "Class",
                "-p",
                "Active",
                "-p",
                "State",
                "-p",
                "Display",
            ]
        )
        if srrc == 0:
            props_list.append(_parse_show_session(srout))

    chosen = _choose_session(props_list)
    if chosen is None:
        log.debug("no active graphical session found among %d sessions — headless", len(props_list))
        return SessionInfo(mode="headless")

    mode = chosen.get("Type", "").lower()  # x11 | wayland
    try:
        uid = int(chosen.get("User", ""))
    except ValueError:
        uid = os.getuid()
    user = chosen.get("Name") or None
    display, xauthority = _scan_proc_for_x_env(uid)
    if not display:
        display = chosen.get("Display") or ":0"
    if not xauthority:
        xauthority = _guess_xauthority(uid, user)

    info = SessionInfo(
        mode=mode if mode in ("x11", "wayland") else "headless",
        display=display,
        xauthority=xauthority,
        uid=uid,
        user=user,
        session_id=chosen.get("Id"),
        runtime_dir=f"/run/user/{uid}",
        dbus_address=f"unix:path=/run/user/{uid}/bus",
    )
    log.debug("session detected via loginctl: %s", info)
    return info


_SESSION: Optional[SessionInfo] = None


def get_session(force: bool = False) -> SessionInfo:
    """Return the cached :class:`SessionInfo`, detecting it on first use.

    ``force=True`` re-detects (used after an X command fails, in case the session
    changed — e.g. the user logged out/in).
    """
    global _SESSION
    if _SESSION is None or force:
        _SESSION = _detect()
    return _SESSION


def _unavailable() -> str:
    """The honest 'not available' message for the current non-X11 mode."""
    s = get_session()
    if s.mode == "wayland":
        return "Not available: Wayland session detected (X11 backend only — Wayland support is a separate tier)"
    return _MSG


# ---------------------------------------------------------------------------
# X command execution (env injection or sudo -u depending on who we run as)
# ---------------------------------------------------------------------------


def _session_env(s: SessionInfo) -> dict[str, str]:
    env = dict(os.environ)
    if s.display:
        env["DISPLAY"] = s.display
    if s.xauthority:
        env["XAUTHORITY"] = s.xauthority
    if s.uid is not None:
        env["XDG_RUNTIME_DIR"] = s.runtime_dir or f"/run/user/{s.uid}"
        env["DBUS_SESSION_BUS_ADDRESS"] = s.dbus_address or f"unix:path=/run/user/{s.uid}/bus"
    return env


def _build_invocation(s: SessionInfo, cmd: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return ``(argv, env)`` to run ``cmd`` inside the session.

    * Running as the session user (or any non-root)  → run directly with the
      session variables injected into the environment.
    * Running as root with a known non-root session user → wrap with
      ``sudo -n -u <user> env VAR=… <cmd>`` so the command executes with the
      correct credentials and X authority.
    """
    if os.geteuid() == 0 and s.user and s.uid not in (None, 0):
        prefix = [
            "sudo",
            "-n",
            "-u",
            s.user,
            "env",
            f"DISPLAY={s.display or ''}",
            f"XAUTHORITY={s.xauthority or ''}",
            f"XDG_RUNTIME_DIR={s.runtime_dir or f'/run/user/{s.uid}'}",
            f"DBUS_SESSION_BUS_ADDRESS={s.dbus_address or f'unix:path=/run/user/{s.uid}/bus'}",
        ]
        return prefix + cmd, dict(os.environ)
    return cmd, _session_env(s)


def _subprocess_run(argv, env, input_bytes, timeout, discard_output):
    """Thin wrapper choosing capture vs DEVNULL output.

    ``discard_output=True`` sends stdout/stderr to ``/dev/null`` instead of a
    pipe. This is REQUIRED for a command that forks and stays resident holding
    the pipe open — notably ``xclip`` when SETTING a selection, which forks a
    background process to serve the clipboard; a captured pipe would keep
    ``subprocess.run`` blocked until timeout even though the command succeeded.
    """
    if discard_output:
        return subprocess.run(
            argv,
            input=input_bytes,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
        )
    return subprocess.run(argv, input=input_bytes, capture_output=True, env=env, timeout=timeout)


def _run_x(
    cmd: list[str],
    *,
    input_bytes: Optional[bytes] = None,
    timeout: int = _TIMEOUT,
    discard_output: bool = False,
):
    """Run an X-session command; returns ``subprocess.CompletedProcess``.

    Raises :class:`RuntimeError` on non-zero exit so callers can surface a clear
    message. Retries once with a fresh session detection if the first attempt
    fails (handles a stale cached session after a logout/login).
    """
    s = get_session()
    argv, env = _build_invocation(s, cmd)
    log.debug("x-run: %s", argv)
    try:
        r = _subprocess_run(argv, env, input_bytes, timeout, discard_output)
    except FileNotFoundError as e:
        raise RuntimeError(f"{cmd[0]} not installed: {e}") from e
    if r.returncode != 0:
        # One retry with a freshly-detected session.
        s2 = get_session(force=True)
        if s2.mode == "x11":
            argv2, env2 = _build_invocation(s2, cmd)
            log.debug("x-run retry: %s", argv2)
            try:
                r = _subprocess_run(argv2, env2, input_bytes, timeout, discard_output)
            except FileNotFoundError as e:
                raise RuntimeError(f"{cmd[0]} not installed: {e}") from e
        if r.returncode != 0:
            stderr = (r.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"{cmd[0]} failed (rc={r.returncode}): {stderr or '(no stderr)'}")
    return r


def _xdotool(subargs: list[str]) -> str:
    r = _run_x(["xdotool", *subargs])
    return (r.stdout or b"").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def capture_png(bbox: Optional[tuple[int, int, int, int]] = None) -> bytes:
    """Capture the screen (or a region) as PNG bytes via scrot (fallback: import).

    ``bbox`` is ``(left, top, right, bottom)`` in screen coordinates, or ``None``
    for the full screen. Only valid on an X11 session — callers must gate on mode.
    """
    tmp = Path("/tmp") / f"remoteos-shot-{uuid.uuid4().hex}.png"
    scrot_cmd = ["scrot", "--overwrite"]
    if bbox is not None:
        left, top, right, bottom = bbox
        w = max(1, right - left)
        h = max(1, bottom - top)
        scrot_cmd += ["-a", f"{left},{top},{w},{h}"]
    scrot_cmd.append(str(tmp))
    try:
        try:
            _run_x(scrot_cmd)
        except RuntimeError as scrot_err:
            # Fallback: ImageMagick import to stdout.
            log.warning("scrot failed (%s) — falling back to ImageMagick import", scrot_err)
            import_cmd = ["import", "-window", "root", "-silent"]
            if bbox is not None:
                left, top, right, bottom = bbox
                import_cmd += ["-crop", f"{right - left}x{bottom - top}+{left}+{top}"]
            import_cmd.append("png:-")
            r = _run_x(import_cmd)
            return r.stdout or b""
        data = tmp.read_bytes()
        if not data:
            raise RuntimeError("screenshot produced an empty file")
        return data
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def take_screenshot(quality: int = 65, max_width: int = 1280, monitor: int = 0) -> str:
    """Capture screen, return base64 JPEG. Resizes if wider than ``max_width``.

    Mirrors the Windows/macOS providers. Raises :class:`RuntimeError` (which the
    ``Snapshot`` tool catches) when there is no usable X11 session — preserving
    the headless contract exactly.
    """
    s = get_session()
    if s.mode != "x11":
        raise RuntimeError(_unavailable())

    import base64
    import io

    from PIL import Image

    png = capture_png()
    img = Image.open(io.BytesIO(png))
    if max_width > 0 and img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), resample=Image.LANCZOS)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Screen recording — ffmpeg x11grab (issue #8)
# ---------------------------------------------------------------------------


def _ffmpeg_x11grab_argv(
    display: str,
    out_path: str,
    *,
    duration: float,
    fps: int,
    region: Optional[tuple[int, int, int, int]],
    max_width: int,
) -> list[str]:
    """Build the ``ffmpeg -f x11grab`` argv for a bounded recording (PURE).

    ``region`` is ``(left, top, right, bottom)`` or ``None`` for the whole display
    (x11grab auto-detects the root-window size when no ``-video_size`` is given).
    Output is an animated GIF so the shared ``ScreenRecord`` tool returns it the
    SAME way as Windows/macOS (``ImageContent`` ``image/gif``). Split out from
    :func:`record_screen` so the argv can be unit-tested without a display.
    """
    fps = min(max(int(fps), 1), 10)
    duration = min(max(float(duration), 0.5), 10.0)
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "x11grab",
        "-framerate",
        str(fps),
    ]
    input_spec = display or ":0"
    if region is not None:
        left, top, right, bottom = region
        width = max(2, right - left)
        height = max(2, bottom - top)
        argv += ["-video_size", f"{width}x{height}"]
        input_spec = f"{input_spec}+{left},{top}"
    argv += ["-i", input_spec, "-t", f"{duration:g}"]
    vf = [f"fps={fps}"]
    if max_width and int(max_width) > 0:
        # ``-2`` keeps the aspect ratio while rounding to an even dimension.
        vf.append(f"scale={int(max_width)}:-2:flags=lanczos")
    argv += ["-vf", ",".join(vf), out_path]
    return argv


def record_screen(
    duration: float = 3.0,
    fps: int = 5,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
    max_width: int = 800,
) -> str:
    """Record the X11 screen via ffmpeg x11grab; return a base64 animated GIF.

    Mirrors :func:`remoteos.recording.record_screen`'s signature + return shape
    (a base64 string) so the shared ``ScreenRecord`` tool treats Linux exactly
    like Windows/macOS. Runs inside the session via ``_build_invocation`` (env
    injection as the session user, ``sudo -u`` wrap when running as root), the
    same as every other X command.

    Raises :class:`RuntimeError` with the headless stub message on a non-X11 box
    — the same contract as :func:`take_screenshot`, so the headless cam1..4 boxes
    never shell out and never crash (the tool is display-gated off there anyway).
    """
    s = get_session()
    if s.mode != "x11":
        raise RuntimeError(_unavailable())

    region: Optional[tuple[int, int, int, int]] = None
    if None not in (left, top, right, bottom):
        li, ti, ri, bi = int(left), int(top), int(right), int(bottom)  # type: ignore[arg-type]
        if ri > li and bi > ti:
            region = (li, ti, ri, bi)

    dur = min(max(float(duration), 0.5), 10.0)
    tmp = Path("/tmp") / f"remoteos-rec-{uuid.uuid4().hex}.gif"
    argv = _ffmpeg_x11grab_argv(s.display or ":0", str(tmp), duration=dur, fps=fps, region=region, max_width=max_width)
    log.info("record_screen: %.1fs @ %sfps region=%s -> %s", dur, fps, region, tmp)
    try:
        # ffmpeg records for `dur` seconds then encodes — allow generous headroom.
        _run_x(argv, timeout=int(dur) + 30)
        data = tmp.read_bytes()
        if not data:
            raise RuntimeError("ffmpeg x11grab produced an empty recording")
        import base64

        log.info("record_screen: produced %d bytes of GIF", len(data))
        return base64.b64encode(data).decode()
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            pass


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


def enumerate_windows() -> list[WindowInfo]:
    """List open top-level windows (via wmctrl). Empty list when not on X11."""
    if get_session().mode != "x11":
        return []
    try:
        r = _run_x(["wmctrl", "-lpG"])
        return _parse_wmctrl_windows((r.stdout or b"").decode("utf-8", "replace"))
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

    In-process AT-SPI reads ``DBUS_SESSION_BUS_ADDRESS`` / ``DISPLAY`` from THIS
    process's environment to find the user's accessibility bus, so we inject the
    resolved-session values first (a systemd service usually starts with none).
    Raises on any failure; callers degrade to an empty element list.
    """
    s = get_session()
    if s.dbus_address:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = s.dbus_address
    if s.display:
        os.environ["DISPLAY"] = s.display
    if s.xauthority:
        os.environ["XAUTHORITY"] = s.xauthority
    if s.runtime_dir:
        os.environ["XDG_RUNTIME_DIR"] = s.runtime_dir
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    return Atspi


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


def get_interactive_elements() -> list[dict]:
    """Interactive UI elements of the focused app via the AT-SPI accessibility tree.

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
        return _unavailable()
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
            return f"No window matching '{title}' (best score {best_score})"
    if not win_id:
        return "No window found"
    try:
        _run_x(["wmctrl", "-i", "-a", hex(win_id)])
        return f"Focused window handle={win_id}"
    except RuntimeError as e:
        return f"Failed to focus: {e}"


def minimize_all() -> str:
    """Show the desktop (minimize all windows) via wmctrl."""
    if get_session().mode != "x11":
        return _unavailable()
    try:
        _run_x(["wmctrl", "-k", "on"])
        return "Minimized all windows"
    except RuntimeError as e:
        return f"Failed: {e}"


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
        return _unavailable()
    arg_list = args.split() if args else []
    desktop_id = name[:-8] if name.endswith(".desktop") else name
    last_rc: Optional[int] = None
    for attempt in (["gtk-launch", desktop_id, *arg_list], ["xdg-open", name], [name, *arg_list]):
        rc = _spawn_x(attempt)
        if rc is None or rc == 0:
            return f"Launched {name} (via {attempt[0]})"
        log.debug("launch attempt %s exited rc=%s", attempt[0], rc)
        last_rc = rc
    return f"Failed to launch {name} (last rc={last_rc})"


def resize_window(handle: int, width: int, height: int) -> str:
    """Resize a window by handle (via xdotool)."""
    if get_session().mode != "x11":
        return _unavailable()
    try:
        _xdotool(["windowsize", str(handle), str(width), str(height)])
        return f"Resized {handle} to {width}x{height}"
    except RuntimeError as e:
        return f"Failed: {e}"


# ---------------------------------------------------------------------------
# Input (mouse / keyboard) — xdotool
# ---------------------------------------------------------------------------

_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def click(x: int, y: int, button: str = "left", action: str = "click") -> str:
    """Mouse click / double-click / hover at screen coordinates."""
    if get_session().mode != "x11":
        return _unavailable()
    btn = _BUTTONS.get(button, 1)
    try:
        if action == "hover":
            _xdotool(["mousemove", str(x), str(y)])
            return f"Hovered at ({x},{y})"
        if action == "double":
            _xdotool(["mousemove", str(x), str(y), "click", "--repeat", "2", "--delay", "120", str(btn)])
            return f"Double-clicked {button} at ({x},{y})"
        _xdotool(["mousemove", str(x), str(y), "click", str(btn)])
        return f"Clicked {button} at ({x},{y})"
    except RuntimeError as e:
        return f"Click error: {e}"


def type_text(text: str, x: int = 0, y: int = 0, clear: bool = False, press_enter: bool = False) -> str:
    """Type text, optionally clicking at (x,y) first / clearing / pressing Enter."""
    if get_session().mode != "x11":
        return _unavailable()
    try:
        if x and y:
            _xdotool(["mousemove", str(x), str(y), "click", "1"])
        if clear:
            _xdotool(["key", "--clearmodifiers", "ctrl+a"])
            _xdotool(["key", "Delete"])
        _xdotool(["type", "--delay", "20", "--", text])
        if press_enter:
            _xdotool(["key", "Return"])
        return f"Typed {len(text)} chars"
    except RuntimeError as e:
        return f"Type error: {e}"


def scroll(amount: int, x: int = 0, y: int = 0, horizontal: bool = False) -> str:
    """Scroll at a position. Positive = up/right, negative = down/left."""
    if get_session().mode != "x11":
        return _unavailable()
    try:
        if x and y:
            _xdotool(["mousemove", str(x), str(y)])
        steps = min(abs(int(amount)), 100) or 1
        if horizontal:
            btn = 7 if amount > 0 else 6  # 6=left, 7=right
        else:
            btn = 4 if amount > 0 else 5  # 4=up, 5=down
        _xdotool(["click", "--repeat", str(steps), str(btn)])
        direction = "horizontally" if horizontal else "vertically"
        return f"Scrolled {amount} {direction}"
    except RuntimeError as e:
        return f"Scroll error: {e}"


def move(x: int, y: int, drag: bool = False, start_x: int = 0, start_y: int = 0, duration: float = 0.3) -> str:
    """Move the mouse, or drag from a start position to the target."""
    if get_session().mode != "x11":
        return _unavailable()
    try:
        if drag:
            if start_x and start_y:
                _xdotool(["mousemove", str(start_x), str(start_y)])
            _xdotool(["mousedown", "1"])
            _xdotool(["mousemove", str(x), str(y)])
            _xdotool(["mouseup", "1"])
            return f"Dragged to ({x},{y})"
        _xdotool(["mousemove", str(x), str(y)])
        return f"Moved to ({x},{y})"
    except RuntimeError as e:
        return f"Move error: {e}"


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
        return _unavailable()
    combo = "+".join(_map_key(k) for k in keys.split("+") if k.strip())
    if not combo:
        return "No keys given"
    try:
        _xdotool(["key", combo])
        return f"Executed shortcut: {keys}"
    except RuntimeError as e:
        return f"Shortcut error: {e}"


# ---------------------------------------------------------------------------
# Clipboard — xclip
# ---------------------------------------------------------------------------


def get_clipboard() -> str:
    if get_session().mode != "x11":
        return _unavailable()
    try:
        r = _run_x(["xclip", "-selection", "clipboard", "-o"])
        return (r.stdout or b"").decode("utf-8", "replace")
    except RuntimeError as e:
        return f"Error: {e}"


def set_clipboard(text: str) -> str:
    if get_session().mode != "x11":
        return _unavailable()
    try:
        # xclip forks to stay resident serving the selection — discard_output so
        # the inherited pipe doesn't keep subprocess.run blocked until timeout.
        _run_x(
            ["xclip", "-selection", "clipboard"],
            input_bytes=text.encode("utf-8"),
            discard_output=True,
        )
        return "Clipboard set"
    except RuntimeError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Lock screen / Notification
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    """Lock the active graphical session via loginctl (needs no display)."""
    s = get_session()
    if s.mode not in ("x11", "wayland"):
        return _unavailable()
    target = ["loginctl", "lock-session"]
    if s.session_id:
        target.append(s.session_id)
    rc, _out, err = _run_capture(target)
    if rc == 0:
        return "Screen locked"
    return f"Failed: {err.strip() or f'loginctl rc={rc}'}"


def show_notification(title: str, message: str) -> str:
    """Show a desktop notification via notify-send (addresses the user's D-Bus)."""
    if get_session().mode not in ("x11", "wayland"):
        return _unavailable()
    try:
        _run_x(["notify-send", title, message], timeout=10)
        return "Notification shown"
    except RuntimeError as e:
        return f"Failed: {e}"
