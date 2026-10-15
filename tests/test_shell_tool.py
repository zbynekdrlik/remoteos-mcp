"""Shell tool: the working directory goes to the subprocess, never into the command text (#31).

The Linux cases run the real tool through a real ``/bin/bash``. The Windows and
macOS branches cannot run their shell here, so ``subprocess.Popen`` (the OS
process boundary) is replaced by a recorder; the command line and the ``cwd``
the tool hands it are what is checked.
"""

from __future__ import annotations

import pytest
from fake_console_child import POWERSHELL_UTF8_PREFIX, returned
from tool_test_support import tool_error

import remoteos.__main__ as server


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
            # the type subprocess returns: text in text mode, else bytes (#48)
            return (returned(self.kwargs, "ok\n"), returned(self.kwargs, ""))

    monkeypatch.setattr(server.subprocess, "Popen", _Proc)
    return calls


@pytest.mark.parametrize(
    "windows, macos, argv",
    [
        (True, False, ["powershell", "-NoProfile", "-Command", POWERSHELL_UTF8_PREFIX + "\nGet-ChildItem"]),
        (False, True, ["/bin/zsh", "-c", "Get-ChildItem"]),
        (False, False, ["/bin/bash", "-c", "Get-ChildItem"]),
    ],
    ids=["windows", "macos", "linux"],
)
def test_every_os_passes_cwd_to_the_process_and_leaves_the_command_alone(
    tmp_path, monkeypatch, popen, windows, macos, argv
):
    work = tmp_path / "Program Files"
    work.mkdir()
    monkeypatch.setattr(server, "is_windows", lambda: windows)
    monkeypatch.setattr(server, "is_macos", lambda: macos)
    assert server.Shell("Get-ChildItem", cwd=str(work)) == "ok"
    [(args, kwargs)] = popen
    assert args == argv
    assert kwargs["cwd"] == str(work)


def test_without_cwd_the_process_inherits_the_server_directory(monkeypatch, popen):
    monkeypatch.setattr(server, "is_windows", lambda: True)
    assert server.Shell("Get-Location") == "ok"
    [(args, kwargs)] = popen
    assert args == ["powershell", "-NoProfile", "-Command", POWERSHELL_UTF8_PREFIX + "\nGet-Location"]
    assert kwargs.get("cwd") is None


def test_output_that_is_not_utf8_is_still_the_commands_answer():
    # review round 3: bytes outside the expected encoding produced a 'utf-8 codec' Shell error on
    # Linux (on Windows the reader thread dropped stdout: "(no output)") and lost the output
    out = server.Shell("printf 'ok \\220 done'")
    assert out.startswith("ok ") and out.endswith(" done")


# The Windows output encoding (#32 rounds 4-5: its own console; #48: UTF-8, exact) is tested in
# tests/test_output_encoding.py.
