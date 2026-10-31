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
import os
import re
import subprocess
from pathlib import Path

import pytest
from fake_console_child import popen_answered_by
from fastmcp import Client
from fastmcp.exceptions import ToolError
from tool_surface import server

from remoteos.platform.linux import desktop as linux_desktop
from remoteos.platform.linux import session as linux_session

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
    # only the OS boundary is faked (review round 8): the stdlib subprocess.run cannot start systemctl
    def _no_systemctl(cmd, **kwargs):
        raise OSError("systemctl unavailable")

    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(_no_systemctl))
    _, text = _error_text("ServiceList")
    assert text == "ServiceList error: systemctl unavailable"


def test_a_failure_is_logged_without_its_message(monkeypatch, caplog):
    # The error text can echo a tool argument (a path, a title, a process name); logs never
    # carry tool arguments or replies (#31), so a failure logs the tool and the error type only.
    secret = "secret-" + "marker-32"  # built at run time: the logged frames show source lines, never values

    def _fails(cmd, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(subprocess, "Popen", popen_answered_by(_fails))
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        _, text = _error_text("ServiceList")
    assert text == "ServiceList error: secret-marker-32"  # the client gets the whole message
    assert "ServiceList" in caplog.text and "OSError" in caplog.text
    assert "Traceback" in caplog.text  # where it broke stays in the log
    assert "secret-marker-32" not in caplog.text


@pytest.fixture
def every_log(caplog):
    """caplog that also hears the ``fastmcp`` loggers (they do not propagate: FastMCP has its own handler)."""
    fastmcp_log = logging.getLogger("fastmcp")
    fastmcp_log.addHandler(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="remoteos"):
        yield caplog
    fastmcp_log.removeHandler(caplog.handler)


def test_refused_arguments_are_never_logged(tmp_path, every_log):
    # review round 2: a path or a working directory is a tool argument; logs never carry those (#31)
    marker = "secret-" + "path-32"
    _error_text("Shell", {"command": "true", "cwd": str(tmp_path / marker)})
    _error_text("FileUpload", {"path": str(tmp_path / marker), "data_base64": "!!!!"})
    _error_text("FileUpload", {"path": str(tmp_path / marker), "data_base64": "A" * (100 * 1024 * 1024 + 4)})
    assert "Shell refused" in every_log.text and "FileUpload refused" in every_log.text
    assert marker not in every_log.text


def test_a_written_upload_logs_its_size_never_its_path(tmp_path, every_log):
    marker = "secret-" + "path-32"
    result = _call("FileUpload", {"path": str(tmp_path / marker), "data_base64": "aGk="})
    assert result.is_error is False
    assert "FileUpload wrote 2 bytes" in every_log.text
    assert marker not in every_log.text


def test_fastmcp_never_logs_the_value_of_an_invalid_argument(every_log):
    # FastMCP logs "Invalid arguments for tool ...: [... 'input': <value>]" at WARNING (review round 2)
    marker = "secret-" + "arg-32"
    result = _call("KillProcess", {"pid": marker})
    assert result.is_error is True  # the client still gets the validation error
    assert "Invalid arguments for tool 'KillProcess'" in every_log.text
    assert "int_parsing" in every_log.text  # what was wrong stays in the log
    assert marker not in every_log.text


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


def test_every_registered_tool_reports_its_failures_as_mcp_errors():
    # a tool without @reports_errors would fall into FastMCP's own exception path, which
    # logs the message (#31) and words it differently (#32 review round 1)
    missing = []
    for name, tool in server._get_registered_tools().items():
        fn = tool.fn
        while fn is not None and not getattr(fn, "__remoteos_reports_errors__", False):
            fn = getattr(fn, "__wrapped__", None)
        if fn is None:
            missing.append(name)
    assert missing == []


def test_an_expected_failure_is_logged_at_warning_not_error(tmp_path):
    # a direct ToolError ("File not found: …") is an expected failure, like the mapped ones
    with pytest.raises(ToolError) as exc:
        server.FileRead(str(tmp_path / "missing.txt"))
    assert exc.value.log_level == logging.WARNING
    with pytest.raises(ToolError) as exc:
        server.Shell("true", cwd=str(tmp_path / "nope"))
    assert exc.value.log_level == logging.WARNING


def test_file_list_shows_an_entry_with_an_unconvertible_mtime_as_unknown(tmp_path, monkeypatch):
    # one entry whose mtime datetime cannot convert lists as "?"; it never fails the listing
    (tmp_path / "good.txt").write_text("x")
    bad = tmp_path / "bad.txt"
    bad.write_text("y")
    os.utime(bad, (1_000_012_345, 1_000_012_345))
    real = server.datetime

    class _Datetime:
        @staticmethod
        def fromtimestamp(ts):
            if ts == 1_000_012_345:
                raise ValueError("year 99999 is out of range")
            return real.fromtimestamp(ts)

    monkeypatch.setattr(server, "datetime", _Datetime)
    out = server.FileList(str(tmp_path))
    assert re.search(r"^\?\s+bad\.txt\s+\?\s+\?\s*$", out, re.MULTILINE), out
    assert re.search(r"^FILE\s+good\.txt", out, re.MULTILINE), out


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

    monkeypatch.setattr(linux_session, "_run_x", _boom)
    monkeypatch.setattr(linux_session, "_run_capture", _boom)


@pytest.mark.parametrize(("name", "args"), _DESKTOP_CALLS + [("LockScreen", {}), ("Notification", {"message": "x"})])
def test_headless_desktop_tool_is_an_mcp_error_with_the_unchanged_stub(monkeypatch, no_subprocess, name, args):
    monkeypatch.setattr(linux_session, "_SESSION", linux_desktop.SessionInfo(mode="headless"))
    _, text = _error_text(name, args)
    assert text == _HEADLESS


@pytest.mark.parametrize(("name", "args"), _DESKTOP_CALLS)
def test_wayland_desktop_tool_is_an_mcp_error_with_the_unchanged_stub(monkeypatch, no_subprocess, name, args):
    monkeypatch.setattr(linux_session, "_SESSION", linux_desktop.SessionInfo(mode="wayland"))
    _, text = _error_text(name, args)
    assert text == _WAYLAND
