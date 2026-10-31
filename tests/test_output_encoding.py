"""A Windows child's output reaches the client as exact Unicode (#48).

Live in the #32 verification, ``Shell('"Žltý kôň"')`` came back ``Zlty kôn`` on resolume-pp
(en_150, OEM cp437). PowerShell writes its output through the child console's OEM page, so the
characters that page cannot hold were best-fitted away before Python decoded anything.

The contract now (#48 round 2):
- every PowerShell child runs ``powershell_argv(script)``: a console ``python.exe -I -S -X utf8 -c
  <program>`` (the trampoline) in the child's OWN console (#32) switches that console to UTF-8,
  then runs ``powershell -NoProfile -Command <script>`` with the script UNCHANGED. No PowerShell
  code is added, so error records, exit codes and ``param()`` are plain PowerShell's (round 1's
  prefix showed up in every ``Write-Error`` record);
- the child runs in binary mode, and ``decode_output`` reads each stream as strict UTF-8 (a
  leading BOM dropped), falling back line by line to the OEM page (``errors="replace"``) for a
  native program that writes its own page. The fallback is logged with the stream and the count,
  never the content;
- macOS/Linux are unchanged: the locale.

Only the OS boundary is faked:
- ``windows_child`` (conftest): a real Python child that writes the given bytes, started through
  the real ``subprocess``;
- Python's Windows-only ``oem`` codec, registered as a named code page;
- the trampoline program's own boundary: kernel32 and its ``subprocess.call``.
Real Windows PowerShell 5.1 runs the same contract in ``tests_windows`` (the windows-latest job).
Expected values are literals, never imported from the code under test.
"""

from __future__ import annotations

import ast
import locale
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fake_console_child import TEXT_MODE, assert_runs_powershell, oem_code_page, popen_answered_by, run_result
from tool_surface import server
from tool_test_support import tool_error

from remoteos.platform.win import services as win_services

SLOVAK = "Žltý kôň ďakuje – €"
_CREATE_NO_WINDOW = 0x08000000


def _binary_in_its_own_console(kwargs: dict) -> None:
    """The child's bytes come back raw (decoded by us), and it has its own hidden console (#32 round 5).

    Review round 1: its stdin is NUL, never the server's (the MCP stream under the stdio transport).
    """
    assert not any(kwargs.get(key) for key in TEXT_MODE), kwargs
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    assert kwargs["stdin"] == subprocess.DEVNULL


def _remoteos_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("remoteos")]


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


def test_windows_shell_returns_powershell_utf8_output_exactly(monkeypatch, windows_child):
    # the child writes UTF-8 (its console is UTF-8 before PowerShell starts); read through the OEM page
    # (cp437 on resolume-pp) the same bytes came back as "┼╜lt├╜ k├┤┼ê …"
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")
    assert server.Shell(f'"{SLOVAK}"') == SLOVAK


def test_windows_shell_runs_the_command_unchanged_under_the_trampoline_in_its_own_console(
    monkeypatch, windows_child, windows_python
):
    # #32 round 5: its own hidden console (CREATE_NO_WINDOW), so switching THAT console to UTF-8
    # can never change the server's console or another command's
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = b"ok\r\n"
    assert server.Shell("Get-ChildItem") == "ok"
    [(argv, kwargs)] = windows_child.calls
    assert_runs_powershell(argv, windows_python, "Get-ChildItem")
    _binary_in_its_own_console(kwargs)


def test_windows_shell_translates_newlines_like_a_text_mode_run(monkeypatch, windows_child):
    # PowerShell writes \r\n; the reply had \n while the child ran in text mode, and still does
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = b"a\r\nb\r\n"
    assert server.Shell("Get-Content x") == "a\nb"


_COULD_NOT_START = "remoteos: could not start powershell: [WinError 2] The system cannot find the file specified"


def test_powershell_that_could_not_be_started_is_a_tool_error_not_a_reply(monkeypatch, windows_child):
    # review round 3: a command that never ran is a failure (tool-errors.md), never "[Exit code: 1]",
    # which PowerShell's own failures use too. The trampoline says so in one fixed line.
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stderr = (_COULD_NOT_START + "\r\n").encode("utf-8")
    windows_child.returncode = 1
    assert tool_error(lambda: server.Shell("Get-Date")) == f"Shell error: {_COULD_NOT_START}"


