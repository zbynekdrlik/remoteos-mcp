"""Tests for the remoteos MCP gateway (#25).

Hermetic: the "machine" is an in-process Starlette app reached through
``httpx2.ASGITransport`` (or a transport that simulates a dead machine / a slow stream);
the consumer side talks to the gateway app in-process as well. No network, no real keys,
and every file lives under ``tmp_path`` — the real registry / ~/.claude are never touched.
The shared harness is ``gateway_harness.py``; connection reuse lives in
``test_gateway_upstream_pool.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import signal
import stat

import httpx2
import pytest
from click.testing import CliRunner
from gateway_harness import (
    CONSUMER,
    MACHINE,
    MACHINE_KEY,
    MACHINE_KEY_VALUE,
    OTHER_CONSUMER,
    OTHER_MACHINE,
    OTHER_TOKEN,
    SSE_BODY,
    TOKEN,
    UPSTREAM_URL,
    WRONG_TOKEN,
    _BreakingTransport,
    _default_registry,
    _drive_asgi,
    _machines_file,
    _tools_list,
    _write_json,
)

from remoteos import __version__
from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.accesslog import AccessLog, read_access_log
from remoteos.gateway.registry import (
    LiveConfig,
    MachineSourceError,
    Registry,
    RegistryError,
    edit_registry,
    load_registry,
    token_sha256,
)


@pytest.fixture(autouse=True)
def _isolate_default_paths(tmp_path, monkeypatch):
    """Any CLI call that forgets an explicit path still lands in tmp, never the real home."""
    monkeypatch.setenv("REMOTEOS_GATEWAY_REGISTRY", str(tmp_path / "env-default" / "registry.json"))
    monkeypatch.setenv("REMOTEOS_GATEWAY_MACHINES", str(tmp_path / "env-default" / "machines.json"))
    monkeypatch.setenv("REMOTEOS_GATEWAY_ACCESS_LOG", str(tmp_path / "env-default" / "access.jsonl"))


# -- auth: 401 / 403 / 404 ------------------------------------------------------------


def test_wrong_token_is_401_and_nothing_reaches_the_machine(gw):
    wrong = gw.call("POST", f"/{MACHINE}/mcp", token=WRONG_TOKEN, json_body=_tools_list())
    missing = gw.call("POST", f"/{MACHINE}/mcp", token=None, json_body=_tools_list())
    basic = gw.call(
        "POST", f"/{MACHINE}/mcp", token=None, json_body=_tools_list(), headers={"Authorization": "Basic Zm9vOmJhcg=="}
    )

    assert (wrong.status_code, missing.status_code, basic.status_code) == (401, 401, 401)
    assert wrong.json() == {"error": "Unauthorized"}
    assert gw.machine.received == []


def test_consumer_not_allowed_that_machine_is_403(gw):
    # OTHER_CONSUMER is valid but allowed only OTHER_MACHINE.
    resp = gw.call("POST", f"/{MACHINE}/mcp", token=OTHER_TOKEN, json_body=_tools_list())

    assert resp.status_code == 403
    assert gw.machine.received == []


def test_not_allowed_wins_over_unexposed_so_exposure_cannot_be_probed(gw):
    with edit_registry(gw.registry_path) as reg:
        reg.unexpose(MACHINE)
    # OTHER_CONSUMER may not use MACHINE: it must get 403 whether or not MACHINE is exposed.
    resp = gw.call("POST", f"/{MACHINE}/mcp", token=OTHER_TOKEN, json_body=_tools_list())

    assert resp.status_code == 403


def test_unexposed_machine_is_404(gw):
    with edit_registry(gw.registry_path) as reg:
        reg.unexpose(MACHINE)  # CONSUMER is still allowed MACHINE — the kill switch wins

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list())

    assert resp.status_code == 404
    assert gw.machine.received == []


def test_unknown_path_is_404_without_auth(gw):
    assert gw.call("GET", "/nope", token=None).status_code == 404


@pytest.mark.parametrize(
    "raw_machine",
    ["x%0A2026-10-14%20FORGED%20INFO%20status%3D200", "%1B%5B31mRED", f"{MACHINE}%0A", ".hidden"],
    ids=["newline-forgery", "ansi-escape", "trailing-newline", "leading-dot"],
)
def test_malformed_machine_name_is_404_and_never_logged_raw(gw, caplog, raw_machine):
    caplog.set_level("DEBUG", logger="remoteos.gateway")

    resp = gw.call("POST", f"/{raw_machine}/mcp", token=None, json_body=_tools_list())

    assert resp.status_code == 404
    assert gw.machine.received == []
    for record in caplog.records:
        message = record.getMessage()
        assert "\n" not in message and "\x1b" not in message, message
    [entry] = read_access_log(gw.access_log_path)
    assert entry["outcome"] == "denied_bad_machine_name"
    assert "\n" not in gw.access_log_path.read_text(encoding="utf-8").rstrip("\n")


# -- pass-through ---------------------------------------------------------------------


def test_pass_through_injects_machine_key_and_never_forwards_consumer_token(gw):
    resp = gw.call(
        "POST",
        f"/{MACHINE}/mcp?trace=1",
        json_body=_tools_list(request_id=5),
        headers={"Mcp-Session-Id": "sess-from-client", "X-Forwarded-For": "6.6.6.6"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": 5, "result": {"tools": [{"name": "Snapshot"}]}}
    assert resp.headers["mcp-session-id"] == "sess-json-41"

    [seen] = gw.machine.received
    headers = dict(seen["headers"])
    assert headers["authorization"] == MACHINE_KEY
    assert all(TOKEN not in value for _, value in seen["headers"]), "consumer token leaked upstream"
    assert headers["mcp-session-id"] == "sess-from-client"
    assert "x-forwarded-for" not in headers
    assert seen["query"] == "trace=1"
    assert json.loads(seen["body"]) == _tools_list(request_id=5)


def test_connection_listed_request_headers_are_not_forwarded(gw):
    resp = gw.call(
        "POST",
        f"/{MACHINE}/mcp",
        json_body=_tools_list(),
        headers={"Connection": "keep-alive, X-Hop-Secret", "X-Hop-Secret": "1", "TE": "trailers", "X-Keep": "yes"},
    )

    assert resp.status_code == 200
    [seen] = gw.machine.received
    names = {name for name, _ in seen["headers"]}
    assert "x-hop-secret" not in names
    assert "te" not in names
    assert "connection" not in names
    assert ("x-keep", "yes") in seen["headers"]  # an end-to-end header still passes


def test_upstream_hop_by_hop_headers_are_not_relayed(gw):
    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body={"jsonrpc": "2.0", "id": 1, "method": "test/hop-headers"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "keep-alive" not in resp.headers
    assert "x-upstream-hop" not in resp.headers
    assert "connection" not in resp.headers


def test_oversized_body_is_413_and_nothing_reaches_the_machine(gw, monkeypatch):
    from remoteos.gateway import app as gateway_app

    monkeypatch.setattr(gateway_app, "MAX_BODY_BYTES", 1024)
    big = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "FileUpload", "arguments": {"data_base64": "A" * 4096}},
    }

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=big)

    assert resp.status_code == 413
    assert gw.machine.received == []
    [entry] = read_access_log(gw.access_log_path)
    assert (entry["consumer"], entry["outcome"]) == (CONSUMER, "payload_too_large")


def test_oversized_chunked_body_without_content_length_is_413(gw, monkeypatch):
    """No Content-Length to check up front: the cap must hold while the body streams in."""
    from remoteos.gateway import app as gateway_app

    monkeypatch.setattr(gateway_app, "MAX_BODY_BYTES", 1024)

    async def chunked_body():
        for _ in range(8):
            yield b"A" * 512

    async def go():
        app, upstream = gw.build()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test") as c:
            resp = await c.post(f"/{MACHINE}/mcp", content=chunked_body(), headers={"Authorization": f"Bearer {TOKEN}"})
            sent_headers = resp.request.headers
        await upstream.aclose()
        return resp.status_code, sent_headers

    status, sent_headers = asyncio.run(go())
    assert "content-length" not in sent_headers  # really chunked
    assert status == 413
    assert gw.machine.received == []


def test_machine_rejecting_its_key_is_relayed_and_flagged_in_the_log(gw):
    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body={"jsonrpc": "2.0", "id": 1, "method": "test/reject-key"})

    assert resp.status_code == 401  # relayed unchanged — the proxy stays transparent
    [entry] = read_access_log(gw.access_log_path)
    assert (entry["consumer"], entry["status"], entry["outcome"]) == (CONSUMER, 401, "upstream_denied")


def test_non_ok_outcomes_are_warnings_and_ok_is_info(gw, caplog):
    caplog.set_level("INFO", logger="remoteos.gateway")

    gw.call("POST", f"/{MACHINE}/mcp", json_body={"jsonrpc": "2.0", "id": 1, "method": "test/reject-key"})
    gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list())

    levels = {
        outcome: r.levelno
        for r in caplog.records
        if r.name == "remoteos.gateway"
        for outcome in ("upstream_denied", "outcome=ok")
        if outcome in r.getMessage()
    }
    assert levels == {"upstream_denied": logging.WARNING, "outcome=ok": logging.INFO}


def test_consumer_supplied_rpc_and_tool_strings_never_reach_a_log_line_raw(gw, caplog):
    caplog.set_level("DEBUG", logger="remoteos.gateway")
    evil_method = {"jsonrpc": "2.0", "id": 1, "method": "x\n2026-10-14 FORGED INFO\x1b[31m"}
    evil_tool = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "Snap\nshot\x1b[0m"}}

    assert gw.call("POST", f"/{MACHINE}/mcp", json_body=evil_method).status_code == 200
    assert gw.call("POST", f"/{MACHINE}/mcp", json_body=evil_tool).status_code == 200

    gateway_records = [r for r in caplog.records if r.name == "remoteos.gateway"]
    assert len(gateway_records) >= 2
    for record in gateway_records:
        message = record.getMessage()
        assert "\n" not in message and "\x1b" not in message, message


def test_sse_response_is_relayed_intact_with_session_id(gw):
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "Snapshot", "arguments": {}}}

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=call)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["mcp-session-id"] == "sess-sse-42"
    assert resp.content == SSE_BODY


class _GatedStream(httpx2.AsyncByteStream):
    """Upstream body that yields part 1, then waits until the CONSUMER has received it."""

    def __init__(self, first: bytes, second: bytes | None, gate: asyncio.Event) -> None:
        self.first, self.second, self.gate = first, second, gate
        self.closed = False

    async def __aiter__(self):
        yield self.first
        await self.gate.wait()
        if self.second is None:  # an SSE stream that stays open until the client leaves
            await asyncio.Event().wait()
        yield self.second

    async def aclose(self) -> None:
        self.closed = True


class _GatedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, stream: _GatedStream) -> None:
        self.stream = stream

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream", "mcp-session-id": "sess-live"}, stream=self.stream
        )


def test_sse_is_streamed_chunk_by_chunk_not_buffered(gw):
    async def go():
        gate = asyncio.Event()
        stream = _GatedStream(b"data: one\n\n", b"data: two\n\n", gate)
        gw.upstream_transport = _GatedTransport(stream)
        app, upstream = gw.build()
        sent: list[dict] = []
        first_sent_at: list[datetime.datetime] = []

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body") == b"data: one\n\n":
                first_sent_at.append(datetime.datetime.now(datetime.timezone.utc))
                # only now may the machine produce part two (a little later, so the stream
                # demonstrably spans time — and the log must stamp its START, not its end)
                asyncio.get_running_loop().call_later(0.3, gate.set)

        # A buffering proxy would wait for the whole upstream body before sending part one,
        # so the gate would never open and this would time out.
        await asyncio.wait_for(_drive_asgi(app, json.dumps(_tools_list()).encode(), send, asyncio.Event()), 5)
        await upstream.aclose()
        return sent, stream, first_sent_at[0]

    sent, stream, first_sent_at = asyncio.run(go())
    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    assert (b"mcp-session-id", b"sess-live") in start["headers"]
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
    assert bodies == [b"data: one\n\n", b"data: two\n\n", b""]
    assert stream.closed
    [entry] = read_access_log(gw.access_log_path)
    assert datetime.datetime.fromisoformat(entry["ts"]) <= first_sent_at
    assert entry["duration_ms"] >= 300


def test_client_disconnect_closes_the_upstream_stream_and_is_logged(gw):
    async def go():
        gate = asyncio.Event()
        stream = _GatedStream(b"data: one\n\n", None, gate)  # never ends on its own
        gw.upstream_transport = _GatedTransport(stream)
        app, upstream = gw.build()
        disconnect = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body") == b"data: one\n\n":
                disconnect.set()  # the consumer goes away after the first event

        await asyncio.wait_for(_drive_asgi(app, json.dumps(_tools_list()).encode(), send, disconnect), 5)
        await upstream.aclose()
        return stream

    stream = asyncio.run(go())
    assert stream.closed
    [record] = read_access_log(gw.access_log_path)
    assert record["outcome"] == "client_disconnect"
    assert record["status"] == 200


def test_upstream_is_closed_even_when_the_consumer_vanishes_before_headers(gw):
    """The relay never starts here, so only the response's own cleanup can close upstream."""

    async def go():
        stream = _GatedStream(b"data: one\n\n", b"data: two\n\n", asyncio.Event())
        gw.upstream_transport = _GatedTransport(stream)
        app, upstream = gw.build()

        async def send(message):
            if message["type"] == "http.response.start":
                raise OSError("client connection reset")

        with pytest.raises(OSError):
            await asyncio.wait_for(_drive_asgi(app, json.dumps(_tools_list()).encode(), send, asyncio.Event()), 5)
        await upstream.aclose()
        return stream

    assert asyncio.run(go()).closed
    [record] = read_access_log(gw.access_log_path)
    assert record["consumer"] == CONSUMER


