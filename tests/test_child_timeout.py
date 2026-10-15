"""A child that times out is killed with everything it started (#48 round 2).

From round 2 on, a Windows PowerShell child runs under a console trampoline, so the process the
server starts is not the one doing the work. Killing only that process (what ``subprocess.run``
does on a timeout) would leave PowerShell, and anything it started, running with the server's
pipes open. On Windows ``subprocess.run`` then waits in ``communicate()`` for those pipes with no
timeout at all (CPython gh-81605), and the tool call never returns. So ``run_child`` kills the
whole tree, as Shell already did:
- Windows: ``taskkill /F /T /PID <child>``;
- POSIX: the child's own process group (it is started in a new session), never the caller's.

The POSIX cases run real processes. The Windows case fakes only ``subprocess.Popen`` (a child
that never finishes) and the ``taskkill`` run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import remoteos.__main__ as server
from remoteos.platform.command import run_command

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
    """A Windows child at the Popen boundary that is still running when the timeout expires."""

    pid = 31337
    returncode = None

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if timeout is not None and not self.killed:
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


def test_a_timed_out_windows_child_is_killed_with_taskkill_of_its_tree(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    started: list[_NeverFinishes] = []

    def _popen(args, **kwargs):
        child = _NeverFinishes(args, **kwargs)
        started.append(child)
        return child

    killed: list[list[str]] = []
    real_run = subprocess.run

    def _run(cmd, **kwargs):
        if cmd[0] == "taskkill":
            killed.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(["tscon", "2", "/dest:console"], timeout=3)
    assert killed == [["taskkill", "/F", "/T", "/PID", "31337"]]
    assert started[0].killed
