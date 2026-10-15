"""Helpers report a failure by raising, and only a failure (#32 review round 1).

Each helper runs for real; only its OS boundary is faked (the stdlib
``subprocess.run``, the screen grab, ``psutil``). A command that CHANGES state (start/stop a
service, create/delete a task, read/write a macOS default) and exits non-zero
failed, so it raises and the tool reports an MCP error. A listing keeps the
command's own answer, exit code included.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import types

import pytest
from fastmcp import Client
from PIL import Image

import remoteos.__main__ as server
from remoteos import ocr, process_mgr
from remoteos.platform.linux import services as linux_services
from remoteos.platform.mac import services as mac_services
from remoteos.platform.mac import system as mac_system
from remoteos.platform.win import services as win_services


class _Commands:
    """A fake ``subprocess.run``: records every argv and answers ``(returncode, stdout, stderr)``."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls: list[list[str]] = []
        self.reply = (returncode, stdout, stderr)

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        rc, out, err = self.reply
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


# ---------------------------------------------------------------------------
# Windows OCR: a PowerShell script that fails part-way can exit 0
# ---------------------------------------------------------------------------


@pytest.fixture
def windows_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "ImageGrab", types.SimpleNamespace(grab=lambda bbox=None: Image.new("RGB", (8, 8))))
    commands = _Commands()
    monkeypatch.setattr(ocr.subprocess, "run", commands)
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
    assert argv[-1].lstrip().startswith("$ErrorActionPreference = 'Stop'")


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
    monkeypatch.setattr(subprocess, "run", commands)
    with pytest.raises(RuntimeError, match="Unit nosuch.service not found"):
        call(module)


def test_a_failed_linux_task_enable_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_services, "_SYSTEMD_DIR", str(tmp_path))
    commands = _Commands()

    def _run(cmd, **kwargs):
        commands.calls.append(list(cmd))
        rc = 1 if cmd[:2] == ["systemctl", "enable"] else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="Failed to enable unit: Access denied")

    monkeypatch.setattr(subprocess, "run", _run)
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
    monkeypatch.setattr(subprocess, "run", _Commands(returncode=1, stdout="", stderr="No events were found"))
    out = call(module)
    assert "No events were found" in out and "[Exit code: 1]" in out


def test_service_start_failure_reaches_the_client_as_an_mcp_error(monkeypatch):
    failed = _Commands(returncode=5, stderr="Unit nosuch.service not found.")
    monkeypatch.setattr(subprocess, "run", failed)

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