def test_a_start_failure_after_a_refused_page_is_still_an_error(monkeypatch, windows_child):
    # review round 4: the trampoline may first say a console page was refused, then that it could not
    # start PowerShell; the second line still makes it a failure
    monkeypatch.setattr(server, "is_windows", lambda: True)
    refused = "remoteos: the console output stays in the OEM code page (error 87)"
    windows_child.stderr = f"{refused}\r\n{_COULD_NOT_START}\r\n".encode("utf-8")
    windows_child.returncode = 1
    assert tool_error(lambda: server.Shell("Get-Date")) == f"Shell error: {refused}\n{_COULD_NOT_START}"


def test_a_trampolined_exit_1_with_nothing_on_stderr_is_the_commands_answer(monkeypatch, windows_child):
    # review round 5: e.g. `cmd /c exit 7` as the last statement; an empty stderr is no start failure
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.returncode = 1
    assert server.Shell("cmd /c exit 7") == "[Exit code: 1]"


def test_the_start_failure_line_counts_only_as_the_first_line(monkeypatch, windows_child):
    # review round 6: PowerShell's own error first, then the same text: still the command's answer
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stderr = f"real error\r\n{_COULD_NOT_START}\r\n".encode("utf-8")
    windows_child.returncode = 1
    assert server.Shell("Get-Date") == f"[STDERR] real error\n{_COULD_NOT_START}\n\n[Exit code: 1]"


def test_only_the_trampolines_own_exit_1_is_a_start_failure(monkeypatch, windows_child):
    # review round 4: the exit code and the trampolined argv are part of the rule (both guards were
    # surviving mutants). PowerShell exiting 2 with that text is its own answer.
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stderr = (_COULD_NOT_START + "\r\n").encode("utf-8")
    windows_child.returncode = 2
    assert server.Shell("Get-Date") == f"[STDERR] {_COULD_NOT_START}\n\n[Exit code: 2]"


def test_a_posix_command_that_writes_the_start_failure_line_is_its_own_answer():
    # a real bash: no trampoline, so the same text and exit 1 are just the command's output
    reply = server.Shell('echo "remoteos: could not start x" >&2; exit 1')
    assert reply == "[STDERR] remoteos: could not start x\n\n[Exit code: 1]"


def test_powershell_that_could_not_be_started_fails_a_listing_too(monkeypatch, windows_child):
    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.stderr = (_COULD_NOT_START + "\r\n").encode("utf-8")
    windows_child.returncode = 1
    with pytest.raises(RuntimeError, match=r"^remoteos: could not start powershell: \[WinError 2\]"):
        win_services.service_list()  # check=False: a listing's own exit code is its answer, but this never ran
    # PowerShell's own failure with exit 1 stays the listing's answer
    windows_child.stderr = b"Get-Service : Cannot find any service\r\n"
    assert win_services.service_list() == "[STDERR] Get-Service : Cannot find any service\n\n[Exit code: 1]"


def test_windows_shell_decodes_each_stream_on_its_own(monkeypatch, windows_child, caplog):
    # e.g. a native program that writes its error in the OEM page whatever the console's page
    # (cp852 on a Slovak Windows): stderr falls back to OEM; stdout stays UTF-8
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.oem = "cp852"
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")
    windows_child.stderr = "Chýba ukončovací znak.\r\n".encode("cp852")
    windows_child.returncode = 1
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        out = server.Shell("x")
    assert out == f"{SLOVAK}\n\n[STDERR] Chýba ukončovací znak.\n\n[Exit code: 1]"
    # review round 2: the log names the stream that fell back (a swapped label survived)
    [record] = _remoteos_records(caplog)
    assert record.getMessage().startswith("stderr: 1 line(s) not UTF-8")