def test_mid_stream_machine_failure_aborts_the_response_instead_of_a_clean_end(gw):
    async def go():
        gw.upstream_transport = _BreakingTransport()
        app, upstream = gw.build()
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        with pytest.raises(httpx2.ReadError):  # propagates, so the server aborts the connection
            await asyncio.wait_for(_drive_asgi(app, json.dumps(_tools_list()).encode(), send, asyncio.Event()), 5)
        await upstream.aclose()
        return sent

    sent = asyncio.run(go())
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert [m["body"] for m in bodies] == [b"data: one\n\n"]
    assert all(m.get("more_body") for m in bodies), "a clean end-of-stream was sent for a broken upstream"
    [record] = read_access_log(gw.access_log_path)
    assert record["outcome"] == "upstream_stream_error"


# -- failure handling -----------------------------------------------------------------


def test_machine_down_returns_503_jsonrpc_error_naming_the_machine(gw):
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("[Errno 111] Connection refused", request=request)

    gw.upstream_transport = httpx2.MockTransport(refuse)
    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list(request_id=9))

    assert resp.status_code == 503
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 9
    assert body["error"]["code"] == -32000
    assert MACHINE in body["error"]["message"]
    [record] = read_access_log(gw.access_log_path)
    assert (record["status"], record["outcome"]) == (503, "upstream_unreachable")


