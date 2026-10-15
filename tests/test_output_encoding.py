"""A Windows child's output reaches the client as exact Unicode (#48).

Live in the #32 verification, ``Shell('"Žltý kôň"')`` came back ``Zlty kôn`` on resolume-pp
(en_150, OEM cp437). PowerShell writes its output through the child console's OEM page, so the
characters that page cannot hold were best-fitted away before Python decoded anything.

The contract now:
- every PowerShell child runs ``powershell_argv(script)``: the UTF-8 prefix on its own line, then
  the script verbatim. It switches the child's OWN console (#32) to UTF-8;
- the child runs in binary mode, and ``decode_output`` reads each stream as strict UTF-8, falling
  back to the OEM page (``errors="replace"``) for bytes written before the prefix ran or by a
  native program started directly. The fallback is logged with the stream and the count, never
  the content;
- macOS/Linux are unchanged: the locale.

Only the OS boundary is faked:
- ``windows_child`` (conftest): a real Python child that writes the given bytes, started through
  the real ``subprocess``;
- Python's Windows-only ``oem`` codec, registered as a named code page.
Expected values are literals, never imported from the code under test.
"""

from __future__ import annotations

import ast
import locale
import logging
import re
import sys
from pathlib import Path

import pytest
from fake_console_child import POWERSHELL_UTF8_PREFIX, TEXT_MODE, oem_code_page, run_result

import remoteos.__main__ as server
from remoteos.platform.win import services as win_services

SLOVAK = "Žltý kôň ďakuje – €"
_CREATE_NO_WINDOW = 0x08000000


def _binary_in_its_own_console(kwargs: dict) -> None:
    """The child's bytes come back raw (decoded by us), and it has its own hidden console (#32 round 5)."""
    assert not any(kwargs.get(key) for key in TEXT_MODE), kwargs
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW


def _remoteos_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("remoteos")]


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


def test_windows_shell_returns_powershell_utf8_output_exactly(monkeypatch, windows_child):
    # the child writes UTF-8 (its console was switched by the prefix); read through the OEM page
    # (cp437 on resolume-pp) the same bytes came back as "┼╜lt├╜ k├┤┼ê …"
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")
    assert server.Shell(f'"{SLOVAK}"') == SLOVAK


def test_windows_shell_runs_the_command_after_the_utf8_prefix_in_its_own_console(monkeypatch, windows_child):
    # #32 round 5: its own hidden console (CREATE_NO_WINDOW), so switching THAT console to UTF-8
    # can never change the server's console or another command's
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = b"ok\r\n"
    assert server.Shell("Get-ChildItem") == "ok"
    [(argv, kwargs)] = windows_child.calls
    assert argv == ["powershell", "-NoProfile", "-Command", POWERSHELL_UTF8_PREFIX + "\nGet-ChildItem"]
    _binary_in_its_own_console(kwargs)


def test_windows_shell_translates_newlines_like_a_text_mode_run(monkeypatch, windows_child):
    # PowerShell writes \r\n; the reply had \n while the child ran in text mode, and still does
    monkeypatch.setattr(server, "is_windows", lambda: True)
    windows_child.stdout = b"a\r\nb\r\n"
    assert server.Shell("Get-Content x") == "a\nb"


def test_windows_shell_decodes_each_stream_on_its_own(monkeypatch, windows_child, caplog):
    # A script that does not parse never runs the prefix: PowerShell prints the parse error in the
    # console's OEM page (cp852 on a Slovak Windows). stderr falls back to OEM; stdout stays UTF-8.
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


def test_powershell_argv_puts_the_utf8_prefix_on_its_own_line_and_keeps_the_script_verbatim():
    from remoteos.platform.command import powershell_argv

    script = "Write-Output \"a; b\"\n$x = 'it''s'; Get-ChildItem 'C:\\Program Files'"
    assert powershell_argv(script) == ["powershell", "-NoProfile", "-Command", POWERSHELL_UTF8_PREFIX + "\n" + script]


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


def test_windows_native_program_output_in_the_oem_page_is_still_read_exactly(monkeypatch, windows_child):
    # tscon started directly (no PowerShell) writes the console's OEM page: it takes the OEM fallback
    from remoteos.platform.command import run_command

    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.oem = "cp852"
    windows_child.stderr = "Prístup odmietnutý.\r\n".encode("cp852")
    windows_child.returncode = 5
    with pytest.raises(RuntimeError, match=r"^tscon failed \(exit 5\): Prístup odmietnutý\.$"):
        run_command(["tscon", "2", "/dest:console"], check=True, label="tscon")


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
        (lambda: win_services.task_delete("t"), 'schtasks /Delete /TN "t" /F'),
        (
            lambda: win_services.event_log("System", 5),
            "Get-WinEvent -LogName 'System' -MaxEvents 5"
            " | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap",
        ),
    ],
    ids=["ServiceList", "TaskDelete", "EventLog"],
)
def test_windows_service_helpers_run_powershell_after_the_utf8_prefix(monkeypatch, windows_child, call, script):
    monkeypatch.setattr(sys, "platform", "win32")
    windows_child.stdout = (SLOVAK + "\r\n").encode("utf-8")  # e.g. a service with a Slovak display name
    assert call() == SLOVAK
    [(argv, kwargs)] = windows_child.calls
    assert argv == ["powershell", "-NoProfile", "-Command", POWERSHELL_UTF8_PREFIX + "\n" + script]
    _binary_in_its_own_console(kwargs)


def test_windows_launch_app_runs_powershell_after_the_utf8_prefix(win_session, monkeypatch):
    mod, _ = win_session
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return run_result(kwargs)

    monkeypatch.setattr("subprocess.run", _run)
    assert mod.launch_app("notepad", "C:\\Users\\Žltý kôň.txt") == "Launched notepad"
    assert calls == [
        [
            "powershell",
            "-NoProfile",
            "-Command",
            POWERSHELL_UTF8_PREFIX + '\nStart-Process "notepad" -ArgumentList "C:\\Users\\Žltý kôň.txt"',
        ]
    ]


def test_windows_notification_runs_powershell_after_the_utf8_prefix(win_session, monkeypatch):
    mod, _ = win_session
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return run_result(kwargs)

    monkeypatch.setattr("subprocess.run", _run)
    assert mod.show_notification("Titulok", "Žltý kôň") == "Notification shown"
    [argv] = calls
    assert argv[:3] == ["powershell", "-NoProfile", "-Command"]
    prefix, script = argv[3].split("\n", 1)
    assert prefix == POWERSHELL_UTF8_PREFIX
    assert "<text>Žltý kôň</text>" in script