def test_windows_shell_keeps_powershell_utf8_lines_exact_next_to_a_native_oem_line(monkeypatch, windows_child):
    # review round 1: one line a native program wrote in the OEM page falls back on its own
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.oem = "cp852"
    windows_child.stdout = (
        (SLOVAK + "\r\n").encode("utf-8") + "Prístup odmietnutý.\r\n".encode("cp852") + "Hotovo\r\n".encode("utf-8")
    )
    assert server.Shell("x") == f"{SLOVAK}\nPrístup odmietnutý.\nHotovo"


def test_linux_shell_returns_utf8_output_exactly_through_real_bash():
    # macOS/Linux are unchanged (the locale, UTF-8 here); this locks it with a real /bin/bash
    assert server.Shell('printf "Žltý kôň"') == "Žltý kôň"


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def test_powershell_argv_keeps_the_script_verbatim_under_the_console_trampoline(windows_python):
    from remoteos.platform.command import powershell_argv

    script = "param($a = 'x')\nWrite-Output \"a; b\"\n$x = 'it''s'; Get-ChildItem 'C:\\Program Files'"
    argv = powershell_argv(script)
    assert_runs_powershell(argv, windows_python, script)
    assert powershell_argv("exit 3")[6] == argv[6]  # one fixed program: no user data in it


def test_the_trampoline_is_the_console_python_even_when_the_server_runs_as_pythonw(windows_python, monkeypatch):
    # pythonw.exe is a GUI-subsystem program: CREATE_NO_WINDOW gives it no console to switch
    from remoteos.platform.command import powershell_argv

    monkeypatch.setattr(sys, "executable", str(windows_python.with_name("pythonw.exe")))
    assert powershell_argv("x")[0] == str(windows_python)


def test_a_missing_console_python_is_a_loud_error(windows_python, monkeypatch):
    from remoteos.platform.command import powershell_argv

    windows_python.unlink()  # only pythonw.exe is left
    monkeypatch.setattr(sys, "executable", str(windows_python.with_name("pythonw.exe")))
    with pytest.raises(RuntimeError, match=r"^no python\.exe next to .*pythonw\.exe"):
        powershell_argv("x")


# ---------------------------------------------------------------------------
# The console trampoline program (#48 round 2), run here against its own boundary: kernel32 and the
# subprocess.call that starts PowerShell. Real Windows runs it in tests_windows.
# ---------------------------------------------------------------------------


class _Kernel32:
    """kernel32's code-page calls, recorded; ``refuse`` names the calls that fail (return 0)."""

    def __init__(self, events: list, refuse: tuple) -> None:
        self.events, self.refuse = events, refuse

    def SetConsoleOutputCP(self, page):
        self.events.append(("SetConsoleOutputCP", page))
        return 0 if "SetConsoleOutputCP" in self.refuse else 1

    def SetConsoleCP(self, page):
        self.events.append(("SetConsoleCP", page))
        return 0 if "SetConsoleCP" in self.refuse else 1


def _run_trampoline(monkeypatch, windows_python, *, cwd=None, child_exit=0, refuse=(), start_error=None):
    """Run the trampoline program of ``powershell_argv`` as python.exe would.

    Returns ``(events, exit code, argv)``. ``child_exit`` is PowerShell's exit code; ``refuse`` the
    kernel32 calls that fail; ``start_error`` an ``OSError`` that starting PowerShell raises.
    """
    import types

    from remoteos.platform.command import powershell_argv

    argv = powershell_argv("Write-Error 'chyba ťava'; exit 3", cwd=cwd)
    events: list = []
    fake_ctypes = types.ModuleType("ctypes")

    def _windll(name, use_last_error=False):
        events.append(("WinDLL", name, use_last_error))
        return _Kernel32(events, refuse)

    fake_ctypes.WinDLL = _windll
    fake_ctypes.get_last_error = lambda: 87
    fake_subprocess = types.ModuleType("subprocess")

    def _call(args, **kwargs):
        events.append(("call", list(args), kwargs))
        if start_error:
            raise start_error
        return child_exit

    fake_subprocess.call = _call
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setitem(sys.modules, "subprocess", fake_subprocess)
    monkeypatch.setattr(sys, "argv", ["-c", *argv[7:]])  # what python.exe -c <program> <args…> sets
    with pytest.raises(SystemExit) as done:
        exec(argv[6], {"__name__": "__main__"})
    return events, done.value.code, argv


