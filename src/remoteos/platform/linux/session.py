"""The Linux graphical session: detection, its cache, and running an X command inside it.

``get_session`` caches what ``_detect`` finds; ``_run_x`` runs a command in that session (env
injection, or ``sudo -u`` when the server is root) and re-detects once on a failure. Screen locking
and notifications act on the session and need no window, so they are here too.
"""

from __future__ import annotations

import logging
import os
import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from remoteos.errors import DesktopUnavailable
from remoteos.platform.linux.shell_env import _snapshot

log = logging.getLogger("remoteos.platform.linux.desktop")  # one logger for the whole provider

# Stub message kept byte-for-byte identical to the original headless provider so
# the headless regression contract (cam1..4) never changes.
_MSG = "Not available on headless Linux (no display)"

# The stated position on Wayland (#38): nothing here works there, and this says why and what to do.
_WAYLAND_MSG = (
    "Not available: this is a Wayland session, and the desktop tools drive X11 (xdotool, scrot, wmctrl, xclip); "
    "Wayland support is a separate tier that does not exist yet. Log in to an X11 session instead (\"Ubuntu on "
    "Xorg\" on the login screen, or WaylandEnable=false in /etc/gdm3/custom.conf), then retry."
)

# Default per-command timeout for the little X helper subprocesses.
_TIMEOUT = 15


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
        try:
            if Path(c).exists():
                return c
        except OSError as exc:
            # Path.exists() does NOT ignore EACCES (PermissionError) — it raises
            # when the parent directory is unreadable (e.g. /home/packer on a CI
            # runner where uid 1000 = packer but the process runs as another user).
            # A real fleet box can hit this too (session-user service probing a
            # root-owned home dir). Treat as "candidate not found" and continue.
            log.debug("xauthority candidate %s inaccessible: %s", c, exc)
            continue
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
    #    Trust our OWN env only when NOT running as root: a root service's own env
    #    is never the real seat (and a leaked/inherited DISPLAY would otherwise make
    #    it classify itself as a uid-0 session, dropping the sudo -u <user> wrap).
    env_mode = _mode_from_env(os.environ)
    if env_mode in ("x11", "wayland") and os.geteuid() != 0:
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
        return _WAYLAND_MSG
    return _MSG


# ---------------------------------------------------------------------------
# X command execution (env injection or sudo -u depending on who we run as)
# ---------------------------------------------------------------------------


def _session_env(s: SessionInfo) -> dict[str, str]:
    env = _snapshot(os.environ)
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
        return prefix + cmd, _snapshot(os.environ)
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


def _loggable(argv: list[str]) -> list[str]:
    """``argv`` for a log line: the text of ``xdotool type … -- <text>`` becomes ``<N chars>``, a held key ``<key>``.

    The typed text can be a password; it is never logged (typing.md), and ``--debug``
    makes these DEBUG lines visible (#31).
    """
    if "xdotool" in argv:
        x = argv.index("xdotool")
        if argv[x + 1 : x + 2] == ["type"] and "--" in argv[x + 2 :]:
            cut = argv.index("--", x + 2) + 1  # the text follows the FIRST "--" after "type"
            return [*argv[:cut], *(f"<{len(a)} chars>" for a in argv[cut:])]
        if argv[x + 1 : x + 2] in (["keydown"], ["keyup"]):  # HoldKey: a key per call can spell a text
            return [*argv[: x + 2], "<key>"]
    return argv


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
    log.debug("x-run: %s", _loggable(argv))
    try:
        r = _subprocess_run(argv, env, input_bytes, timeout, discard_output)
    except FileNotFoundError as e:
        raise RuntimeError(f"{cmd[0]} not installed: {e}") from e
    except subprocess.TimeoutExpired as e:
        # A hang raises TimeoutExpired, which is NOT a RuntimeError — convert it so
        # it honors the "raises RuntimeError" contract and hits callers' handlers
        # instead of escaping to the outer `except Exception` (losing the message).
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s") from e
    if r.returncode != 0:
        # One retry with a freshly-detected session.
        s2 = get_session(force=True)
        if s2.mode == "x11":
            argv2, env2 = _build_invocation(s2, cmd)
            log.debug("x-run retry: %s", _loggable(argv2))
            try:
                r = _subprocess_run(argv2, env2, input_bytes, timeout, discard_output)
            except FileNotFoundError as e:
                raise RuntimeError(f"{cmd[0]} not installed: {e}") from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"{cmd[0]} timed out after {timeout}s") from e
        if r.returncode != 0:
            stderr = (r.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"{cmd[0]} failed (rc={r.returncode}): {stderr or '(no stderr)'}")
    return r


def _xdotool(subargs: list[str]) -> str:
    r = _run_x(["xdotool", *subargs])
    return (r.stdout or b"").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Lock screen / Notification
# ---------------------------------------------------------------------------


def lock_screen() -> str:
    """Lock the active graphical session via loginctl (needs no display)."""
    s = get_session()
    if s.mode not in ("x11", "wayland"):
        raise DesktopUnavailable(_unavailable())
    target = ["loginctl", "lock-session"]
    if s.session_id:
        target.append(s.session_id)
    rc, _out, err = _run_capture(target)
    if rc != 0:
        raise RuntimeError(f"loginctl lock-session failed: {err.strip() or f'rc={rc}'}")
    return "Screen locked"


def show_notification(title: str, message: str) -> str:
    """Show a desktop notification via notify-send (addresses the user's D-Bus)."""
    if get_session().mode not in ("x11", "wayland"):
        raise DesktopUnavailable(_unavailable())
    _run_x(["notify-send", title, message], timeout=10)
    return "Notification shown"
