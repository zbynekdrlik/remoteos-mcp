"""Helpers report a failure by raising, and only a failure (#32 review round 1).

Each helper runs for real; only its OS boundary is faked (the stdlib
``subprocess.run``, the screen grab, ``psutil``). A command that CHANGES state (start/stop a
service, create/delete a task, read/write a macOS default) and exits non-zero
failed, so it raises and the tool reports an MCP error. A listing keeps the
command's own answer, exit code included.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import sys
import types

import pytest
from fake_console_child import POWERSHELL_UTF8_PREFIX, TEXT_MODE, popen_answered_by, run_result
from fastmcp import Client
from PIL import Image

import remoteos.__main__ as server
from remoteos import ocr, process_mgr
from remoteos.platform.linux import services as linux_services
from remoteos.platform.mac import services as mac_services
from remoteos.platform.mac import system as mac_system
from remoteos.platform.win import services as win_services


class _Commands:
    """A ``run``-style fake (for ``popen_answered_by``): records every argv, answers ``(returncode, stdout, stderr)``.

    The reply has the type subprocess returns for that call: text in text mode, else bytes (#48).
    """

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.reply = (returncode, stdout, stderr)

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        rc, out, err = self.reply
        return run_result(kwargs, rc, out, err)


# ---------------------------------------------------------------------------
# Windows OCR: a PowerShell script that fails part-way can exit 0
# ---------------------------------------------------------------------------


@pytest.fixture
def windows_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "ImageGrab", types.SimpleNamespace(grab=lambda bbox=None: Image.new("RGB", (8, 8))))
    commands = _Commands()
    monkeypatch.setattr(ocr.subprocess, "Popen", popen_answered_by(commands))
    return commands


def test_windows_ocr_script_that_fails_but_exits_0_is_an_error(windows_ocr):
    # live on win-resolume: a call on a null OcrEngine exits 0, stdout empty, the error on stderr
    windows_ocr.reply = (0, "", "You cannot call a method on a null-valued expression.")
    with pytest.raises(RuntimeError, match="null-valued expression"):
        ocr.ocr_windows_builtin()


def test_windows_ocr_script_stops_on_its_first_error(windows_ocr):
    windows_ocr.reply = (0, "Hello", "")
    assert ocr.ocr_windows_builtin() == "Hello"
    (argv,) = windows_ocr.calls
    prefix, script = argv[-1].split("\n", 1)  # #48: the shared UTF-8 prefix line comes first
    assert prefix == POWERSHELL_UTF8_PREFIX
    assert script.lstrip().startswith("$ErrorActionPreference = 'Stop'")


def test_windows_ocr_with_no_text_is_an_empty_result(windows_ocr):
    windows_ocr.reply = (0, "", "")
    assert ocr.ocr_windows_builtin() == ""


# ---------------------------------------------------------------------------
# Services, tasks, defaults: a state-changing command that exits non-zero failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "call"),
    [
        (linux_services, lambda m: m.service_start("nosuch")),
        (linux_services, lambda m: m.service_stop("nosuch")),
        (win_services, lambda m: m.service_start("nosuch")),
        (win_services, lambda m: m.service_stop("nosuch")),
        (win_services, lambda m: m.task_create("t", "cmd", "DAILY")),
        (win_services, lambda m: m.task_delete("t")),
        (mac_services, lambda m: m.service_start("nosuch")),
        (mac_services, lambda m: m.service_stop("nosuch")),
        (mac_system, lambda m: m.reg_read("com.example", "missing")),
        (mac_system, lambda m: m.reg_write("com.example", "k", "v")),
    ],
)
def test_a_failed_state_changing_command_raises_with_its_stderr(monkeypatch, module, call):
    commands = _Commands(returncode=5, stderr="Unit nosuch.service not found.\n")
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(commands))
    with pytest.raises(RuntimeError, match="Unit nosuch.service not found"):
        call(module)


def test_a_failed_linux_task_enable_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_services, "_SYSTEMD_DIR", str(tmp_path))
    commands = _Commands()

    def _run(cmd, **kwargs):
        commands.calls.append(list(cmd))
        rc = 1 if cmd[:2] == ["systemctl", "enable"] else 0
        return run_result(kwargs, rc, "", "Failed to enable unit: Access denied")

    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(_run))
    with pytest.raises(RuntimeError, match="Access denied"):
        linux_services.task_create("t", "true", "DAILY")


@pytest.mark.parametrize(
    ("module", "call"),
    [
        (linux_services, lambda m: m.service_list()),
        (linux_services, lambda m: m.event_log()),
        (win_services, lambda m: m.service_list()),
        (win_services, lambda m: m.event_log()),
    ],
)
def test_a_listing_keeps_the_commands_own_answer(monkeypatch, module, call):
    # e.g. Get-WinEvent says "No events were found" with a non-zero exit: that is the answer
    no_events = _Commands(returncode=1, stdout="", stderr="No events were found")
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(no_events))
    out = call(module)
    assert "No events were found" in out and "[Exit code: 1]" in out


def test_service_start_failure_reaches_the_client_as_an_mcp_error(monkeypatch):
    failed = _Commands(returncode=5, stderr="Unit nosuch.service not found.")
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(failed))

    async def _go():
        async with Client(server.mcp) as client:
            return await client.call_tool("ServiceStart", {"name": "nosuch"}, raise_on_error=False)

    result = asyncio.run(_go())
    assert result.is_error is True
    text = result.content[0].text
    assert re.match(r"^\[task:[0-9a-f]{12}\] ServiceStart error: .*Unit nosuch.service not found", text), text


# ---------------------------------------------------------------------------
# KillProcess logs counts and pids, never the process names (#31: logs carry no arguments/replies)
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, pid, name):
        self.info = {"pid": pid, "name": name}
        self.pid = pid
        self._name = name
        self.killed = False

    def name(self):
        return self._name

    def kill(self):
        self.killed = True


def test_kill_by_name_logs_the_count_never_the_names(monkeypatch, caplog):
    secret = "secret-" + "proc-32"  # built at run time
    victim = _FakeProcess(4321, secret)
    monkeypatch.setattr(process_mgr.psutil, "process_iter", lambda attrs=None: [victim])
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        assert process_mgr.kill_process(name=secret) == f"Killed: {secret} (PID 4321)"
    assert victim.killed
    assert "killed 1 process" in caplog.text
    assert secret not in caplog.text


def test_kill_by_pid_logs_the_pid_never_the_name(monkeypatch, caplog):
    secret = "secret-" + "proc-32"
    victim = _FakeProcess(4321, secret)
    monkeypatch.setattr(process_mgr.psutil, "Process", lambda pid: victim)
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        assert process_mgr.kill_process(pid=4321) == f"Killed PID 4321 ({secret})"
    assert "PID 4321" in caplog.text
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Review round 2: output that is not valid in the console code page never hides the failure
# ---------------------------------------------------------------------------


def test_undecodable_command_output_still_reports_the_failure():
    # e.g. localized PowerShell stderr in the OEM code page (cp852): strict decoding raised
    # UnicodeDecodeError and replaced the real reason with a codec message
    from remoteos.platform.command import run_command

    script = "import sys; sys.stderr.buffer.write(b'\\x90 service refused'); sys.exit(3)"
    with pytest.raises(RuntimeError, match="service refused"):
        run_command([sys.executable, "-c", script], check=True, label="probe")
    assert "service refused" in run_command([sys.executable, "-c", script])


# ---------------------------------------------------------------------------
# Review round 4: a native Windows console program is read in its OEM code page
# ---------------------------------------------------------------------------
#
# Live on win-pz-server: PowerShell and native tools write the OEM code page (852), but Python
# reads a pipe as ANSI (1250), so "AčšžB" came back as "Aźç§B"; and on Windows a decode error
# in subprocess's reader thread leaves stdout None: Shell said "(no output)", OCR and Ping
# crashed on None.strip(). "oem" is Python's codec for the system OEM code page (Windows only).
#
# Review round 5: a child writes the CURRENT page of the console it is attached to, and every
# child shared the server's own console, so one `chcp 65001` in a Shell call garbled every later
# reply for every client (live: 'A─Ź┼í┼żB'). CREATE_NO_WINDOW (0x08000000) gives each child its
# own hidden console at the default OEM page, which is exactly what "oem" decodes.
#
# #48: a PowerShell child switches its own console to UTF-8 and is read as exact UTF-8 (Shell,
# run_command, OCR: tests/test_output_encoding.py). console_child() stays for ping, a native
# program started directly, whose localized text fits the OEM page.

_CREATE_NO_WINDOW = 0x08000000
_OEM = {"text": True, "encoding": "oem", "errors": "replace", "creationflags": _CREATE_NO_WINDOW}


def test_a_windows_console_child_gets_its_own_console_in_the_oem_code_page():
    from remoteos.platform.command import console_child

    assert console_child(windows=True) == _OEM
    # off Windows: the locale, and no creationflags (POSIX rejects a non-zero value)
    assert console_child(windows=False) == {"text": True, "encoding": None, "errors": "replace"}


def test_windows_ocr_reads_its_own_console_losslessly_as_utf8(windows_ocr):
    # review round 5: through the OEM page PowerShell replaced € and – ("12,50 €" read as "12,50 ?",
    # live "1€–č" -> "1?-č"). #48: OCR runs the shared UTF-8 prefix in its own console (its private
    # copy is gone), and Python decodes the raw bytes as UTF-8: lossless, the server's console untouched.
    windows_ocr.reply = (0, "12,50 € – Žltý", "")
    assert ocr.ocr_windows_builtin() == "12,50 € – Žltý"
    (kwargs,) = windows_ocr.kwargs
    assert not any(kwargs.get(key) for key in TEXT_MODE), kwargs  # binary: the bytes are decoded by us
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    (argv,) = windows_ocr.calls
    assert argv[:3] == ["powershell", "-NoProfile", "-Command"]
    assert argv[-1].startswith(POWERSHELL_UTF8_PREFIX + "\n")
    assert argv[-1].count("[Console]::OutputEncoding") == 1  # only the shared prefix sets it


def test_ping_reads_a_windows_console_in_its_code_page(monkeypatch):
    from remoteos import network

    commands = _Commands(stdout="Odpoveď od 10.0.0.1")
    monkeypatch.setattr(subprocess, "run", commands)
    monkeypatch.setattr(sys, "platform", "win32")
    assert network.ping("10.0.0.1", count=1) == "Odpoveď od 10.0.0.1"
    assert commands.calls == [["ping", "-n", "1", "10.0.0.1"]]
    assert {key: commands.kwargs[0][key] for key in _OEM} == _OEM


# ---------------------------------------------------------------------------
# Review round 8: the remaining raise paths, each with the OS boundary faked
# ---------------------------------------------------------------------------
#
# Deleting a task that does not exist is an error; unloading/disabling one the OS no longer
# knows is not (the files are removed anyway, so that output stays part of the answer).


def _replies(calls: list, failing: list[str], stderr: str):
    """A ``run``-style fake (for ``popen_answered_by``): a command starting with ``failing`` exits 1 with ``stderr``."""

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[: len(failing)] == failing:
            return run_result(kwargs, 1, "", stderr)
        return run_result(kwargs)

    return _run


def test_linux_task_delete_of_a_missing_task_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_services, "_SYSTEMD_DIR", str(tmp_path))
    commands = _Commands()
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(commands))
    with pytest.raises(FileNotFoundError, match="No remoteos task 'gone'"):
        linux_services.task_delete("gone")
    assert commands.calls == []  # nothing was disabled or reloaded


def test_linux_task_delete_removes_the_units_when_systemd_no_longer_knows_the_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_services, "_SYSTEMD_DIR", str(tmp_path))
    timer, service = tmp_path / "remoteos-t.timer", tmp_path / "remoteos-t.service"
    timer.write_text("[Timer]\n")
    service.write_text("[Service]\n")
    calls: list = []
    not_loaded = "Unit remoteos-t.timer not loaded."
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(_replies(calls, ["systemctl", "disable"], not_loaded)))
    out = linux_services.task_delete("t")
    assert "not loaded" in out and "[Exit code: 1]" in out
    assert f"Deleted {timer}" in out and f"Deleted {service}" in out
    assert not timer.exists() and not service.exists()
    assert calls == [["systemctl", "disable", "--now", "remoteos-t.timer"], ["systemctl", "daemon-reload"]]


def test_mac_task_delete_of_a_missing_plist_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(mac_services, "_AGENTS_DIR", str(tmp_path))
    commands = _Commands()
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(commands))
    with pytest.raises(FileNotFoundError, match="Plist not found"):
        mac_services.task_delete("gone")
    assert commands.calls == []


def test_mac_task_delete_removes_the_plist_when_launchd_no_longer_has_it_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(mac_services, "_AGENTS_DIR", str(tmp_path))
    plist = tmp_path / "com.remoteos.t.plist"
    plist.write_bytes(b"<plist/>")
    calls: list = []
    not_loaded = "Could not find specified service"
    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(_replies(calls, ["launchctl", "unload"], not_loaded)))
    out = mac_services.task_delete("t")
    assert out.startswith(f"Unloaded and deleted {plist}") and "[Exit code: 1]" in out
    assert not plist.exists()
    assert calls == [["launchctl", "unload", str(plist)]]


@pytest.fixture
def linux_x11(monkeypatch):
    from remoteos.platform.linux import desktop as linux_desktop

    session = linux_desktop.SessionInfo(
        mode="x11", display=":0", xauthority="/run/user/1000/gdm/Xauthority", uid=1000, user="u", session_id="7"
    )
    monkeypatch.setattr(linux_desktop, "_SESSION", session)
    return linux_desktop


def test_linux_launch_app_that_fails_every_way_is_an_error(monkeypatch, linux_x11):
    launched: list = []

    class _ExitsAtOnce:
        def __init__(self, argv, **kwargs):
            launched.append(list(argv))

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(subprocess, "Popen", _ExitsAtOnce)
    with pytest.raises(RuntimeError, match=r"^Failed to launch zz-app \(last rc=1\)$"):
        linux_x11.launch_app("zz-app")
    assert len(launched) == 3  # gtk-launch, xdg-open, then the command itself


def test_linux_lock_screen_that_loginctl_refuses_is_an_error(monkeypatch, linux_x11):
    commands = _Commands(returncode=1, stderr="Access denied\n")
    monkeypatch.setattr(subprocess, "run", commands)
    with pytest.raises(RuntimeError, match="^loginctl lock-session failed: Access denied$"):
        linux_x11.lock_screen()
    assert commands.calls == [["loginctl", "lock-session", "7"]]


@pytest.fixture(params=["key", "value"])
def windows_registry(request, monkeypatch):
    """winreg where the key is missing (OpenKey raises) or only the value is (QueryValueEx raises)."""
    from remoteos.platform.win import registry

    def _missing():
        return FileNotFoundError(2, "The system cannot find the file specified")

    def _open_key(root, subkey, reserved, access):
        if request.param == "key":
            raise _missing()
        return contextlib.nullcontext("hkey")

    def _query_value(key, value_name):
        raise _missing()

    fake = types.SimpleNamespace(OpenKey=_open_key, QueryValueEx=_query_value, KEY_READ=0x20019)
    monkeypatch.setattr(registry, "HAS_WINREG", True)
    monkeypatch.setattr(registry, "winreg", fake, raising=False)
    monkeypatch.setitem(registry._ROOT_KEYS, "HKLM", 0x80000002)  # HKEY_LOCAL_MACHINE
    return registry


def test_windows_registry_read_of_a_missing_key_or_value_is_an_error(windows_registry):
    with pytest.raises(FileNotFoundError, match=r"^Key or value not found: HKLM\\SOFTWARE\\Nope\\Missing$"):
        windows_registry.reg_read("HKLM\\SOFTWARE\\Nope", "Missing")


def test_kill_by_a_name_that_matches_nothing_is_an_error(monkeypatch):
    bystander = _FakeProcess(4321, "bash")
    monkeypatch.setattr(process_mgr.psutil, "process_iter", lambda attrs=None: [bystander])
    with pytest.raises(RuntimeError, match="^No process matching 'zz-no-such-process'$"):
        process_mgr.kill_process(name="zz-no-such-process")
    assert not bystander.killed