def test_exposed_machine_missing_from_machine_source_is_503(gw):
    _machines_file(gw.machines_path, [OTHER_MACHINE])  # MACHINE vanished from the master config

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list())

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == -32000
    assert gw.machine.received == []


# -- access log -----------------------------------------------------------------------


def test_access_log_has_tool_name_and_never_token_key_or_arguments(gw):
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "Shell", "arguments": {"command": "echo ARG-SECRET-do-not-log"}},
    }

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=call)
    gw.call("POST", f"/{MACHINE}/mcp", token=WRONG_TOKEN, json_body=call)

    assert resp.status_code == 200
    text = gw.access_log_path.read_text(encoding="utf-8")
    ok, denied = [json.loads(line) for line in text.splitlines()]
    assert ok["consumer"] == CONSUMER
    assert ok["machine"] == MACHINE
    assert ok["rpc_method"] == "tools/call"
    assert ok["tool"] == "Shell"
    assert ok["status"] == 200
    assert ok["outcome"] == "ok"
    assert isinstance(ok["duration_ms"], float)
    assert (denied["status"], denied["consumer"], denied["outcome"]) == (401, None, "denied_unknown_token")
    for secret in (TOKEN, WRONG_TOKEN, MACHINE_KEY_VALUE, "ARG-SECRET", "arguments"):
        assert secret not in text
    assert stat.S_IMODE(os.stat(gw.access_log_path).st_mode) == 0o600


