"""A Linux Shell child gets the server user's own session environment (#52).

The Linux server runs as a system unit with ``User=``, which has no login-session environment, so
a Shell child had no ``XDG_RUNTIME_DIR`` / ``DBUS_SESSION_BUS_ADDRESS`` / ``DISPLAY`` and
``systemctl --user`` failed (live on stream-pp). ``platform/linux/shell_env.shell_env`` adds
them, for the server's OWN user only.

Only the OS boundary is faked:

- the runtime-dir root (``/run/user``) and the X socket directory (``/tmp/.X11-unix``) point into
  a short temp dir holding REAL AF_UNIX sockets (an AF_UNIX path must fit in 108 bytes, which
  pytest's ``tmp_path`` may not);
- the effective uid (``os.geteuid``) where a test needs another one, and the owner ``os.stat``
  reports for a directory only root could really own;
- ``loginctl``, at ``subprocess.run``: it lists no session (a re-detection that reaches it finds
  none; a test that needs a session found again gets it from the provider's env branch);
- the provider's cached session (``desktop._SESSION``), as the provider's own tests do.

The Shell tests run a real ``/bin/bash``.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile

import pytest
from fake_console_child import assert_runs_powershell, returned

import remoteos.__main__ as server
from remoteos.platform import command
from remoteos.platform.linux import desktop

SESSION_VARS = (
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "XDG_SESSION_TYPE",
)
ADDED_VARS = SESSION_VARS[:5]
LOGGER = "remoteos.platform.linux.shell_env"


class FakeLoginctl:
    """``loginctl`` at the stdlib boundary (``subprocess.run``): a box with no login session.

    ``listed`` counts the detections that reached it. ``_run_capture`` swallows any exception, so
    an unexpected command is recorded, not raised, and the fixture fails the test on it.
    """

    def __init__(self) -> None:
        self.listed = 0
        self.unexpected: list[list[str]] = []

    def run(self, cmd, **kwargs):
        if cmd[:2] == ["loginctl", "list-sessions"]:
            self.listed += 1
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.unexpected.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")


class FakeTree:
    """A fake ``/run/user`` and ``/tmp/.X11-unix`` under one short temp dir, with REAL sockets."""

    def __init__(self, root: str, monkeypatch) -> None:
        self.root = root
        self.run_user = os.path.join(root, "run", "user")
        self.x11 = os.path.join(root, "x11")
        os.makedirs(self.run_user)
        os.makedirs(self.x11)
        self._sockets: list[socket.socket] = []
        self._owners: dict[str, int] = {}
        self._monkeypatch = monkeypatch

    def socket(self, path: str) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        self._sockets.append(s)
        return path

    def runtime_dir(self, uid: int, bus: bool = True) -> str:
        path = os.path.join(self.run_user, str(uid))
        os.mkdir(path, 0o700)
        if bus:
            self.socket(os.path.join(path, "bus"))
        return path

    def x_display(self, number: int) -> str:
        return self.socket(os.path.join(self.x11, f"X{number}"))

    def file(self, name: str) -> str:
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write("cookie")
        return path

    def report_owner(self, path: str, uid: int) -> None:
        """``os.stat(path)`` reports ``uid`` as the owner (only root can really chown)."""
        if not self._owners:
            real_stat = os.stat

            def fake_stat(p, *args, **kwargs):
                st = real_stat(p, *args, **kwargs)
                if isinstance(p, str) and p in self._owners:
                    fields = list(st[:10])
                    fields[4] = self._owners[p]  # st_uid
                    return os.stat_result(fields)
                return st

            self._monkeypatch.setattr(os, "stat", fake_stat)
        self._owners[path] = uid

    def close(self) -> None:
        for s in self._sockets:
            s.close()
        shutil.rmtree(self.root)


@pytest.fixture
def se():
    """The module under test."""
    return importlib.import_module("remoteos.platform.linux.shell_env")


@pytest.fixture
def loginctl(monkeypatch):
    fake = FakeLoginctl()
    monkeypatch.setattr(desktop.subprocess, "run", fake.run)
    yield fake
    assert fake.unexpected == []


@pytest.fixture
def tree(monkeypatch, se, loginctl):
    """The server's environment has no session variable, as a system unit's has none."""
    assert os.geteuid() != 0, "these tests need a non-root user"
    for name in SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    fake = FakeTree(tempfile.mkdtemp(prefix="r52-", dir="/tmp"), monkeypatch)
    monkeypatch.setattr(se, "RUNTIME_ROOT", fake.run_user)
    monkeypatch.setattr(se, "X11_SOCKET_DIR", fake.x11)
    monkeypatch.setattr(se, "_redetected_for", None)
    monkeypatch.setattr(se, "_last_added", None)
    monkeypatch.setattr(desktop, "_SESSION", desktop.SessionInfo(mode="headless"))
    yield fake
    fake.close()


def _x11(uid: int, display: str = ":0", xauthority: str | None = None) -> desktop.SessionInfo:
    return desktop.SessionInfo(mode="x11", display=display, xauthority=xauthority, uid=uid, user="someone")


def _base() -> dict[str, str]:
    return {"PATH": os.environ["PATH"]}


def _added(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in ADDED_VARS}


# --------------------------------------------------------------------------- the bug


def test_linux_shell_child_gets_the_runtime_dir_and_the_user_bus(tree):
    # the bug: a system unit's Shell child had neither, so `systemctl --user` could not connect
    runtime = tree.runtime_dir(os.geteuid())
    out = server.Shell('echo "$XDG_RUNTIME_DIR $DBUS_SESSION_BUS_ADDRESS"')
    assert out == f"{runtime} unix:path={runtime}/bus"


def test_linux_shell_child_gets_the_servers_own_display(tree, monkeypatch):
    tree.runtime_dir(os.geteuid())
    tree.x_display(0)
    xauth = tree.file("Xauthority")
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":0", xauth))
    assert server.Shell('echo "$DISPLAY $XAUTHORITY"') == f":0 {xauth}"


# --------------------------------------------------------------------------- never override


def test_a_variable_the_server_has_is_never_overridden(tree, se, monkeypatch):
    tree.runtime_dir(os.geteuid())
    tree.x_display(0)
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":0", tree.file("Xauthority")))
    base = {
        "XDG_RUNTIME_DIR": "/srv/rt",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/srv/bus",
        "DISPLAY": ":9",
        "XAUTHORITY": "/srv/xa",
        "WAYLAND_DISPLAY": "w-9",
        "PATH": "/usr/bin",
    }
    assert se.shell_env(base) == base


def test_the_servers_own_value_reaches_the_shell_child(tree, monkeypatch):
    runtime = tree.runtime_dir(os.geteuid())
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/srv/rt")
    assert server.Shell('echo "$XDG_RUNTIME_DIR $DBUS_SESSION_BUS_ADDRESS"') == f"/srv/rt unix:path={runtime}/bus"


def test_an_empty_value_counts_as_not_set(tree, se):
    runtime = tree.runtime_dir(os.geteuid())
    env = se.shell_env({**_base(), "XDG_RUNTIME_DIR": "", "DBUS_SESSION_BUS_ADDRESS": ""})
    assert _added(env) == {"XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def test_shell_env_starts_from_the_servers_environment_and_never_changes_it(tree, se, monkeypatch):
    runtime = tree.runtime_dir(os.geteuid())
    monkeypatch.setenv("REMOTEOS_T52", "kept")
    env = se.shell_env()
    assert env["REMOTEOS_T52"] == "kept"
    assert env["XDG_RUNTIME_DIR"] == runtime
    assert "XDG_RUNTIME_DIR" not in os.environ


# --------------------------------------------------------------------------- runtime dir and bus


def test_no_runtime_dir_adds_nothing(tree, se):
    assert _added(se.shell_env(_base())) == {}


def test_a_runtime_dir_without_a_bus_socket_adds_only_the_dir(tree, se):
    runtime = tree.runtime_dir(os.geteuid(), bus=False)
    assert _added(se.shell_env(_base())) == {"XDG_RUNTIME_DIR": runtime}


def test_a_bus_that_is_not_a_socket_is_not_given(tree, se):
    runtime = tree.runtime_dir(os.geteuid(), bus=False)
    with open(os.path.join(runtime, "bus"), "w") as fh:
        fh.write("not a socket")
    assert _added(se.shell_env(_base())) == {"XDG_RUNTIME_DIR": runtime}


def test_a_runtime_dir_owned_by_another_user_adds_nothing(tree, se, monkeypatch):
    # /run/user/<euid> exists, but it is not the euid's: neither the dir nor its bus is ours
    euid = os.geteuid() + 4321
    tree.runtime_dir(euid)  # really owned by the test user
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    assert _added(se.shell_env(_base())) == {}


@pytest.mark.parametrize("euid", [0, 4242], ids=["root", "a-user"])
def test_as_root_nothing_is_added(tree, se, loginctl, monkeypatch, euid):
    # root's own runtime dir, bus, X display and session all exist: root still gets none of them,
    # and the session is never consulted. The same tree for a user is the positive control.
    runtime = tree.runtime_dir(euid)
    tree.report_owner(runtime, euid)
    tree.x_display(0)
    xauth = tree.file("Xauthority")
    monkeypatch.setattr(desktop, "_SESSION", _x11(euid, ":0", xauth))
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    added = _added(se.shell_env(_base()))
    if euid == 0:
        assert added == {}
        assert loginctl.listed == 0
    else:
        assert added == {
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
            "DISPLAY": ":0",
            "XAUTHORITY": xauth,
        }


# --------------------------------------------------------------------------- the display


def test_the_servers_own_session_gives_display_and_xauthority(tree, se, loginctl, monkeypatch):
    tree.x_display(0)
    xauth = tree.file("Xauthority")
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":0.0", xauth))
    assert _added(se.shell_env(_base())) == {"DISPLAY": ":0.0", "XAUTHORITY": xauth}
    assert loginctl.listed == 0  # a live X11 session of ours is never re-detected


def test_another_users_session_gives_no_display(tree, se, loginctl, monkeypatch):
    other = os.geteuid() + 4321
    tree.x_display(0)
    monkeypatch.setattr(desktop, "_SESSION", _x11(other, ":0", tree.file("Xauthority")))
    assert _added(se.shell_env(_base())) == {}
    assert loginctl.listed == 0  # not ours: nothing to re-detect either


def test_a_missing_xauthority_file_is_not_given(tree, se, monkeypatch):
    tree.x_display(0)
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":0", os.path.join(tree.root, "gone")))
    assert _added(se.shell_env(_base())) == {"DISPLAY": ":0"}


def test_xauthority_goes_only_with_the_sessions_display(tree, se, monkeypatch):
    tree.x_display(0)
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":0", tree.file("Xauthority")))
    env = se.shell_env({**_base(), "DISPLAY": ":9"})
    assert (env["DISPLAY"], "XAUTHORITY" in env) == (":9", False)


@pytest.mark.parametrize("display", ["localhost:10.0", "", None, ":x"])
def test_a_display_that_is_not_a_local_x_socket_is_not_given(tree, se, monkeypatch, display):
    # nor its XAUTHORITY: with no session display, "the child's DISPLAY is the session's" must
    # never hold just because both are missing
    tree.x_display(0)
    tree.x_display(10)
    xauth = tree.file("Xauthority")
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), display, xauth))
    # there is no X socket for it, so it is re-detected: the env branch finds the same session
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XAUTHORITY", xauth)
    if display is not None:
        monkeypatch.setenv("DISPLAY", display)
    assert _added(se.shell_env(_base())) == {}
    assert (desktop.get_session().uid, desktop.get_session().display) == (os.geteuid(), display)


def test_a_gone_display_is_re_detected_and_the_new_one_given(tree, se, monkeypatch):
    # the cached session's X socket is gone (the user logged out and in again on :5)
    xauth = tree.file("Xauthority")
    tree.x_display(5)
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":3", xauth))
    # the provider's detection finds the new session (its env branch: a non-root server
    # whose own environment carries the display)
    monkeypatch.setenv("DISPLAY", ":5")
    monkeypatch.setenv("XAUTHORITY", xauth)
    assert _added(se.shell_env(_base())) == {"DISPLAY": ":5", "XAUTHORITY": xauth}
    assert desktop.get_session().display == ":5"


def test_a_gone_display_with_no_new_session_gives_none(tree, se, loginctl, monkeypatch):
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":3", tree.file("Xauthority")))
    assert _added(se.shell_env(_base())) == {}
    assert loginctl.listed == 1
    assert desktop.get_session().mode == "headless"


def test_a_cache_without_a_graphical_session_is_never_re_detected(tree, se, loginctl):
    # a headless box (cam1..4): every Shell call reads the start-up cache, no loginctl
    tree.x_display(0)
    for _ in range(3):
        assert "DISPLAY" not in se.shell_env(_base())
    assert loginctl.listed == 0


def test_a_gone_display_is_re_detected_at_most_once_per_change_of_the_x_sockets(tree, se, monkeypatch, caplog):
    # the detection keeps finding :3, whose socket stays gone: one look, not one per Shell call
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(desktop, "_SESSION", _x11(os.geteuid(), ":3"))
    monkeypatch.setenv("DISPLAY", ":3")  # the provider's env branch finds :3 again
    for _ in range(3):
        assert "DISPLAY" not in se.shell_env(_base())
    tree.x_display(1)  # the X sockets change (another X server starts): one more look
    for _ in range(3):
        se.shell_env(_base())
    looks = [r for r in caplog.records if r.name == LOGGER and "re-detecting" in r.getMessage()]
    assert len(looks) == 2


def test_a_failing_session_lookup_never_fails_the_shell_call(tree, se, monkeypatch, caplog):
    # the provider cannot even be imported (a missing Linux dependency)
    runtime = tree.runtime_dir(os.geteuid())
    import remoteos.platform.linux as linux_pkg

    monkeypatch.delattr(linux_pkg, "desktop")
    monkeypatch.setitem(sys.modules, "remoteos.platform.linux.desktop", None)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    out = server.Shell('echo "$XDG_RUNTIME_DIR|$DISPLAY|"')
    assert out == f"{runtime}||"
    [record] = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    # a None entry in sys.modules is ModuleNotFoundError, ImportError's subclass; only the type is logged
    assert "(ModuleNotFoundError)" in record.getMessage()


# --------------------------------------------------------------------------- Wayland


def test_a_wayland_session_of_ours_gives_wayland_display(tree, se, monkeypatch):
    runtime = tree.runtime_dir(os.geteuid())
    tree.socket(os.path.join(runtime, "wayland-0"))
    session = desktop.SessionInfo(mode="wayland", display=":0", uid=os.geteuid(), user="someone")
    monkeypatch.setattr(desktop, "_SESSION", session)
    env = se.shell_env(_base())
    assert _added(env) == {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "WAYLAND_DISPLAY": "wayland-0",
    }
    assert desktop.get_session().mode == "wayland"


@pytest.mark.parametrize("mode, socket_there", [("wayland", False), ("x11", True)])
def test_wayland_display_needs_a_wayland_session_and_its_socket(tree, se, monkeypatch, mode, socket_there):
    runtime = tree.runtime_dir(os.geteuid())
    if socket_there:
        tree.socket(os.path.join(runtime, "wayland-0"))
    monkeypatch.setattr(desktop, "_SESSION", desktop.SessionInfo(mode=mode, uid=os.geteuid(), user="someone"))
    monkeypatch.setenv("XDG_SESSION_TYPE", mode)
    assert "WAYLAND_DISPLAY" not in se.shell_env(_base())


# --------------------------------------------------------------------------- logging


def test_the_log_names_the_added_variables_once_per_change_never_their_values(tree, se, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = tree.runtime_dir(os.geteuid(), bus=False)
    se.shell_env(_base())
    se.shell_env(_base())
    first = [r.getMessage() for r in caplog.records if r.name == LOGGER and "XDG_RUNTIME_DIR" in r.getMessage()]
    assert len(first) == 1
    tree.socket(os.path.join(runtime, "bus"))
    se.shell_env(_base())
    both = [r.getMessage() for r in caplog.records if "DBUS_SESSION_BUS_ADDRESS" in r.getMessage()]
    assert len(both) == 1
    assert all(tree.root not in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- the runner


def test_run_child_env_is_the_whole_environment_of_a_real_bash_child():
    env = {"PATH": os.environ["PATH"], "REMOTEOS_T52": "from-env"}
    argv = ["/bin/bash", "-c", 'printf "%s|%s" "$REMOTEOS_T52" "${HOME-unset}"']
    result = command.run_child(argv, 10, False, env=env)
    assert (result.returncode, result.stdout) == (0, "from-env|unset")


def test_run_child_without_env_inherits_the_servers(monkeypatch):
    monkeypatch.setenv("REMOTEOS_T52", "inherited")
    result = command.run_child(["/bin/bash", "-c", 'printf %s "$REMOTEOS_T52"'], 10, False)
    assert result.stdout == "inherited"


# --------------------------------------------------------------------------- Windows / macOS


@pytest.fixture
def popen(monkeypatch):
    """Record what Shell hands ``subprocess.Popen`` (the OS process boundary)."""
    calls: list[tuple[list[str], dict]] = []

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            calls.append((list(args), kwargs))
            self.kwargs = kwargs

        def communicate(self, timeout=None):
            return (returned(self.kwargs, "ok\n"), returned(self.kwargs, ""))

    monkeypatch.setattr(server.subprocess, "Popen", _Proc)
    return calls


@pytest.mark.parametrize("windows, macos", [(True, False), (False, True)], ids=["windows", "macos"])
def test_windows_and_macos_shell_children_keep_the_servers_environment(
    tree, monkeypatch, popen, windows_python, windows, macos
):
    tree.runtime_dir(os.geteuid())  # would be added on Linux
    monkeypatch.setattr(server, "is_windows", lambda: windows)
    monkeypatch.setattr(server, "is_macos", lambda: macos)
    assert server.Shell("Get-Date") == "ok"
    [(args, kwargs)] = popen
    if windows:
        assert_runs_powershell(args, windows_python, "Get-Date", cwd="")
    else:
        assert args == ["/bin/zsh", "-c", "Get-Date"]
    assert kwargs.get("env") is None


def test_the_linux_shell_child_gets_shell_env(tree, monkeypatch, popen):
    runtime = tree.runtime_dir(os.geteuid())
    monkeypatch.setattr(server, "is_windows", lambda: False)
    monkeypatch.setattr(server, "is_macos", lambda: False)
    assert server.Shell("true") == "ok"
    [(args, kwargs)] = popen
    assert args == ["/bin/bash", "-c", "true"]
    assert kwargs["env"]["XDG_RUNTIME_DIR"] == runtime
    assert kwargs["env"]["PATH"] == os.environ["PATH"]
