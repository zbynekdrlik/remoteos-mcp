"""A failing tool reaches the MCP client as an error result (``isError: true``), never as success (#32).

Adapted from upstream winremote-mcp PR 83 ``tests/test_tool_errors.py``. Every
call here goes through a REAL in-process FastMCP 4.0.3 client and server
(``fastmcp.Client(remoteos.__main__.mcp)``): the task-manager wrapper, FastMCP's
error handling and the MCP result are the real ones. Only the OS boundary is
faked where a test needs a failure (a helper's subprocess, a headless session).
Expected texts are literals, never imported from the code under test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from fastmcp import Client

import remoteos.__main__ as server
from remoteos.platform.linux import desktop as linux_desktop
from remoteos.platform.linux import services as linux_services

_TASK = re.compile(r"^\[task:([0-9a-f]{12})\] ")
_HEADLESS = "Not available on headless Linux (no display)"
_WAYLAND = "Not available: Wayland session detected (X11 backend only — Wayland support is a separate tier)"


def _call(name: str, args: dict | None = None):
    async def _go():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(_go())


def _error_text(name: str, args: dict | None = None) -> tuple[str, str]:
    """``(task id, text after the [task:…] prefix)`` of a call that must be an MCP error."""
    result = _call(name, args)
    assert result.is_error is True, f"{name} reported success: {result.content}"
    (block,) = result.content
    match = _TASK.match(block.text)
    assert match, f"no [task:…] prefix: {block.text!r}"
    return match.group(1), block.text[match.end() :]


def test_a_service_helper_failure_is_an_mcp_error(monkeypatch):
    def _no_systemctl(cmd, timeout=30):
        raise RuntimeError("systemctl unavailable")

    monkeypatch.setattr(linux_services, "_run", _no_systemctl)
    _, text = _error_text("ServiceList")
    assert text == "ServiceList error: systemctl unavailable"


def test_a_failure_is_logged_without_its_message(monkeypatch, caplog):
    # The error text can echo a tool argument (a path, a title, a process name); logs never
    # carry tool arguments or replies (#31), so a failure logs the tool and the error type only.
    secret = "secret-" + "marker-32"  # built at run time: the logged frames show source lines, never values

    def _fails(cmd, timeout=30):
        raise RuntimeError(secret)

    monkeypatch.setattr(linux_services, "_run", _fails)
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        _, text = _error_text("ServiceList")
    assert text == "ServiceList error: secret-marker-32"  # the client gets the whole message
    assert "ServiceList" in caplog.text and "RuntimeError" in caplog.text
    assert "Traceback" in caplog.text  # where it broke stays in the log
    assert "secret-marker-32" not in caplog.text


def test_shell_with_a_missing_cwd_is_an_mcp_error(tmp_path):
    missing = tmp_path / "nope"
    _, text = _error_text("Shell", {"command": "true", "cwd": str(missing)})
    assert text == f"Shell error: working directory not found: {missing}"


def test_file_upload_with_bad_base64_is_an_mcp_error_and_writes_nothing(tmp_path):
    dest = tmp_path / "x.bin"
    _, text = _error_text("FileUpload", {"path": str(dest), "data_base64": "!!!!"})
    assert text.startswith("FileUpload error: data_base64 is not valid base64")
    assert not dest.exists()


def _unused_pid() -> int:
    """A pid above the kernel's pid_max: no process can ever have it."""
    try:
        return int(Path("/proc/sys/kernel/pid_max").read_text()) + 1
    except OSError:
        return 4194305


def test_kill_process_with_no_such_pid_is_an_mcp_error():
    pid = _unused_pid()
    _, text = _error_text("KillProcess", {"pid": pid})
    assert text == f"KillProcess error: PID {pid} not found"


def test_file_read_of_a_missing_file_is_an_mcp_error(tmp_path):
    missing = tmp_path / "missing.txt"
    _, text = _error_text("FileRead", {"path": str(missing)})
    assert text == f"File not found: {missing}"


def test_an_unexpected_exception_in_a_tool_is_an_mcp_error_carrying_its_message(monkeypatch):
    def _broken():
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(server.process_mgr, "get_system_info", _broken)
    _, text = _error_text("GetSystemInfo")
    assert text == "GetSystemInfo error: division by zero"


def test_a_failed_call_leaves_a_failed_task_with_the_error(tmp_path):
    missing = tmp_path / "nope"
    task_id, _ = _error_text("Shell", {"command": "true", "cwd": str(missing)})
    status = _call("GetTaskStatus", {"task_id": task_id})
    assert status.is_error is False
    info = json.loads(status.content[0].text)
    assert info["status"] == "failed"
    assert info["error"] == f"Shell error: working directory not found: {missing}"


def test_a_successful_call_is_not_an_error():
    result = _call("Shell", {"command": "echo hi"})
    assert result.is_error is False
    (block,) = result.content
    assert _TASK.match(block.text) and block.text.endswith("] hi")


def test_the_error_text_is_not_masked_even_with_mask_error_details(monkeypatch, tmp_path):
    # FastMCP masks only ordinary exceptions; a ToolError message always reaches the client
    monkeypatch.setattr(server.mcp, "_mask_error_details", True)
    missing = tmp_path / "nope"
    _, text = _error_text("Shell", {"command": "true", "cwd": str(missing)})
    assert text == f"Shell error: working directory not found: {missing}"


def test_cancel_task_with_an_unknown_id_is_an_mcp_error():
    result = _call("CancelTask", {"task_id": "nope"})
    assert result.is_error is True
    assert result.content[0].text == "Cancel failed: Task nope not found"


def test_get_task_status_with_an_unknown_id_is_an_mcp_error():
    result = _call("GetTaskStatus", {"task_id": "nope"})
    assert result.is_error is True
    assert result.content[0].text == "Task nope not found"


# ---------------------------------------------------------------------------
# Linux without X11: the exact stub text (the cam1..4 contract), now as an MCP error
# ---------------------------------------------------------------------------

_DESKTOP_CALLS = [
    ("Snapshot", {}),
    ("AnnotatedSnapshot", {}),
    ("ScreenRecord", {"duration": 0.1}),
    ("Click", {"x": 10, "y": 10}),
    ("Type", {"text": "x"}),
    ("Move", {"x": 10, "y": 10}),
    ("Scroll", {"amount": 3, "direction": "down"}),
    ("Shortcut", {"keys": "ctrl+c"}),
    ("FocusWindow", {"title": "Editor"}),
    ("MinimizeAll", {}),
    ("App", {"name": "firefox"}),
    ("GetClipboard", {}),
    ("SetClipboard", {"text": "x"}),
]


@pytest.fixture
def no_subprocess(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a desktop tool without a display must not run a subprocess")

    monkeypatch.setattr(linux_desktop, "_run_x", _boom)
    monkeypatch.setattr(linux_desktop, "_run_capture", _boom)


@pytest.mark.parametrize(("name", "args"), _DESKTOP_CALLS + [("LockScreen", {}), ("Notification", {"message": "x"})])
def test_headless_desktop_tool_is_an_mcp_error_with_the_unchanged_stub(monkeypatch, no_subprocess, name, args):
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="headless"))
    _, text = _error_text(name, args)
    assert text == _HEADLESS


@pytest.mark.parametrize(("name", "args"), _DESKTOP_CALLS)
def test_wayland_desktop_tool_is_an_mcp_error_with_the_unchanged_stub(monkeypatch, no_subprocess, name, args):
    monkeypatch.setattr(linux_desktop, "_SESSION", linux_desktop.SessionInfo(mode="wayland"))
    _, text = _error_text(name, args)
    assert text == _WAYLAND