_POWERSHELL = ["powershell", "-NoProfile", "-Command", "Write-Error 'chyba ťava'; exit 3"]


def test_the_trampoline_switches_its_own_console_to_utf8_then_runs_powershell_unchanged(monkeypatch, windows_python):
    events, code, _ = _run_trampoline(monkeypatch, windows_python)
    assert events == [
        ("WinDLL", "kernel32", True),
        ("SetConsoleOutputCP", 65001),
        ("SetConsoleCP", 65001),
        # the same console (no creationflags), the trampoline's own stdout/stderr handed down, and
        # no working directory of its own: PowerShell starts in the server's
        ("call", _POWERSHELL, {"cwd": None, "stdout": 1, "stderr": 2}),
    ]
    assert code == 0


def test_the_trampoline_starts_powershell_in_the_users_cwd_but_finds_it_from_the_servers(
    monkeypatch, windows_python
):
    # review round 1: CreateProcess looks for "powershell" in the PARENT's current directory first.
    # The trampoline runs in the server's (run_child gets no cwd), so a powershell.exe planted in the
    # user's working directory is never found; PowerShell itself starts in that directory.
    events, _, argv = _run_trampoline(monkeypatch, windows_python, cwd="C:\\Users\\Žltý kôň\\repo")
    assert argv[7] == "C:\\Users\\Žltý kôň\\repo"
    assert events[-1] == ("call", _POWERSHELL, {"cwd": "C:\\Users\\Žltý kôň\\repo", "stdout": 1, "stderr": 2})


@pytest.mark.parametrize(
    ("child_exit", "process_exit"),
    [(3, 3), (1, 1), (0x7FFFFFFF, 0x7FFFFFFF), (0x80000000, -0x80000000), (0xC000013A, -1073741510), (0xFFFFFFFF, -1)],
    ids=["exit-3", "exit-1", "largest-positive", "first-negative", "ctrl-c-status", "exit-minus-1"],
)
def test_the_trampoline_exits_with_powershells_exact_exit_code(monkeypatch, windows_python, child_exit, process_exit):
    # Windows exit codes are 32-bit unsigned; sys.exit takes a C long, so a code >= 2**31 is handed
    # over as the same 32 bits, signed (sys.exit(-1) ends the process with 0xFFFFFFFF). Review round 1:
    # both sides of the 2**31 boundary are pinned.
    _, code, _ = _run_trampoline(monkeypatch, windows_python, child_exit=child_exit)
    assert code == process_exit
    assert code & 0xFFFFFFFF == child_exit


@pytest.mark.parametrize(
    ("refuse", "said"),
    [
        (("SetConsoleOutputCP",), "remoteos: the console output stays in the OEM code page (error 87)\n"),
        (("SetConsoleCP",), "remoteos: the console input stays in the OEM code page (error 87)\n"),
    ],
    ids=["output-page", "input-page"],
)
def test_a_console_that_refuses_utf8_still_runs_the_command_and_says_which_page(
    monkeypatch, windows_python, capsys, refuse, said
):
    # never seen (the child always has its own console); refused output takes the OEM fallback.
    # Review round 1: each page has its own message, so an input refusal never claims the output
    events, code, _ = _run_trampoline(monkeypatch, windows_python, child_exit=3, refuse=refuse)
    assert [e[0] for e in events] == ["WinDLL", "SetConsoleOutputCP", "SetConsoleCP", "call"]
    assert code == 3
    assert capsys.readouterr().err == said


def test_powershell_that_cannot_be_started_is_one_line_never_a_traceback(monkeypatch, windows_python, capsys):
    # review round 1: no powershell.exe, or a working directory that vanished after Shell checked it
    start_error = FileNotFoundError(2, "The system cannot find the file specified")
    _, code, _ = _run_trampoline(monkeypatch, windows_python, start_error=start_error)
    assert code == 1
    assert capsys.readouterr().err == (
        "remoteos: could not start powershell: [Errno 2] The system cannot find the file specified\n"
    )


