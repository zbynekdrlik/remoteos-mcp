"""Shell tool: the working directory goes to the subprocess, never into the command text (#31).

The Linux cases run the real tool through a real ``/bin/bash``. The Windows and
macOS branches cannot run their shell here, so ``subprocess.Popen`` (the OS
process boundary) is replaced by a recorder; the command line and the ``cwd``
the tool hands it are what is checked.
"""

from __future__ import annotations

import pytest
from fake_console_child import assert_runs_powershell, returned
from tool_surface import server
from tool_test_support import tool_error


def test_command_without_cwd_runs_and_returns_its_output():
    assert server.Shell("echo hi") == "hi"


def test_cwd_with_a_space_runs_the_command_in_that_directory(tmp_path):
    work = tmp_path / "dir with space"
    work.mkdir()
    assert server.Shell("pwd -P", cwd=str(work)) == str(work.resolve())


def test_cwd_with_a_quote_runs_the_command_in_that_directory(tmp_path):
    work = tmp_path / "it's here"
    work.mkdir()
    assert server.Shell("pwd -P", cwd=str(work)) == str(work.resolve())


def test_missing_cwd_is_a_clear_error_and_the_command_never_runs(tmp_path):
    marker = tmp_path / "ran"
    missing = tmp_path / "nope"
    out = tool_error(lambda: server.Shell(f"touch '{marker}'", cwd=str(missing)))
    assert out == f"Shell error: working directory not found: {missing}"
    assert not marker.exists()


def test_cwd_that_is_a_file_is_an_error_and_the_command_never_runs(tmp_path):
    marker = tmp_path / "ran"
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    out = tool_error(lambda: server.Shell(f"touch '{marker}'", cwd=str(a_file)))
    assert out == f"Shell error: working directory is not a directory: {a_file}"
    assert not marker.exists()


def test_cwd_text_is_never_executed_as_a_command(tmp_path):
    pwned = tmp_path / "pwned"
    out = tool_error(lambda: server.Shell("true", cwd=f"{tmp_path}/x; touch {pwned}"))
    assert out.startswith("Shell error: working directory not found: ")
    assert not pwned.exists()


def test_cwd_environment_variables_are_expanded_like_cd_did(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTEOS_TEST_WORK", str(tmp_path))
    (tmp_path / "proj").mkdir()
    assert server.Shell("pwd -P", cwd="$REMOTEOS_TEST_WORK/proj") == str((tmp_path / "proj").resolve())


def test_cwd_tilde_is_expanded_like_cd_did(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "proj").mkdir()
    assert server.Shell("pwd -P", cwd="~/proj") == str((tmp_path / "proj").resolve())


@pytest.fixture
def popen(monkeypatch):
    """Record what the tool hands ``subprocess.Popen`` (the OS process boundary)."""
    calls: list[tuple[list[str], dict]] = []

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self, args, **kwargs):
            calls.append((list(args), kwargs))
            self.kwargs = kwargs

        def communicate(self, timeout=None):
            calls[-1][1]["communicate_timeout"] = timeout  # what the child is given to finish in (#125)
            # the type subprocess returns: text in text mode, else bytes (#48)
            return (returned(self.kwargs, "ok\n"), returned(self.kwargs, ""))

    monkeypatch.setattr(server.subprocess, "Popen", _Proc)
    return calls


@pytest.mark.parametrize(
    "windows, macos, argv",
    [
        (True, False, None),  # PowerShell under the console trampoline (#48 round 2)
        (False, True, ["/bin/zsh", "-c", "Get-ChildItem"]),
        (False, False, ["/bin/bash", "-c", "Get-ChildItem"]),
    ],
    ids=["windows", "macos", "linux"],
)
def test_every_os_passes_cwd_to_the_process_and_leaves_the_command_alone(
    tmp_path, monkeypatch, popen, windows_python, windows, macos, argv
):
    work = tmp_path / "Program Files"
    work.mkdir()
    monkeypatch.setattr(server, "is_windows", lambda: windows)
    monkeypatch.setattr(server, "is_macos", lambda: macos)
    assert server.Shell("Get-ChildItem", cwd=str(work)) == "ok"
    [(args, kwargs)] = popen
    if windows:
        # review round 1 (#48 round 2): the console trampoline starts in the server's directory, so
        # "powershell" is never looked up in the user's; it starts PowerShell in `cwd`
        assert_runs_powershell(args, windows_python, "Get-ChildItem", cwd=str(work))
        assert kwargs["cwd"] is None
    else:
        assert args == argv
        assert kwargs["cwd"] == str(work)


