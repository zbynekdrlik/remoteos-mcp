"""Windows PowerShell 5.1 children in a UTF-8 console, with no code added to the script (#48 round 2).

Real processes only: the Shell tool and the shared runner start the console trampoline
(``python.exe -I -S -X utf8 -c <program> <cwd>``), which switches its own hidden console to UTF-8 and runs
``powershell -NoProfile -Command <script>`` with the script unchanged.

The runner is en-US with OEM code page 437, the same class as resolume-pp. cp437 has no Ž, ť, ň,
– or €, so any of them coming back exact proves the UTF-8 console. Round 1 put a UTF-8 prefix in
front of the script instead, and PowerShell printed that prefix in every ``Write-Error`` record
(live on resolume-pp) and refused a leading ``param()``.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path

import psutil
import pytest

import remoteos.__main__ as server
from remoteos.platform.command import powershell_argv, run_command

SLOVAK = "Žltý kôň ďakuje – €"

# Text of OUR machinery that must never appear in a reply: round 1's prefix, the trampoline program
# and its own messages. Compared case-insensitively (review round 1: "REMOTEOS" missed "remoteos:").
_OURS = ("outputencoding", "setconsole", "scriptblock", "remoteos", "kernel32", "subprocess", "65001", "-noprofile")


def _only_the_users_text(reply: str) -> None:
    for marker in _OURS:
        assert marker not in reply.lower(), f"{marker!r} leaked into the reply: {reply!r}"
    assert "\ufffd" not in reply


def test_the_runner_is_windows_powershell_5_1_on_oem_code_page_437():
    # the precondition of every exactness check here: a page without the Slovak letters
    assert ctypes.windll.kernel32.GetOEMCP() == 437
    assert server.Shell("$PSVersionTable.PSVersion.Major") == "5"
    assert powershell_argv("x")[0].lower().endswith("\\python.exe")  # the console python, never pythonw


@pytest.mark.parametrize("trailing", ["", "\\"], ids=["plain", "trailing-backslash"])
def test_shell_starts_powershell_in_its_cwd(tmp_path, trailing):
    # review round 6: the cwd travels as a trampoline argv element (two command-line hops) and
    # PowerShell must start there, spaces, diacritics and a trailing backslash included
    work = tmp_path / "Žltý kôň dir"
    work.mkdir()
    reply = server.Shell("(Get-Location).Path", cwd=str(work) + trailing)
    assert reply.rstrip("\\").endswith("\\Žltý kôň dir"), reply
    assert os.path.samefile(reply, work)


def test_powershell_output_is_exact():
    assert server.Shell(f'"{SLOVAK}"') == SLOVAK


def test_a_native_program_started_from_powershell_is_exact():
    assert server.Shell("cmd /c echo Žltý kôň") == "Žltý kôň"


def test_the_shared_runner_reads_powershell_exactly():
    # services, tasks, EventLog, launch_app and notifications go through run_command
    assert run_command(powershell_argv(f'"{SLOVAK}"')) == SLOVAK


def test_write_error_shows_only_the_users_text_and_exits_1():
    reply = server.Shell('Write-Error "chyba ťava"')
    assert reply.startswith('[STDERR] Write-Error "chyba ťava" : chyba ťava\n'), reply
    assert reply.endswith("\n[Exit code: 1]"), reply
    _only_the_users_text(reply)


def test_write_error_then_output_keeps_both_and_exits_0():
    # plain PowerShell semantics: the last statement succeeded
    reply = server.Shell('Write-Error "chyba ťava"; "po chybe"')
    # stdout ends with its own line break, so a blank line comes before [STDERR], as on every OS
    assert reply.startswith('po chybe\n\n[STDERR] Write-Error "chyba ťava"; "po chybe" : chyba ťava\n'), reply
    assert "[Exit code:" not in reply
    _only_the_users_text(reply)


def test_exit_3_is_the_exit_code():
    assert server.Shell('"pred"; exit 3') == "pred\n\n[Exit code: 3]"


def test_exit_minus_1_keeps_all_32_bits_of_the_exit_code():
    assert server.Shell("exit -1") == "[Exit code: 4294967295]"


def test_a_failing_native_program_last_exits_1_and_lastexitcode_is_kept():
    assert server.Shell("cmd /c exit 7") == "[Exit code: 1]"
    assert server.Shell('cmd /c exit 7; "code=$LASTEXITCODE"; exit $LASTEXITCODE') == "code=7\n\n[Exit code: 7]"


def test_param_at_the_start_of_the_script_works():
    assert server.Shell("param($a='x') $a") == "x"
    assert server.Shell('param($a = "ťava")\n"a=$a"') == "a=ťava"


def test_a_parse_error_is_reported_with_the_scripts_exact_text():
    # Windows PowerShell 5.1 prints a -Command parse error as the message, CategoryInfo and
    # FullyQualifiedErrorId, with no "At line / + <script>" excerpt (first CI run). The unexpected-
    # token message quotes the script's own token, so its Slovak letters prove the exact read.
    reply = server.Shell('"a" ťava kôň')
    assert reply.startswith("[STDERR] Unexpected token 'ťava' in expression or statement.\n"), reply
    assert "ParserError" in reply and reply.endswith("[Exit code: 1]"), reply
    _only_the_users_text(reply)


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return False


def _sleeps_after_writing_its_pid(pid_file: Path) -> str:
    """A PowerShell script that writes its own $PID, then hangs."""
    return f"$PID | Set-Content -LiteralPath '{pid_file}'; Start-Sleep -Seconds 120"


def test_a_timed_out_powershell_child_is_killed_with_its_tree(tmp_path):
    # Only the trampoline is the server's child. Review round 1: PowerShell ITSELF must be gone; the
    # bounded reap would otherwise still return in time and hide a missing tree kill.
    pid_file = tmp_path / "powershell.pid"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(powershell_argv(_sleeps_after_writing_its_pid(pid_file)), timeout=15)
    assert time.monotonic() - started < 60
    assert _gone_within(int(pid_file.read_text().strip()), 10), "PowerShell survived the timeout"


def test_a_timed_out_shell_command_is_an_error_and_powershell_is_killed(tmp_path):
    pid_file = tmp_path / "powershell.pid"
    started = time.monotonic()
    with pytest.raises(Exception, match=r"^Command timed out after 15s$"):
        server.Shell(_sleeps_after_writing_its_pid(pid_file), timeout=15)
    assert time.monotonic() - started < 60
    assert _gone_within(int(pid_file.read_text().strip()), 10), "PowerShell survived the timeout"