# review round 2: also a full path, e.g. C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe
_POWERSHELL_PROGRAM = re.compile(r"(?i)(^|[\\/])(powershell|pwsh)(\.exe)?$")
_POWERSHELL_COMMAND_LINE = re.compile(r"(?i)^(\S*[\\/])?(powershell|pwsh)(\.exe)?\s+-")


def _starts_powershell(node: ast.AST) -> bool:
    """An argv list/tuple whose program is PowerShell, or a command-line string that starts it."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and bool(
            _POWERSHELL_PROGRAM.search(first.value)
        )
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and bool(
        _POWERSHELL_COMMAND_LINE.match(node.value)
    )


def test_every_powershell_child_is_started_by_the_one_builder():
    # one builder instead of four hand-built argv lists (Shell, services, launch_app/notification, OCR).
    # Review round 1: an AST scan, so ['powershell', …], ("PowerShell.exe", …), ["pwsh", …] or a
    # "powershell -Command …" string outside platform/command.py is caught whatever the quotes or
    # case; a label such as label="PowerShell" is not a child.
    src = Path(server.__file__).parent
    hand_built = [
        f"{path.relative_to(src).as_posix()}:{node.lineno}"
        for path in sorted(src.rglob("*.py"))
        if path.relative_to(src).as_posix() != "platform/command.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if _starts_powershell(node)
    ]
    assert hand_built == []


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        ("argv = ['powershell', '-Command', 'x']", True),
        ('argv = ("PowerShell.exe", "-NoProfile")', True),
        ('argv = ["pwsh", "-c", "x"]', True),
        ('cmd = "powershell -NoProfile -Command x"', True),
        ('argv = [r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-Command", "x"]', True),
        ('cmd = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -Command x"', True),
        ('run_command(argv, label="PowerShell")', False),
        ('argv = powershell_argv("x")', False),
    ],
)
def test_the_one_builder_guard_catches_a_hand_built_powershell_child(source, caught):
    # the guard itself can fail: each hand-built shape is caught, a label or the builder is not
    assert any(_starts_powershell(node) for node in ast.walk(ast.parse(source))) is caught


# ---------------------------------------------------------------------------
# decode_output
# ---------------------------------------------------------------------------


def test_decode_output_drops_a_leading_utf8_bom(caplog):
    # none was seen live (resolume-pp wrote C5 BD E2 82 AC 0D 0A for "Ž€"); dropped defensively,
    # so a BOM never reaches the client as U+FEFF and never sends the stream to the OEM fallback
    from remoteos.platform.command import decode_output

    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        assert decode_output(b"\xef\xbb\xbf" + "Žltý\r\n".encode("utf-8"), windows=True) == "Žltý\n"
    assert _remoteos_records(caplog) == []
    assert decode_output(b"a\xef\xbb\xbf", windows=True) == "a\ufeff"  # only a LEADING one


def test_decode_output_reads_utf8_exactly(caplog):
    from remoteos.platform.command import decode_output

    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        assert decode_output(SLOVAK.encode("utf-8"), windows=True) == SLOVAK
    assert _remoteos_records(caplog) == []  # no fallback, nothing to log


def test_decode_output_falls_back_to_the_oem_page_for_bytes_that_are_not_utf8(caplog):
    from remoteos.platform.command import decode_output

    with oem_code_page("cp852"), caplog.at_level(logging.DEBUG, logger="remoteos"):
        assert decode_output("Čaká".encode("cp852"), windows=True, stream="stderr") == "Čaká"
    [record] = _remoteos_records(caplog)
    message = record.getMessage()
    assert record.levelno == logging.INFO  # nothing was lost
    assert "stderr: 1 line(s) not UTF-8" in message and "; 0 undecodable" in message
    assert "Čaká" not in caplog.text and "aká" not in caplog.text  # never the content


def test_decode_output_keeps_undecodable_bytes_as_replacement_characters_and_logs_the_count(caplog):
    # cp932 (a double-byte OEM page): a lead byte with no trail byte cannot be decoded
    from remoteos.platform.command import decode_output

    with oem_code_page("cp932"), caplog.at_level(logging.DEBUG, logger="remoteos"):
        out = decode_output(b"Zz-marker \x81", windows=True, stream="stdout")
    assert out == "Zz-marker \ufffd"  # the output is never lost
    [record] = _remoteos_records(caplog)
    message = record.getMessage()
    assert record.levelno == logging.WARNING  # a character was lost
    assert "stdout: 1 line(s) not UTF-8" in message and "; 1 undecodable" in message
    assert "Zz-marker" not in caplog.text


def test_decode_output_counts_every_fallback_line_and_every_replaced_character(caplog):
    # round 2 🔵: with one fallback line per test, `fallback = 1` and `replaced = <this line's count>`
    # survived as mutants; two cp932 lines, each with one undecodable lead byte, pin both totals
    from remoteos.platform.command import decode_output

    with oem_code_page("cp932"), caplog.at_level(logging.DEBUG, logger="remoteos"):
        out = decode_output(b"a \x81\r\nb \x81", windows=True, stream="stdout")
    assert out == "a �\nb �"
    [record] = _remoteos_records(caplog)
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "stdout: 2 line(s) not UTF-8" in message and "; 2 undecodable" in message


def test_decode_output_falls_back_line_by_line_so_utf8_lines_stay_exact(caplog):
    # review round 1: a native program started from PowerShell may write its own page into the same
    # pipe; that one line must not turn PowerShell's UTF-8 lines around it into mojibake
    from remoteos.platform.command import decode_output

    raw = (
        (SLOVAK + "\r\n").encode("utf-8")
        + "Prístup odmietnutý.\r\n".encode("cp852")
        + "Hotovo – €\r\n".encode("utf-8")
    )
    with oem_code_page("cp852"), caplog.at_level(logging.DEBUG, logger="remoteos"):
        out = decode_output(raw, windows=True, stream="stdout")
    assert out == f"{SLOVAK}\nPrístup odmietnutý.\nHotovo – €\n"
    [record] = _remoteos_records(caplog)
    message = record.getMessage()
    assert record.levelno == logging.INFO
    assert "stdout: 1 line(s) not UTF-8" in message and "; 0 undecodable" in message
    assert "odmietnut" not in caplog.text and "Hotovo" not in caplog.text


def test_decode_output_fallback_breaks_lines_at_a_lone_carriage_return_too():
    # review round 2: a text-mode run (and decode_output's own result) treats a lone \r as a line
    # break, so the per-line fallback must split there too; read together, the UTF-8 line went OEM
    from remoteos.platform.command import decode_output

    raw = "Žltý".encode("utf-8") + b"\r" + "čaká".encode("cp852") + b"\r\n" + "kôň".encode("utf-8")
    with oem_code_page("cp852"):
        assert decode_output(raw, windows=True) == "Žltý\nčaká\nkôň"


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_decode_output_translates_newlines_like_a_text_mode_run(windows):
    from remoteos.platform.command import decode_output

    assert decode_output(b"a\r\nb\rc\n", windows=windows) == "a\nb\nc\n"
    assert decode_output(b"", windows=windows) == ""


def test_decode_output_off_windows_reads_the_locale_and_never_loses_output():
    # unchanged on macOS/Linux: the locale's encoding (UTF-8 here), an undecodable byte replaced
    from remoteos.platform.command import decode_output

    assert decode_output("Žltý kôň".encode("utf-8") + b" \x90", windows=False) == "Žltý kôň \ufffd"


def test_decode_output_off_windows_follows_the_locale_not_a_fixed_utf8(monkeypatch):
    # review round 1: on a UTF-8 CI a hard-coded "utf-8" would pass the test above; the C library's
    # locale (the OS boundary) is faked as latin-1, the page a text-mode run would have used
    from remoteos.platform.command import decode_output

    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "latin-1")
    assert decode_output(b"caf\xe9", windows=False) == "café"


# ---------------------------------------------------------------------------
# run_command and its PowerShell users (services, tasks, EventLog, launch_app, notifications)
# ---------------------------------------------------------------------------


def test_windows_run_command_reads_utf8_exactly_in_its_own_console(monkeypatch, windows_child):
    from remoteos.platform.command import run_command

    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")
    assert run_command(["schtasks", "/Query"]) == SLOVAK
    [(argv, kwargs)] = windows_child.calls
    assert argv == ["schtasks", "/Query"]
    _binary_in_its_own_console(kwargs)


def test_an_unlabeled_powershell_failure_names_powershell_not_the_trampoline(
    monkeypatch, windows_child, windows_python
):
    # review round 2: the label fallback is the command as the user knows it, never "python.exe -I"
    from remoteos.platform.command import powershell_argv, run_command

    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.stderr = b"Get-Thing: not found\r\n"
    windows_child.returncode = 1
    with pytest.raises(RuntimeError, match=r"^powershell -NoProfile failed \(exit 1\): Get-Thing: not found$"):
        run_command(powershell_argv("Get-Thing"), check=True)


def test_windows_native_program_output_in_the_oem_page_is_still_read_exactly(monkeypatch, windows_child, caplog):
    # tscon started directly (no PowerShell) writes the console's OEM page: it takes the OEM fallback
    from remoteos.platform.command import run_command

    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.oem = "cp852"
    windows_child.stderr = "Prístup odmietnutý.\r\n".encode("cp852")
    windows_child.returncode = 5
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        with pytest.raises(RuntimeError, match=r"^tscon failed \(exit 5\): Prístup odmietnutý\.$"):
            run_command(["tscon", "2", "/dest:console"], check=True, label="tscon")
    # review round 3: run_child names the stream that fell back (swapped labels survived)
    [record] = _remoteos_records(caplog)
    assert record.getMessage().startswith("stderr: 1 line(s) not UTF-8")


def test_posix_run_command_reads_the_locale_exactly():
    # unchanged on macOS/Linux, with a real child
    from remoteos.platform.command import run_command

    assert run_command([sys.executable, "-c", "import sys; sys.stdout.buffer.write('Žltý kôň\\n'.encode())"]) == (
        "Žltý kôň"
    )


@pytest.mark.parametrize(
    ("call", "script"),
    [
        (lambda: win_services.service_list(), "Get-Service | Format-Table Name, DisplayName, Status -AutoSize"),
        (lambda: win_services.task_delete("t"), "schtasks /Delete /TN 't' /F"),  # #39: a literal
        (
            lambda: win_services.event_log("System", 5),
            "Get-WinEvent -LogName 'System' -MaxEvents 5"
            " | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap",
        ),
    ],
    ids=["ServiceList", "TaskDelete", "EventLog"],
)
def test_windows_service_helpers_run_powershell_under_the_trampoline(
    monkeypatch, windows_child, windows_python, call, script
):
    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")  # e.g. a service with a Slovak display name
    assert call() == SLOVAK
    [(argv, kwargs)] = windows_child.calls
    assert_runs_powershell(argv, windows_python, script)
    _binary_in_its_own_console(kwargs)


def test_windows_launch_app_runs_powershell_under_the_trampoline(win_session, windows_python, monkeypatch):
    mod, _ = win_session
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return run_result(kwargs)

    monkeypatch.setattr("subprocess.Popen", popen_answered_by(_run))
    assert mod.launch_app("notepad", "C:\\Users\\Žltý kôň.txt") == "Launched notepad"
    [argv] = calls
    # #39: the name and the arguments are single-quoted literals, never expandable strings
    assert_runs_powershell(argv, windows_python, "Start-Process 'notepad' -ArgumentList 'C:\\Users\\Žltý kôň.txt'")


def test_windows_notification_runs_powershell_under_the_trampoline(win_session, windows_python, monkeypatch):
    mod, _ = win_session
    calls: list[tuple[list[str], dict]] = []

    def _run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return run_result(kwargs)

    monkeypatch.setattr("subprocess.Popen", popen_answered_by(_run))
    assert mod.show_notification("Titulok", "Žltý kôň") == "Notification shown"
    [(argv, kwargs)] = calls
    assert_runs_powershell(argv, windows_python, argv[-1])
    assert argv[-1].startswith("\n[Windows.UI.Notifications.ToastNotificationManager")  # the script as written
    # #39: the text reaches PowerShell in the environment, exact, never in the script
    assert "Žltý kôň" not in argv[-1]
    assert kwargs["env"]["REMOTEOS_TOAST_MESSAGE"] == "Žltý kôň"
