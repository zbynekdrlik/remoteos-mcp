"""A child that times out is killed with everything it started (#48 round 2).

From round 2 on, a Windows PowerShell child runs under a console trampoline, so the process the
server starts is not the one doing the work. Killing only that process (what ``subprocess.run``
does on a timeout) would leave PowerShell, and anything it started, running with the server's
pipes open. On Windows ``subprocess.run`` then waits in ``communicate()`` for those pipes with no
timeout at all (CPython gh-81605), and the tool call never returns. So ``run_child`` kills the
whole tree, as Shell already did:
- Windows: ``taskkill /F /T /PID <child>``, in its own hidden console;
- POSIX: the child's own process group (it is started in a new session), never the caller's:
  SIGTERM, then SIGKILL after a short grace (review round 1).
Review round 1 also: the reap never logs the command, an interrupted wait kills the tree too, a
timeout names the PowerShell command rather than the trampoline, and a child's stdin is NUL.

The POSIX cases run real processes. The Windows cases fake only ``subprocess.Popen`` (a child
that never finishes) and the ``taskkill`` run.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest
from tool_surface import server

from remoteos.platform.command import KILL_GRACE_S, powershell_argv, run_child, run_command

# A child that starts a grandchild sharing its stdout, writes the grandchild's pid to a file, then
# hangs. The grandchild hangs too, holding the pipe open.
_PARENT_OF_A_HANGING_GRANDCHILD = """
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(grandchild.pid))
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def grandchild_pid_file(tmp_path):
    """Where the child writes its grandchild's pid; a grandchild left alive is killed afterwards."""
    path = tmp_path / "grandchild.pid"
    yield path
    if path.exists():
        pid = int(path.read_text())
        if _alive(pid):
            os.kill(pid, 9)


def test_a_timed_out_child_is_killed_with_the_grandchild_holding_its_pipe(grandchild_pid_file):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", _PARENT_OF_A_HANGING_GRANDCHILD, str(grandchild_pid_file)], timeout=2)
    assert time.monotonic() - started < 15  # the call returns; it never waits for the grandchild
    grandchild = int(grandchild_pid_file.read_text())
    assert _gone_within(grandchild, 5), "the grandchild survived the timeout"


def test_a_timed_out_shell_command_is_an_error_and_its_children_are_killed(grandchild_pid_file):
    # unchanged for Shell (it always killed the tree), now through the shared runner
    script = Path(grandchild_pid_file.parent / "parent.py")
    script.write_text(_PARENT_OF_A_HANGING_GRANDCHILD)
    started = time.monotonic()
    with pytest.raises(Exception) as exc:
        server.Shell(f"'{sys.executable}' '{script}' '{grandchild_pid_file}'", timeout=2)
    assert str(exc.value) == "Command timed out after 2s"
    assert time.monotonic() - started < 15
    assert _gone_within(int(grandchild_pid_file.read_text()), 5), "the grandchild survived the timeout"


_KILLS_A_CHILD_IN_ITS_OWN_GROUP = """
import subprocess, sys
from remoteos.platform.command import kill_process_tree
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # same group as this process
kill_process_tree(child, windows=False)
print("caller alive, child", child.poll())
"""


