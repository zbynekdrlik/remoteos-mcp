"""Caller values reach PowerShell and AppleScript as literal text, never as code (#39).

Before #39 the Windows helpers spliced values into PowerShell source inside DOUBLE quotes
(``Start-Service -Name "{name}"``, ``Start-Process "{name}"``) or an EXPANDABLE here-string (the
toast), so ``$(Set-Content x pwned)`` ran as PowerShell. Notification is a tier-1 tool, Shell tier 3:
a way around the tier gate. The contract now:

- a value in PowerShell source is ``ps_literal(value)``: a single-quoted literal, every quote
  doubled, the typographic single quotes PowerShell treats as ``'`` included;
- the toast title and message never enter the script: they travel in ``REMOTEOS_TOAST_TITLE`` /
  ``REMOTEOS_TOAST_MESSAGE`` and the script reads ``$env:``;
- an AppleScript string escapes ``\\`` first, then ``"``.

Only the OS boundary is faked (the child process, osascript). Real Windows PowerShell 5.1 runs the
same payloads in ``tests_windows/test_injection_powershell.py``. Expected values are literals.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
import types

import pytest
from fake_console_child import assert_runs_powershell, popen_answered_by, run_result
from PIL import Image
from tool_test_support import purge_module

from remoteos import ocr
from remoteos.platform.win import services as win_services

PAYLOAD = "$(Set-Content x pwned)"


# ---------------------------------------------------------------------------
# ps_literal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        ("Spooler", "'Spooler'"),
        ("", "''"),
        ("$(Set-Content x pwned)", "'$(Set-Content x pwned)'"),
        ("'; calc; '", "'''; calc; '''"),
        ('"; calc; "', "'\"; calc; \"'"),
        ("`n $env:PATH @(1)", "'`n $env:PATH @(1)'"),
        # U+2018 U+2019 U+201A U+201B end a PowerShell single-quoted string like ' does
        ("\u2018; calc; \u2019", "'\u2018\u2018; calc; \u2019\u2019'"),
        ("a\u201ab\u201bc", "'a\u201a\u201ab\u201b\u201bc'"),
        ("C:\\Program Files\\Žltý kôň.exe", "'C:\\Program Files\\Žltý kôň.exe'"),
    ],
    ids=["plain", "empty", "subexpression", "single-quote", "double-quote", "specials", "smart-quotes",
         "low-smart-quotes", "path"],
)
def test_ps_literal_is_a_single_quoted_literal_with_every_quote_doubled(value, literal):
    from remoteos.platform.win.psquote import ps_literal

    assert ps_literal(value) == literal


# ---------------------------------------------------------------------------
# Windows services, scheduled tasks, event log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "script"),
    [
        (
            lambda: win_services.service_start(PAYLOAD),
            "Start-Service -Name '$(Set-Content x pwned)' -PassThru | Format-Table Name, Status -AutoSize",
        ),
        (
            lambda: win_services.service_stop("'; calc; '"),
            "Stop-Service -Name '''; calc; ''' -Force -PassThru | Format-Table Name, Status -AutoSize",
        ),
        (
            lambda: win_services.service_list("x'; calc; '"),
            "Get-Service | Where-Object { $_.DisplayName -like '*x''; calc; ''*'"
            " -or $_.Name -like '*x''; calc; ''*' } | Format-Table Name, DisplayName, Status -AutoSize",
        ),
        (
            lambda: win_services.task_list("a'b $(x)"),
            "Get-ScheduledTask | Where-Object { $_.TaskName -like '*a''b $(x)*' }"
            " | Format-Table TaskName, State, TaskPath -AutoSize",
        ),
        (
            lambda: win_services.task_create(PAYLOAD, 'notepad "C:\\a b.txt"', "DAILY"),
            "schtasks /Create /TN '$(Set-Content x pwned)' /TR 'notepad \"C:\\a b.txt\"' /SC 'DAILY' /F",
        ),
        (
            lambda: win_services.task_delete('"; calc; "'),
            "schtasks /Delete /TN '\"; calc; \"' /F",
        ),
        (
            lambda: win_services.event_log("System'; calc; '", 5),
            "Get-WinEvent -LogName 'System''; calc; ''' -MaxEvents 5"
            " | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap",
        ),
        (
            lambda: win_services.event_log("a'b $(x)", 3, "error"),
            "Get-WinEvent -FilterHashtable @{LogName='a''b $(x)';Level=2} -MaxEvents 3"
            " | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap",
        ),
    ],
    ids=["ServiceStart", "ServiceStop", "ServiceList", "TaskList", "TaskCreate", "TaskDelete", "EventLog",
         "EventLog-level"],
)
def test_windows_service_helpers_put_caller_values_in_single_quoted_literals(
    monkeypatch, windows_child, windows_python, call, script
):
    monkeypatch.setattr(sys, "platform", "win32")
    call()
    [(argv, _)] = windows_child.calls
    assert_runs_powershell(argv, windows_python, script)


def test_event_log_count_is_a_number_in_the_script(monkeypatch, windows_child, windows_python):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError):
        win_services.event_log("System", "5; Set-Content x pwned")
    assert windows_child.calls == []


# ---------------------------------------------------------------------------
# Windows App (Start-Process) and toast
# ---------------------------------------------------------------------------


def _recording_popen(monkeypatch) -> list[tuple[list[str], dict]]:
    calls: list[tuple[list[str], dict]] = []

    def _run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return run_result(kwargs)

    monkeypatch.setattr("subprocess.Popen", popen_answered_by(_run))
    return calls


def test_windows_launch_app_puts_name_and_args_in_single_quoted_literals(win_session, windows_python, monkeypatch):
    mod, _ = win_session
    calls = _recording_popen(monkeypatch)
    assert mod.launch_app(PAYLOAD, "'; calc; '") == f"Launched {PAYLOAD}"
    [(argv, _)] = calls
    assert_runs_powershell(
        argv, windows_python, "Start-Process '$(Set-Content x pwned)' -ArgumentList '''; calc; '''"
    )


def test_windows_toast_text_travels_in_the_environment_never_in_the_script(win_session, windows_python, monkeypatch):
    mod, _ = win_session
    monkeypatch.setenv("REMOTEOS_TEST_KEEP", "kept")
    calls = _recording_popen(monkeypatch)
    title, message = "Titulok $(Set-Content x pwned)", "Riadok 1\n'; calc; '\n<b>&amp;</b> ’"
    assert mod.show_notification(title, message) == "Notification shown"
    [(argv, kwargs)] = calls
    script = argv[-1]
    assert_runs_powershell(argv, windows_python, script)
    assert "Set-Content" not in script and "calc" not in script and "Titulok" not in script
    assert "$env:REMOTEOS_TOAST_TITLE" in script and "$env:REMOTEOS_TOAST_MESSAGE" in script
    assert '@"' not in script  # no expandable here-string left
    env = kwargs["env"]
    assert env["REMOTEOS_TOAST_TITLE"] == title
    assert env["REMOTEOS_TOAST_MESSAGE"] == message
    assert env["REMOTEOS_TEST_KEEP"] == "kept"  # the server's own environment, plus the two variables


def test_windows_toast_text_is_set_as_xml_text_nodes(win_session, windows_python, monkeypatch):
    # an XML metacharacter in the text used to break LoadXml; a text node takes any character
    mod, _ = win_session
    calls = _recording_popen(monkeypatch)
    mod.show_notification("a", "b")
    [(argv, _)] = calls
    script = argv[-1]
    assert "CreateTextNode($env:REMOTEOS_TOAST_TITLE)" in script
    assert "CreateTextNode($env:REMOTEOS_TOAST_MESSAGE)" in script


# ---------------------------------------------------------------------------
# Windows OCR: the temp file path
# ---------------------------------------------------------------------------


def test_windows_ocr_path_is_a_single_quoted_literal(monkeypatch, windows_python, tmp_path):
    folder = tmp_path / "it's $(x)"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    monkeypatch.setattr(ocr, "ImageGrab", types.SimpleNamespace(grab=lambda bbox=None: Image.new("RGB", (8, 8))))
    calls = _recording_popen(monkeypatch)
    assert ocr.ocr_windows_builtin() == ""
    [(argv, _)] = calls
    [line] = [x for x in argv[-1].splitlines() if x.startswith("$path = ")]
    assert line.startswith(f"$path = '{tmp_path}/it''s $(x)/")
    assert line.endswith(".png'")


# ---------------------------------------------------------------------------
# macOS AppleScript strings
# ---------------------------------------------------------------------------


@pytest.fixture
def mac_session(monkeypatch):
    """The macOS provider imported against a fake Quartz/pyautogui; yields ``(provider, None)``."""
    monkeypatch.setitem(sys.modules, "Quartz", types.ModuleType("Quartz"))
    monkeypatch.setitem(sys.modules, "pyautogui", types.ModuleType("pyautogui"))
    purge_module("remoteos.platform.mac.desktop")
    yield importlib.import_module("remoteos.platform.mac.desktop"), None
    purge_module("remoteos.platform.mac.desktop")


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        ("Safari", '"Safari"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("a\\", '"a\\\\"'),
        ('x\\" & (do shell script "touch /tmp/p") & "', '"x\\\\\\" & (do shell script \\"touch /tmp/p\\") & \\""'),
    ],
    ids=["plain", "quote", "trailing-backslash", "backslash-before-quote"],
)
def test_applescript_literal_escapes_the_backslash_first_then_the_quote(mac_session, value, literal):
    mod, _ = mac_session
    assert mod.applescript_literal(value) == literal


def _osascript_recorder(mod, monkeypatch) -> list[str]:
    scripts: list[str] = []

    def _run(cmd, **kwargs):
        assert cmd[:2] == ["osascript", "-e"]
        scripts.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    return scripts


def test_mac_notification_escapes_a_backslash_before_a_quote(mac_session, monkeypatch):
    # before #39: '\"' became '\\"' -- the escaped backslash ended the string and the rest ran
    mod, _ = mac_session
    scripts = _osascript_recorder(mod, monkeypatch)
    mod.show_notification('T\\" & (do shell script "id") & "', 'm\\')
    assert scripts == [
        'display notification "m\\\\" with title "T\\\\\\" & (do shell script \\"id\\") & \\""'
    ]


def test_mac_focus_window_title_is_an_applescript_literal(mac_session, monkeypatch):
    mod, _ = mac_session
    scripts = _osascript_recorder(mod, monkeypatch)
    mod.focus_window(title='a\\" & (do shell script "id") & "')
    [script] = scripts
    literal = '"a\\\\\\" & (do shell script \\"id\\") & \\""'
    assert f"if name of proc contains {literal} then" in script
    assert f"error \"No process matching \" & {literal}" in script
    assert script.count("do shell script") == 2  # both only inside the literal