def test_access_log_rotates_past_max_bytes(tmp_path):
    log = AccessLog(tmp_path / "state" / "access.jsonl", max_bytes=200)
    for i in range(10):
        log.write({"consumer": "c", "machine": "m", "i": i, "pad": "x" * 40})

    assert (tmp_path / "state" / "access.jsonl.1").exists()
    tail = read_access_log(tmp_path / "state" / "access.jsonl", tail=3)
    assert [r["i"] for r in tail] == [7, 8, 9]


# -- reload ---------------------------------------------------------------------------


def test_registry_reload_picks_up_a_revoke_without_restart(gw):
    async def go():
        app, upstream = gw.build()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test") as c:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            before = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
            result = CliRunner().invoke(
                gateway_cli.main,
                [
                    "--registry",
                    str(gw.registry_path),
                    "--machines",
                    str(gw.machines_path),
                    "consumer",
                    "revoke",
                    CONSUMER,
                ],
            )
            assert result.exit_code == 0, result.output
            after = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
        await upstream.aclose()
        return before.status_code, after.status_code

    assert asyncio.run(go()) == (200, 401)


def test_invalid_registry_at_reload_keeps_the_last_good_one(gw):
    async def go():
        app, upstream = gw.build()
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test") as c:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            gw.registry_path.write_text('{"exposed": "not-a-list"', encoding="utf-8")
            broken = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
            gw.registry_path.write_bytes(b"\xff\xfe not utf-8")
            undecodable = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
            gw.registry_path.write_text("[" * 100_000, encoding="utf-8")  # pathologically nested
            nested = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
            gw.machines_path.write_bytes(b"\xff not utf-8 either")
            machines_broken = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
            gw.registry_path.unlink()  # deleted registry = empty = deny all (never allow all)
            deleted = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
        await upstream.aclose()
        return [r.status_code for r in (broken, undecodable, nested, machines_broken, deleted)]

    assert asyncio.run(go()) == [200, 200, 200, 200, 401]