def test_killing_a_child_that_shares_the_callers_group_never_kills_the_caller():
    # killpg on a child that does not lead its own group would kill the caller's whole group (the
    # server). Run in a separate session so a broken guard can only kill that helper.
    env = dict(os.environ, PYTHONPATH=str(Path(server.__file__).parents[1]))
    done = subprocess.run(
        [sys.executable, "-c", _KILLS_A_CHILD_IN_ITS_OWN_GROUP],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("caller alive, child ")
    assert done.stdout.strip() != "caller alive, child None"  # the child itself was killed and reaped


class _NeverFinishes:
    """A Windows child at the Popen boundary that is still running when the timeout expires.

    ``mode``: "timeout" = it ends once killed; "unreapable" = even the reap after the kill times out
    (a grandchild holds the pipe); "interrupt" = ``communicate`` is interrupted (KeyboardInterrupt).
    """

    pid = 31337
    returncode = None

    def __init__(self, args, mode="timeout", **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.mode = mode
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if self.mode == "interrupt" and not self.killed:
            raise KeyboardInterrupt
        if timeout is not None and (self.mode == "unreapable" or not self.killed):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return (b"", b"")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture
def windows_never_finishing(monkeypatch):
    """Windows, a child that never finishes, and a recorded taskkill; yields ``(started, taskkills, set_mode)``."""
    monkeypatch.setattr(sys, "platform", "win32")
    started: list[_NeverFinishes] = []
    mode = {"value": "timeout"}

    def _popen(args, **kwargs):
        child = _NeverFinishes(args, mode["value"], **kwargs)
        started.append(child)
        return child

    taskkills: list[tuple[list[str], dict]] = []
    real_run = subprocess.run

    def _run(cmd, **kwargs):
        if cmd[0] == "taskkill":
            taskkills.append((list(cmd), kwargs))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(subprocess, "run", _run)
    yield started, taskkills, lambda value: mode.__setitem__("value", value)


def test_a_timed_out_windows_child_is_killed_with_taskkill_of_its_tree(windows_never_finishing):
    started, taskkills, _ = windows_never_finishing
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_command(["tscon", "2", "/dest:console"], timeout=3)
    assert exc.value.cmd == ["tscon", "2", "/dest:console"]  # review round 2: no trampoline, kept as is
    [(argv, kwargs)] = taskkills
    assert argv == ["taskkill", "/F", "/T", "/PID", "31337"]
    # review round 1: its own hidden console, so no window flashes up when the server runs as pythonw.exe
    assert kwargs.get("creationflags") == 0x08000000
    assert started[0].killed


def test_a_timeout_names_the_powershell_command_not_the_trampoline(windows_never_finishing, windows_python):
    # review round 1: the error the client sees names the command as before round 2, without the
    # ~700 characters of the console trampoline in front of it
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_command(powershell_argv("Start-Sleep 60"), timeout=3)
    assert exc.value.cmd == ["powershell", "-NoProfile", "-Command", "Start-Sleep 60"]
    shown = "['powershell', '-NoProfile', '-Command', 'Start-Sleep 60']"
    assert str(exc.value) == f"Command '{shown}' timed out after 3 seconds"


def test_a_failed_reap_never_logs_the_command(windows_never_finishing, caplog):
    # review round 1: TimeoutExpired's text is the whole argv, the user's command included (#31: logs
    # carry no tool arguments). The marker is built at run time, so no source line in a logged frame has it.
    _, _, set_mode = windows_never_finishing
    set_mode("unreapable")
    marker = "secret-" + "marker-48"
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        with pytest.raises(subprocess.TimeoutExpired):
            run_child(["tool", marker], timeout=1, windows=True)
    assert "reaping 31337 failed: TimeoutExpired" in caplog.text
    assert marker not in caplog.text


def test_an_interrupted_wait_still_kills_the_childs_tree(windows_never_finishing):
    # review round 1: subprocess.run killed its child on any exception, not only on a timeout
    started, taskkills, set_mode = windows_never_finishing
    set_mode("interrupt")
    with pytest.raises(KeyboardInterrupt):
        run_command(["tscon", "2", "/dest:console"], timeout=3)
    assert [argv for argv, _ in taskkills] == [["taskkill", "/F", "/T", "/PID", "31337"]]
    assert started[0].killed


_GRANDCHILD_THAT_IGNORES_SIGTERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
)
_PARENT_OF_A_GRANDCHILD_THAT_IGNORES_SIGTERM = f"""
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", {_GRANDCHILD_THAT_IGNORES_SIGTERM!r}])
open(sys.argv[1], "w").write(str(grandchild.pid))
time.sleep(60)
"""


def test_a_grandchild_that_ignores_sigterm_is_killed_too(grandchild_pid_file):
    # review round 1: SIGTERM to the group first, then SIGKILL after a short grace
    started = time.monotonic()
    cmd = [sys.executable, "-c", _PARENT_OF_A_GRANDCHILD_THAT_IGNORES_SIGTERM, str(grandchild_pid_file)]
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(cmd, timeout=5)
    assert time.monotonic() - started < 15
    assert _gone_within(int(grandchild_pid_file.read_text()), 5), "the grandchild survived the timeout"


# Review round 2: a grandchild that ignores SIGTERM and does NOT hold our pipes, so the reap
# succeeds; the SIGKILL to the group must come anyway.
_DETACHED_GRANDCHILD_THAT_IGNORES_SIGTERM = f"""
import subprocess, sys, time
grandchild = subprocess.Popen(
    [sys.executable, "-c", {_GRANDCHILD_THAT_IGNORES_SIGTERM!r}],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
open(sys.argv[1], "w").write(str(grandchild.pid))
time.sleep(60)
"""


def test_a_grandchild_that_ignores_sigterm_without_our_pipes_is_killed_too(grandchild_pid_file):
    cmd = [sys.executable, "-c", _DETACHED_GRANDCHILD_THAT_IGNORES_SIGTERM, str(grandchild_pid_file)]
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(cmd, timeout=5)
    assert _gone_within(int(grandchild_pid_file.read_text()), 5), "the grandchild survived the timeout"


# Review round 2: SIGTERM comes first, so a process that handles it can clean up (a removed SIGTERM
# step survived the suite). The handler leaves a marker, then exits.
# Review round 5: the grandchild writes its OWN pid file, after its handler is installed, so a SIGTERM
# can never find it still starting up.
_GRANDCHILD_THAT_CLEANS_UP_ON_SIGTERM = (
    "import os, signal, sys, time\n"
    "def done(*_):\n"
    "    open(sys.argv[1], 'w').write('cleaned up')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, done)\n"
    "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)
_PARENT_OF_A_GRANDCHILD_THAT_CLEANS_UP = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {_GRANDCHILD_THAT_CLEANS_UP_ON_SIGTERM!r}, sys.argv[2], sys.argv[1]])
time.sleep(60)
"""


def test_the_group_gets_sigterm_before_sigkill(grandchild_pid_file):
    marker = grandchild_pid_file.parent / "cleaned-up"
    cmd = [sys.executable, "-c", _PARENT_OF_A_GRANDCHILD_THAT_CLEANS_UP, str(grandchild_pid_file), str(marker)]
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(cmd, timeout=5)
    assert _gone_within(int(grandchild_pid_file.read_text()), 5)
    assert marker.read_text() == "cleaned up"


# Review round 3: the usual POSIX timeout. The child (the shell) has exited and a process it left
# behind (`x &`, `nohup x &`) holds the pipe. It was reparented to init, so it is no longer the
# child's descendant, but it is still in the child's process group, and it gets the grace too.
_GRANDCHILD_THAT_CLEANS_UP_SLOWLY = (
    "import os, signal, sys, time\n"
    "def done(*_):\n"
    "    time.sleep(0.3)\n"
    "    open(sys.argv[1], 'w').write('cleaned up')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, done)\n"
    "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)
_PARENT_THAT_LEAVES_A_GRANDCHILD_BEHIND = f"""
import subprocess, sys
subprocess.Popen([sys.executable, "-c", {_GRANDCHILD_THAT_CLEANS_UP_SLOWLY!r}, sys.argv[2], sys.argv[1]])
"""


def test_a_process_left_behind_in_the_group_gets_the_sigterm_grace(grandchild_pid_file):
    marker = grandchild_pid_file.parent / "cleaned-up"
    cmd = [sys.executable, "-c", _PARENT_THAT_LEAVES_A_GRANDCHILD_BEHIND, str(grandchild_pid_file), str(marker)]
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(cmd, timeout=5)
    assert _gone_within(int(grandchild_pid_file.read_text()), 5)
    assert marker.exists() and marker.read_text() == "cleaned up", "SIGKILL came before its SIGTERM handler finished"


def _seed_a_stale_psutil_entry(pid_file: Path, seeded: list) -> None:
    """Once the grandchild's pid is known, put an OLDER identity for that pid in psutil's process_iter
    cache: what that cache holds after the pid was reused since an earlier iteration (ListProcesses and
    KillProcess iterate too)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            pid = int(pid_file.read_text())
            break
        except (OSError, ValueError):
            time.sleep(0.02)
    else:
        return
    stale = psutil.Process(pid)
    stale._ident = (pid, stale.create_time() - 1000.0)
    psutil._pmap[pid] = stale
    # review round 5: the seed must really be a different identity, or a renamed psutil internal would
    # make this test a silent copy of the one above
    seeded.append((pid, stale != psutil.Process(pid)))


