"""Caller values reach real Windows PowerShell 5.1 as literal text, never as code (#39).

No fakes: the helpers start the real console trampoline and ``powershell.exe``. Each payload, if
PowerShell ran it, writes a file named ``x`` into PowerShell's working directory (the test's
``tmp_path``, which the server's children inherit) or starts calc. Before #39 the service name and
App name went into double-quoted strings and the toast text into an expandable here-string, so
``$(Set-Content x pwned)`` ran.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remoteos.platform.command import powershell_argv, run_command
from remoteos.platform.win import desktop, services
from remoteos.platform.win.psquote import ps_literal

PAYLOADS = [
    "$(Set-Content x pwned)",
    "'; Set-Content x pwned; '",
    '"; Set-Content x pwned; "',
    "’; Set-Content x pwned; ’",
    "'; calc; '",
]
IDS = ["subexpression", "single-quote", "double-quote", "smart-quote", "calc"]


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """PowerShell's working directory: where ``Set-Content x`` would write."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _nothing_ran(workdir: Path) -> None:
    assert not (workdir / "x").exists(), "the payload ran as PowerShell"


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
def test_ps_literal_round_trips_through_real_powershell(payload, workdir):
    # PowerShell reads the literal back as exactly the value, whatever quotes it holds
    assert run_command(powershell_argv(f"Write-Output {ps_literal(payload)}")) == payload
    _nothing_ran(workdir)


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
def test_a_service_name_is_literal_text(payload, workdir):
    with pytest.raises(RuntimeError) as exc:
        services.service_start(payload)
    # Start-Service names the service it could not find: the payload, verbatim (lines joined in
    # case PowerShell wraps the error record at its console width)
    assert payload in "".join(str(exc.value).splitlines()), str(exc.value)
    _nothing_ran(workdir)


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
def test_an_event_log_name_is_literal_text(payload, workdir):
    reply = services.event_log(payload, 1)
    # "There is not an event log on the localhost computer that matches <name>."
    assert payload in "".join(reply.splitlines()), reply
    _nothing_ran(workdir)


# App names: Windows file names may hold $ ( ) ' ; and the typographic quote, but never ".
_APP_PAYLOADS = [p for p in PAYLOADS if '"' not in p]
_APP_IDS = [i for p, i in zip(PAYLOADS, IDS) if '"' not in p]


@pytest.mark.parametrize("payload", _APP_PAYLOADS, ids=_APP_IDS)
def test_an_app_name_and_its_arguments_are_literal_text(payload, workdir):
    # a batch file NAMED like the payload: Start-Process starts it only if it got the exact name;
    # it records the arguments it was given
    apps = workdir / "apps"
    apps.mkdir()
    marker = apps / "ran.txt"
    app = apps / f"{payload}.cmd"
    app.write_text(f'@echo off\r\n>"{marker}" echo %*\r\n', encoding="ascii")
    desktop.launch_app(str(app), "$(Set-Content x pwned)")
    for _ in range(100):
        if marker.exists() and marker.read_text(encoding="ascii").strip():
            break
        os.system("ping -n 1 127.0.0.1 >nul")  # ~1 s, no sleep in a test helper
    assert marker.read_text(encoding="ascii").strip() == "$(Set-Content x pwned)"
    _nothing_ran(workdir)
    _nothing_ran(apps)


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
def test_toast_text_becomes_the_exact_text_of_the_toast_xml(payload, workdir):
    # the script that builds the toast's XML, run for real with the same environment as the toast
    script = desktop.TOAST_XML_SCRIPT + "$xml.GetXml()"
    xml = run_command(powershell_argv(script), env=desktop.toast_environment(payload, f"{payload}\n<b>&</b>"))
    assert xml.startswith("<toast><visual><binding template=\"ToastGeneric\"><text>"), xml
    expected_title = payload.replace("&", "&amp;")
    assert f"<text>{expected_title}</text>" in xml, xml
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in xml, xml
    _nothing_ran(workdir)


@pytest.mark.parametrize("payload", PAYLOADS, ids=IDS)
def test_a_toast_with_a_payload_runs_nothing(payload, workdir):
    assert desktop.show_notification(payload, payload) == "Notification shown"
    _nothing_ran(workdir)