def test_undecodable_files_refuse_startup_with_a_clear_error(gw):
    gw.registry_path.write_bytes(b"\xff\xfe")
    with pytest.raises(RegistryError, match="cannot read registry"):
        LiveConfig(gw.registry_path, gw.machines_path)

    _write_json(gw.registry_path, _default_registry())
    gw.machines_path.write_bytes(b"\xff\xfe")
    with pytest.raises(MachineSourceError, match="cannot read machine source"):
        LiveConfig(gw.registry_path, gw.machines_path)


def test_sighup_forces_a_reload_even_when_the_file_signature_is_unchanged(gw):
    """Same size + restored mtime + same inode: only SIGHUP can notice the edit."""
    registry = _default_registry()
    registry["consumers"][CONSUMER]["sha256"] = ["0" * 64]  # same length, revokes TOKEN
    st = os.stat(gw.registry_path)

    async def go():
        app, upstream = gw.build()
        auth = {"Authorization": f"Bearer {TOKEN}"}
        async with app.router.lifespan_context(app):
            assert signal.getsignal(signal.SIGHUP) not in (signal.SIG_DFL, None), "SIGHUP handler not installed"
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test") as c:
                with open(gw.registry_path, "r+", encoding="utf-8") as handle:  # in place: same inode
                    handle.write(json.dumps(registry))
                    handle.truncate()
                os.utime(gw.registry_path, ns=(st.st_atime_ns, st.st_mtime_ns))
                unchanged = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
                os.kill(os.getpid(), signal.SIGHUP)
                await asyncio.sleep(0.05)  # let the loop run the signal handler
                reloaded = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
        return unchanged.status_code, reloaded.status_code

    assert os.path.getsize(gw.registry_path) == len(json.dumps(registry))
    assert asyncio.run(go()) == (200, 401)


