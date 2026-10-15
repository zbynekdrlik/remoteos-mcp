"""The user-session environment of a Linux Shell child (#52).

The Linux server runs as a system unit with ``User=<user>`` (``install-linux.sh``). Such a unit
gets no login-session environment: ``XDG_RUNTIME_DIR``, ``DBUS_SESSION_BUS_ADDRESS`` and
``DISPLAY`` are all unset. A Shell child inherited exactly that, so its ``systemctl --user``
failed with "$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined" although
``/run/user/<uid>/bus`` existed (live on stream-pp).

:func:`shell_env` returns the child's environment: the server's own, plus the session variables
of the server's OWN user (its effective uid). A variable is added only when it is not set (an
empty value counts as not set), and only after what it names is checked:

- ``XDG_RUNTIME_DIR=/run/user/<euid>``: a directory owned by the euid;
- ``DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<euid>/bus``: that directory, and ``bus`` in it
  is a socket. The user's systemd manager serves it with any login or linger, no desktop needed;
- ``DISPLAY``: the provider's session (``desktop.get_session()``, loginctl plus the ``/proc``
  scan, the one the X tools use) when its uid is the euid, and only a local display whose X
  socket (``/tmp/.X11-unix/X<n>``) exists and is owned by root or the euid. That directory is
  world-writable: after a logout another local user could bind ``X0`` and collect the cookie;
- ``XAUTHORITY``: that session's file, when it exists and the child's DISPLAY is the session's;
- ``WAYLAND_DISPLAY=wayland-0``: a Wayland session of the euid whose ``wayland-0`` socket is in
  the child's runtime dir. The provider does not resolve the name; ``wayland-0`` is libwayland's
  own default when the variable is unset.

Never another user's session. As root (euid 0) nothing is added: root has no user bus of its
own, and handing a root child a user's display or bus is what the X tools' ``sudo -u`` wrap
exists to avoid.

The session is only READ: the provider's cache, detected at start-up. A Shell call never runs a
detection and never writes the cache (review round 1). Re-detecting at a logout wrote
"headless" into it, and after a re-login the desktop tools stayed refused until a restart. The
socket checks above are what keep a logout honest: no X socket, no DISPLAY; the same display
back, DISPLAY back. A new session on another display reaches Shell when the X tools' own retry
re-detects it, or at the next start.

Logs name the added variables, once per change, never their values. A failing session lookup is
one WARNING per exception type.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from typing import Optional

log = logging.getLogger("remoteos.platform.linux.shell_env")

# The OS paths read here (module constants, so the tests point them at a fake tree).
RUNTIME_ROOT = "/run/user"
X11_SOCKET_DIR = "/tmp/.X11-unix"
WAYLAND_SOCKET = "wayland-0"

_LOCAL_DISPLAY = re.compile(r"(?:unix)?:(\d+)(?:\.\d+)?")

# The variable names last logged, and the session-lookup failure types already warned about.
_last_added: Optional[tuple[str, ...]] = None
_warned: set[str] = set()


def shell_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment of a Linux Shell child: ``base`` (the server's own by default) plus the
    session variables of the server's own user that ``base`` lacks. ``base`` is never changed."""
    env = dict(os.environ if base is None else base)
    euid = os.geteuid()
    added: list[str] = []
    if euid != 0:
        runtime = os.path.join(RUNTIME_ROOT, str(euid))
        if _owned_dir(runtime, euid):
            _add(env, added, "XDG_RUNTIME_DIR", runtime)
            bus = os.path.join(runtime, "bus")
            if _is_socket(bus):
                _add(env, added, "DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
        session = _own_session(euid)
        if session is not None:
            _add_display(env, added, session, runtime, euid)
    _log_change(added, euid)
    return env


def _add(env: dict[str, str], added: list[str], name: str, value: str) -> None:
    if not env.get(name):
        env[name] = value
        added.append(name)


def _add_display(env: dict[str, str], added: list[str], session, runtime: str, euid: int) -> None:
    """DISPLAY / XAUTHORITY / WAYLAND_DISPLAY of a session of the euid, each one checked."""
    if _display_alive(session.display, euid):
        _add(env, added, "DISPLAY", session.display)
    if (
        session.display
        and session.xauthority
        and env.get("DISPLAY") == session.display
        and os.path.isfile(session.xauthority)
    ):
        _add(env, added, "XAUTHORITY", session.xauthority)
    if (
        session.mode == "wayland"
        and env.get("XDG_RUNTIME_DIR") == runtime
        and _is_socket(os.path.join(runtime, WAYLAND_SOCKET))
    ):
        _add(env, added, "WAYLAND_DISPLAY", WAYLAND_SOCKET)


def _own_session(euid: int):
    """The provider's cached session when it belongs to ``euid``, else ``None``.

    Only read, never re-detected or written (review round 1). Never raises: a failing lookup
    costs the display variables, never the Shell call, and is warned about once per type.
    """
    try:
        # Linux-only (pwd, the X helpers), and imported only when a Linux Shell runs
        from remoteos.platform.linux import desktop

        session = desktop.get_session()
    except Exception as e:
        kind = type(e).__name__
        if kind not in _warned:
            _warned.add(kind)
            log.warning("Shell environment: the session lookup failed (%s); no display variables", kind)
        return None
    return session if session.uid == euid else None


def _display_alive(display: Optional[str], euid: int) -> bool:
    """A local display (``:N``, ``:N.S``, ``unix:N``) whose X socket exists and is root's (Xorg)
    or the euid's (a rootless Xorg, Xwayland), never another user's."""
    m = _LOCAL_DISPLAY.fullmatch(display or "")
    return bool(m) and _is_socket(os.path.join(X11_SOCKET_DIR, f"X{m.group(1)}"), owners=(0, euid))


def _owned_dir(path: str, uid: int) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == uid


def _is_socket(path: str, owners: Optional[tuple[int, ...]] = None) -> bool:
    """``path`` is a socket, owned by one of ``owners`` when they are given."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and (owners is None or st.st_uid in owners)


def _log_change(added: list[str], euid: int) -> None:
    """One INFO line whenever the added names change: names only, never a value (#31)."""
    global _last_added
    names = tuple(added)
    if names == _last_added:
        return
    _last_added = names
    if euid == 0:
        log.info("Shell environment: running as root, no session variable is added")
    else:
        log.info("Shell environment: the children get %s", ", ".join(names) if names else "no session variable")