def test_without_cwd_the_process_inherits_the_server_directory(monkeypatch, popen, windows_python):
    monkeypatch.setattr(server, "is_windows", lambda: True)
    assert server.Shell("Get-Location") == "ok"
    [(args, kwargs)] = popen
    assert_runs_powershell(args, windows_python, "Get-Location", cwd="")
    assert kwargs.get("cwd") is None


def test_output_that_is_not_utf8_is_still_the_commands_answer():
    # review round 3: bytes outside the expected encoding produced a 'utf-8 codec' Shell error on
    # Linux (on Windows the reader thread dropped stdout: "(no output)") and lost the output
    out = server.Shell("printf 'ok \\220 done'")
    assert out.startswith("ok ") and out.endswith(" done")


# The Windows output encoding (#32 rounds 4-5: its own console; #48: UTF-8, exact) is tested in
# tests/test_output_encoding.py.


# --------------------------------------------------------------------------- the timeout (#125)


def test_a_command_that_outlives_its_timeout_is_stopped_and_the_error_says_what_to_do(tmp_path):
    # #125: a caller guessed timeout=120 for a firewall install; Shell stopped it half done and the
    # bare "Command timed out after 120s" read like a server limit. The reply must say the command
    # was stopped, that the rest never ran, and that a larger timeout (no maximum) or TaskCreate is
    # the way, so the next attempt is not another guess.
    before, after = tmp_path / "before", tmp_path / "after"
    out = tool_error(lambda: server.Shell(f"touch '{before}'; sleep 30; touch '{after}'", timeout=1))
    assert out == (
        "Command timed out after 1s and was stopped: the rest of the command did not run "
        "(work it had already handed to a service or a detached process may still be running). "
        "Pass a larger timeout (Shell has no upper limit). For work longer than a call can wait, "
        "start it detached or with TaskCreate and check its result with a later call."
    )
    assert before.exists()
    assert not after.exists()  # the part after the timeout really never ran


def test_without_a_timeout_the_command_gets_the_default_of_120_seconds(monkeypatch, popen):
    # #125: the default is chosen from the gateway journal (p99 of real Shell calls = 89 s; the
    # old 30 s sat at the p95 and a visible cluster of calls was killed there)
    monkeypatch.setattr(server, "is_windows", lambda: False)
    monkeypatch.setattr(server, "is_macos", lambda: False)
    assert server.Shell("true") == "ok"
    [(_, kwargs)] = popen
    assert kwargs["communicate_timeout"] == 120


def test_the_description_states_the_default_the_missing_maximum_and_taskcreate():
    # what an agent reads before it picks a value (tools/list); the golden file pins the exact text
    doc = " ".join(server.Shell.__doc__.split())
    timeout_arg = doc.split("timeout:", 1)[1].split("cwd:", 1)[0]  # the parameter's own description
    assert "default 120" in timeout_arg
    assert "no upper limit" in timeout_arg
    assert "TaskCreate" in timeout_arg
    assert "detached" in timeout_arg  # review round 1: TaskCreate has no real run-once mode yet


def test_the_cli_default_is_the_tools_default():
    # remoteos-call sends Shell's timeout explicitly; it must not quietly keep the old 30 s
    import inspect

    from remoteos import cli_call

    default = inspect.signature(server.Shell).parameters["timeout"].default
    assert cli_call.SHELL_DEFAULT_TIMEOUT_S == default == 120