# -- registry validation --------------------------------------------------------------


def test_registry_rejects_the_same_hash_for_two_consumers():
    digest = token_sha256(TOKEN)
    with pytest.raises(RegistryError, match="registered for both"):
        Registry.from_json(
            {
                "exposed": [],
                "consumers": {"a": {"sha256": [digest], "machines": []}, "b": {"sha256": [digest], "machines": []}},
            }
        )


def test_registry_rejects_unknown_keys_and_uppercase_hashes():
    with pytest.raises(RegistryError, match="unknown top-level"):
        Registry.from_json({"exposd": []})
    with pytest.raises(RegistryError, match="sha256"):
        Registry.from_json({"consumers": {"a": {"sha256": ["A" * 64]}}})


@pytest.mark.parametrize("name", ["a\n", "a\nb", "", "-lead", "has space"])
def test_registry_rejects_consumer_names_that_are_not_a_full_match(name):
    with pytest.raises(RegistryError, match="invalid consumer name"):
        Registry.from_json({"consumers": {name: {"sha256": [], "machines": []}}})
    with pytest.raises(RegistryError, match="invalid consumer name"):
        Registry().add_consumer(name, token_sha256(TOKEN))


# -- CLI ------------------------------------------------------------------------------


def _cli(tmp_path, *args):
    paths = [
        "--registry",
        str(tmp_path / "cfg" / "registry.json"),
        "--machines",
        str(tmp_path / "claude" / "remoteos-mcp.mcp.json"),
        "--access-log",
        str(tmp_path / "state" / "access.jsonl"),
    ]
    return CliRunner().invoke(gateway_cli.main, [*paths, *args])


@pytest.mark.parametrize(
    "value",
    ["abc", TOKEN, "g" * 64, "0" * 63, "0" * 65, "sha256:" + "0" * 64],
    ids=["short", "raw-token", "64-non-hex", "63-hex", "65-hex", "prefixed"],
)
def test_consumer_add_rejects_anything_but_a_64_hex_digest(tmp_path, value):
    result = _cli(tmp_path, "consumer", "add", CONSUMER, "--sha256", value)

    assert result.exit_code != 0
    assert "64 hex" in result.output
    assert not (tmp_path / "cfg" / "registry.json").exists()


