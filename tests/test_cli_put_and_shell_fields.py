"""``remoteos-call`` (#67): the checks of a chunked put and Shell's structured fields.

Replies a real server cannot produce are faked at ``cli_call._run``, the network boundary; the
real-stack tests are in ``test_cli_call.py``.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx2
import pytest
from click.testing import CliRunner
from fastmcp.client.client import CallToolResult
from mcp.types import TextContent

from remoteos import cli_call
from remoteos.gateway import app as gateway_app

_M = cli_call.Machine("linux-x", "http://127.0.0.1:9/mcp")
_SHA = hashlib.sha256(b"12345").hexdigest()


def _reply(text: str, is_error: bool = False, fields: dict | None = None):
    content = [TextContent(type="text", text=text)]
    return CallToolResult(content=content, structured_content=fields, meta=None, is_error=is_error)


@pytest.fixture
def remote(monkeypatch):
    """Answer every call with the next queued reply; record what was asked."""
    queued: list = []
    asked: list[tuple[str, dict, float]] = []

    def fake_run(machine, tool, arguments, wait_s):
        asked.append((tool, arguments, wait_s))
        return queued.pop(0)

    monkeypatch.setattr(cli_call, "_run", fake_run)
    return queued, asked


@pytest.fixture
def local(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"12345")
    return path


def test_a_put_uploads_checks_and_renames_in_that_order(remote, local):
    queued, asked = remote
    queued.extend(
        [
            _reply("[task:a] Written 5 bytes to /r.part"),
            _reply(f"[task:b] sha256:{_SHA} 5 bytes"),
            _reply("[task:c] Renamed /r.part to /r"),
        ]
    )
    assert cli_call.run_put(_M, local, "/r", 9.0) == 0
    assert [(tool, wait) for tool, _, wait in asked] == [("FileUpload", 9.0), ("FileHash", 9.0), ("FileRename", 9.0)]
    assert asked[1][1] == {"path": "/r.part"}
    assert asked[2][1] == {"path": "/r.part", "new_path": "/r"}


def test_only_the_first_chunk_replaces_and_each_is_under_the_machine_s_request_limit(remote, tmp_path, monkeypatch):
    queued, asked = remote
    monkeypatch.setattr(cli_call, "PUT_CHUNK_BYTES", 2)
    path = tmp_path / "f.bin"
    path.write_bytes(b"12345")
    queued.extend(_reply(f"Written {n} bytes to /r.part") for n in (2, 2, 1))
    queued.extend([_reply(f"sha256:{_SHA} 5 bytes"), _reply("Renamed")])
    assert cli_call.run_put(_M, path, "/r", 9.0) == 0
    uploads = [args for tool, args, _ in asked if tool == "FileUpload"]
    assert [args["append"] for args in uploads] == [False, True, True]
    assert [args.get("permissions_from") for args in uploads] == ["/r", None, None]


def test_the_chunk_size_fits_a_machine_s_request_with_room_for_the_json():
    assert (cli_call.PUT_CHUNK_BYTES + 2) // 3 * 4 + 1024 < 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (f"sha256:{'0' * 64} 5 bytes", "put: sha256 mismatch on /r.part, /r is untouched"),
        (f"sha256:{_SHA} 4 bytes", "put: size mismatch, sent 5 bytes, /r.part has 4"),
        ("what?", "put: unexpected FileHash reply: what?"),
    ],
)
def test_put_never_renames_a_part_that_fails_its_check(remote, local, reply, message):
    queued, asked = remote
    queued.extend([_reply("Written 5 bytes to /r.part"), _reply(f"[task:b] {reply}")])
    with pytest.raises(cli_call.CallFailed) as caught:
        cli_call.run_put(_M, local, "/r", 9.0)
    assert (caught.value.code, caught.value.message) == (125, message)
    assert [call[0] for call in asked] == ["FileUpload", "FileHash"]


def test_a_server_without_append_is_named_in_one_line_and_nothing_follows(remote, local):
    queued, asked = remote
    refused = "1 validation error\nappend\n  Unexpected keyword argument [type=unexpected_keyword_argument]"
    queued.append(_reply(refused, is_error=True))
    with pytest.raises(cli_call.CallFailed) as caught:
        cli_call.run_put(_M, local, "/r", 9.0)
    assert (caught.value.code, caught.value.message) == (
        125,
        "put: linux-x runs a server older than 0.7.0.dev43: upgrade it",
    )
    assert len(asked) == 1


def test_a_failed_rename_is_a_tool_error(remote, local):
    queued, _ = remote
    queued.extend(
        [
            _reply("Written 5 bytes to /r.part"),
            _reply(f"sha256:{_SHA} 5 bytes"),
            _reply("[task:c] FileRename error: denied", is_error=True),
        ]
    )
    with pytest.raises(cli_call.CallFailed) as caught:
        cli_call.run_put(_M, local, "/r", 9.0)
    assert (caught.value.code, caught.value.message) == (125, "FileRename error: denied")


@pytest.mark.parametrize(
    "fields",
    [
        None,
        {"result": "text"},  # what a server older than 0.7.0.dev43 sends
        {"stdout": "a", "stderr": "b", "exit_code": "1"},
        {"stdout": "a", "stderr": None, "exit_code": 1},
        {"stdout": "a", "stderr": "b", "exit_code": True},
    ],
)
def test_shell_falls_back_to_the_text_without_valid_structured_fields(fields):
    result = _reply("[task:s] out\n[STDERR] err\n[Exit code: 3]", fields=fields)
    assert cli_call.shell_reply(result) == cli_call.ShellReply("out", "err", 3)


def test_the_fallback_prints_each_stream_with_one_newline_like_before(remote, capsys):
    queued, _ = remote
    queued.append(_reply("[task:s] out\n[STDERR] err"))
    assert cli_call.run_shell(_M, "x", 5, None) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("out\n", "err\n")


def test_shell_timeout_reaches_the_tool_and_the_client_waits_60_s_more(remote):
    queued, asked = remote
    queued.append(_reply("[task:t] ok"))
    assert cli_call.run_shell(_M, "x", 3600, None) == 0
    assert asked == [("Shell", {"command": "x", "timeout": 3600}, 3660.0)]


def test_timeout_is_the_wait_of_a_put(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(cli_call, "run_put", lambda _m, _l, _r, wait: waits.append(wait) or 0)
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers": {"linux-x": {"url": "http://127.0.0.1:9/mcp"}}}', encoding="utf-8")
    monkeypatch.setattr(cli_call, "prepare_process", lambda: None)
    args = ["--config", str(config), "linux-x", "put", "a", "/r", "--timeout", "1234"]
    assert CliRunner().invoke(cli_call.main, args).exit_code == 0
    assert waits == [1234.0]


def test_the_gateway_lets_a_one_hour_shell_call_finish():
    assert gateway_app.UPSTREAM_TIMEOUT.read > cli_call.declared_runtime({"timeout": 3600})
    assert gateway_app.UPSTREAM_TIMEOUT.read == 3700.0


def test_only_a_post_gets_the_long_read_and_the_trace_survives_either_way():
    trace = object()
    assert gateway_app.upstream_extensions("POST", None) == {}
    assert gateway_app.upstream_extensions("POST", trace) == {"trace": trace}
    extensions = gateway_app.upstream_extensions("GET", trace)
    assert extensions["trace"] is trace
    assert extensions["timeout"]["read"] == 600.0


def test_a_request_s_timeout_extension_beats_the_client_s_long_read():
    async def silent(reader, writer):
        await reader.read(1000)
        await asyncio.sleep(5)
        writer.close()

    async def go() -> None:
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        timeout = dict(gateway_app.upstream_extensions("GET", None)["timeout"], read=0.2)
        async with httpx2.AsyncClient(timeout=gateway_app.UPSTREAM_TIMEOUT, trust_env=False) as client:
            request = httpx2.Request("GET", f"http://127.0.0.1:{port}/", extensions={"timeout": timeout})
            with pytest.raises(httpx2.ReadTimeout):
                await client.send(request, stream=True)
        server.close()

    asyncio.run(go())