def test_a_member_whose_pid_psutil_cached_stale_still_gets_the_grace(grandchild_pid_file):
    # review round 4: process_iter() hands back its CACHED Process for a known pid; with an old start
    # time, is_running() says "exited" and the member got no SIGTERM grace
    marker = grandchild_pid_file.parent / "cleaned-up"
    cmd = [sys.executable, "-c", _PARENT_THAT_LEAVES_A_GRANDCHILD_BEHIND, str(grandchild_pid_file), str(marker)]
    seeded: list[tuple[int, bool]] = []
    seeder = threading.Thread(target=_seed_a_stale_psutil_entry, args=(grandchild_pid_file, seeded))
    seeder.start()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_command(cmd, timeout=5)
    finally:
        seeder.join()
        for pid, _ in seeded:
            psutil._pmap.pop(pid, None)
    assert seeded, "the grandchild never wrote its pid"
    assert seeded[0][1], "the seeded identity was not stale: psutil's internals changed"
    assert _gone_within(int(grandchild_pid_file.read_text()), 5)
    assert marker.exists() and marker.read_text() == "cleaned up", "the stale identity cost the grace"


def test_a_failing_process_scan_still_signals_the_group(monkeypatch, grandchild_pid_file):
    # review round 4: kill_process_tree never raises; a failing scan only skips the grace wait. Review
    # round 5: a grandchild holding the pipe proves the GROUP signals still go out (a lone child dies
    # by proc.kill() either way)
    def _broken_pids():
        raise RuntimeError("/proc is not readable")

    monkeypatch.setattr(psutil, "pids", _broken_pids)
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: _broken_pids())
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", _PARENT_OF_A_HANGING_GRANDCHILD, str(grandchild_pid_file)], timeout=5)
    assert time.monotonic() - started < 15
    assert _gone_within(int(grandchild_pid_file.read_text()), 5), "the group was never signalled"