def test_cli_full_management_flow(tmp_path):
    _machines_file(tmp_path / "claude" / "remoteos-mcp.mcp.json", [MACHINE])
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()

    assert _cli(tmp_path, "consumer", "add", CONSUMER, "--sha256", digest.upper()).exit_code == 0
    assert _cli(tmp_path, "expose", MACHINE).exit_code == 0
    assert _cli(tmp_path, "allow", CONSUMER, MACHINE).exit_code == 0
    typo = _cli(tmp_path, "expose", "win-typo")
    assert typo.exit_code != 0 and "not in the machine source" in typo.output

    registry_path = tmp_path / "cfg" / "registry.json"
    reg = load_registry(registry_path)
    assert reg.exposed == [MACHINE]
    assert reg.consumers[CONSUMER].sha256 == [digest]  # normalised to lowercase
    assert reg.consumers[CONSUMER].machines == [MACHINE]
    assert stat.S_IMODE(os.stat(registry_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(registry_path.parent).st_mode) == 0o700

    listing = _cli(tmp_path, "list")
    assert listing.exit_code == 0
    assert f"{MACHINE}  ->  {UPSTREAM_URL}" in listing.output
    assert digest[:12] in listing.output and digest not in listing.output
    assert MACHINE_KEY_VALUE not in listing.output

    assert _cli(tmp_path, "deny", CONSUMER, MACHINE).exit_code == 0
    assert load_registry(registry_path).consumers[CONSUMER].machines == []
    assert _cli(tmp_path, "unexpose", MACHINE).exit_code == 0
    assert load_registry(registry_path).exposed == []
    revoke = _cli(tmp_path, "consumer", "revoke", CONSUMER, "--sha256", digest)
    assert revoke.exit_code == 0
    assert load_registry(registry_path).consumers[CONSUMER].sha256 == []


def test_cli_log_never_echoes_raw_control_characters(tmp_path):
    AccessLog(tmp_path / "state" / "access.jsonl").write(
        {"ts": "t1", "consumer": None, "machine": "x\x1b[31mRED\nFORGED", "http_method": "POST", "status": 404}
    )

    result = _cli(tmp_path, "log")

    assert result.exit_code == 0
    assert "\x1b" not in result.output
    assert "RED\nFORGED" not in result.output
    assert "RED\\nFORGED" in result.output  # shown escaped, still readable


def test_cli_log_filters_by_consumer(tmp_path):
    log = AccessLog(tmp_path / "state" / "access.jsonl")
    log.write(
        {
            "ts": "t1",
            "consumer": CONSUMER,
            "machine": MACHINE,
            "http_method": "POST",
            "rpc_method": "tools/call",
            "tool": "Snapshot",
            "status": 200,
            "outcome": "ok",
            "duration_ms": 1.0,
        }
    )
    log.write(
        {
            "ts": "t2",
            "consumer": OTHER_CONSUMER,
            "machine": OTHER_MACHINE,
            "http_method": "POST",
            "rpc_method": "tools/list",
            "tool": None,
            "status": 200,
            "outcome": "ok",
            "duration_ms": 1.0,
        }
    )

    result = _cli(tmp_path, "log", "--consumer", CONSUMER)

    assert result.exit_code == 0
    assert "tools/call(Snapshot)" in result.output
    assert OTHER_CONSUMER not in result.output


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "0", "::ffff:0.0.0.0"])
def test_serve_refuses_a_wildcard_bind(tmp_path, monkeypatch, host):
    import uvicorn

    def must_not_run(*args, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("uvicorn.run reached despite a wildcard host")

    monkeypatch.setattr(uvicorn, "run", must_not_run)

    result = _cli(tmp_path, "serve", "--host", host)

    assert result.exit_code == 2
    assert "refusing to bind" in result.output


def test_serve_refuses_to_start_on_an_invalid_registry(tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    _machines_file(tmp_path / "claude" / "remoteos-mcp.mcp.json", [MACHINE])
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "registry.json").write_text("{broken", encoding="utf-8")

    result = _cli(tmp_path, "serve", "--host", "127.0.0.1")

    assert result.exit_code == 1
    assert "refusing to start" in result.output


def test_healthz_is_unauthenticated_and_reports_only_the_version(gw):
    resp = gw.call("GET", "/healthz", token=None)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}