def test_a_failing_grace_wait_still_sends_sigkill(monkeypatch, grandchild_pid_file):
    # review round 5: an unexpected OSError from psutil while waiting must neither skip the SIGKILL nor
    # replace TimeoutExpired. The grandchild ignores SIGTERM, so only the SIGKILL ends it.
    def _broken_status(self):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(psutil.Process, "status", _broken_status)
    cmd = [sys.executable, "-c", _DETACHED_GRANDCHILD_THAT_IGNORES_SIGTERM, str(grandchild_pid_file)]
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(cmd, timeout=5)
    assert _gone_within(int(grandchild_pid_file.read_text()), 5), "the SIGKILL never went out"


def test_a_group_that_exits_on_sigterm_never_waits_out_the_grace():
    # review round 5: only the group's members are waited for; a child that dies on SIGTERM ends the
    # wait at once (a scan that waited on other processes would always spend the full 2 s grace)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", "import time; time.sleep(60)"], timeout=1)
    assert time.monotonic() - started < 1 + KILL_GRACE_S - 0.5


_RUNS_A_CHILD_THAT_READS_STDIN = """
import sys
from remoteos.platform.command import run_command
print(run_command([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]))
"""


def test_a_child_never_reads_the_servers_stdin():
    # review round 1: under --transport stdio the server's stdin is the MCP JSON-RPC stream; a child
    # that reads stdin (bash `read`, PowerShell `$input`) must get NUL, never the protocol's frames
    env = dict(os.environ, PYTHONPATH=str(Path(server.__file__).parents[1]))
    done = subprocess.run(
        [sys.executable, "-c", _RUNS_A_CHILD_THAT_READS_STDIN],
        input='{"jsonrpc": "2.0", "id": 7}\n',
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "''"
