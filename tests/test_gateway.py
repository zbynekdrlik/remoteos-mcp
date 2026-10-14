"""Tests for the remoteos MCP gateway (#25).

Hermetic: the "machine" is an in-process Starlette app reached through
``httpx2.ASGITransport`` (or a transport that simulates a dead machine / a slow stream);
the consumer side talks to the gateway app in-process as well. No network, no real keys,
and every file lives under ``tmp_path`` — the real registry / ~/.claude are never touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import signal
import stat
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
import pytest
from click.testing import CliRunner
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from remoteos import __version__
from remoteos.gateway import cli as gateway_cli
from remoteos.gateway.accesslog import AccessLog, read_access_log
from remoteos.gateway.app import create_app
from remoteos.gateway.registry import (
    LiveConfig,
    Registry,
    RegistryError,
    edit_registry,
    load_registry,
    token_sha256,
)

MACHINE = "win-test-pokladna"
OTHER_MACHINE = "win-other-pc"
CONSUMER = "david3@subdev"
OTHER_CONSUMER = "miva1@subdev"
# Throwaway fixture credentials, generated per test run (no literal secret-shaped values).
TOKEN = "consumer-" + secrets.token_hex(16)
OTHER_TOKEN = "other-" + secrets.token_hex(16)
WRONG_TOKEN = "unregistered-" + secrets.token_hex(16)
MACHINE_KEY_VALUE = "machinekey-" + secrets.token_hex(16)
MACHINE_KEY = f"Bearer {MACHINE_KEY_VALUE}"
UPSTREAM_URL = "http://machine.test:8092/mcp"
SSE_BODY = (
    b"event: message\r\ndata: "
    b'{"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"part one"}]}}\r\n\r\n'
    b"event: message\r\ndata: "
    b'{"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\r\n\r\n'
)


@pytest.fixture(autouse=True)
def _isolate_default_paths(tmp_path, monkeypatch):
    """Any CLI call that forgets an explicit path still lands in tmp, never the real home."""
    monkeypatch.setenv("REMOTEOS_GATEWAY_REGISTRY", str(tmp_path / "env-default" / "registry.json"))
    monkeypatch.setenv("REMOTEOS_GATEWAY_MACHINES", str(tmp_path / "env-default" / "machines.json"))
    monkeypatch.setenv("REMOTEOS_GATEWAY_ACCESS_LOG", str(tmp_path / "env-default" / "access.jsonl"))


# -- fake machine ---------------------------------------------------------------------


@dataclass
class FakeMachine:
    """An in-process stand-in for a remoteos-mcp machine's /mcp endpoint."""

    received: list[dict] = field(default_factory=list)

    def app(self) -> Starlette:
        async def mcp(request: Request):
            body = await request.body()
            self.received.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "headers": list(request.headers.items()),
                    "body": body,
                }
            )
            message = json.loads(body) if body else {}
            if message.get("method") == "tools/call":
                return StreamingResponse(
                    _chunks(SSE_BODY, 40),
                    media_type="text/event-stream",
                    headers={"mcp-session-id": "sess-sse-42"},
                )
            return JSONResponse(
                {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": [{"name": "Snapshot"}]}},
                headers={"mcp-session-id": "sess-json-41"},
            )

        return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])])


async def _chunks(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _machines_file(path: Path, names: list[str]) -> Path:
    _write_json(
        path,
        {
            "mcpServers": {
                n: {"type": "http", "url": UPSTREAM_URL, "headers": {"Authorization": MACHINE_KEY}} for n in names
            }
        },
    )
    return path


def _default_registry() -> dict:
    return {
        "exposed": [MACHINE, OTHER_MACHINE],
        "consumers": {
            CONSUMER: {"sha256": [token_sha256(TOKEN)], "machines": [MACHINE], "added": "2026-10-14"},
            OTHER_CONSUMER: {"sha256": [token_sha256(OTHER_TOKEN)], "machines": [OTHER_MACHINE], "added": "2026-10-14"},
        },
    }


@dataclass
class Harness:
    tmp: Path
    machine: FakeMachine
    registry_path: Path
    machines_path: Path
    access_log_path: Path
    upstream_transport: httpx2.AsyncBaseTransport | None = None

    def build(self):
        config = LiveConfig(self.registry_path, self.machines_path)
        transport = self.upstream_transport or httpx2.ASGITransport(app=self.machine.app())
        upstream = httpx2.AsyncClient(transport=transport)
        app = create_app(config, AccessLog(self.access_log_path), upstream_client=upstream)
        return app, upstream

    def call(self, method: str, path: str, *, token: str | None = TOKEN, json_body=None, headers=None):
        """One request through a freshly built gateway; returns the consumer's response."""
        responses = self.calls([(method, path, token, json_body, headers)])
        return responses[0]

    def calls(self, requests: list[tuple]):
        async def go():
            app, upstream = self.build()
            out = []
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test") as c:
                for method, path, token, json_body, headers in requests:
                    hdrs = dict(headers or {})
                    if token is not None:
                        hdrs["Authorization"] = f"Bearer {token}"
                    resp = await c.request(method, path, json=json_body, headers=hdrs)
                    await resp.aread()
                    out.append(resp)
            await upstream.aclose()
            return out

        return asyncio.run(go())


@pytest.fixture
def gw(tmp_path) -> Harness:
    h = Harness(
        tmp=tmp_path,
        machine=FakeMachine(),
        registry_path=tmp_path / "cfg" / "registry.json",
        machines_path=tmp_path / "claude" / "remoteos-mcp.mcp.json",
        access_log_path=tmp_path / "state" / "access.jsonl",
    )
    _write_json(h.registry_path, _default_registry())
    _machines_file(h.machines_path, [MACHINE, OTHER_MACHINE])
    return h


def _tools_list(request_id=1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}


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


def test_unexposed_machine_is_404(gw):
    with edit_registry(gw.registry_path) as reg:
        reg.unexpose(MACHINE)  # CONSUMER is still allowed MACHINE — the kill switch wins

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list())

    assert resp.status_code == 404
    assert gw.machine.received == []


def test_unknown_path_is_404_without_auth(gw):
    assert gw.call("GET", "/nope", token=None).status_code == 404


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


def _drive_asgi(app, body: bytes, on_send, disconnect: asyncio.Event):
    """Call the gateway ASGI app directly so each body message can be observed as sent."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/{MACHINE}/mcp",
        "raw_path": f"/{MACHINE}/mcp".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway.test"),
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
        ],
        "client": ("100.64.0.9", 51515),
        "server": ("100.104.8.125", 8796),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if pending:
            return pending.pop(0)
        await disconnect.wait()
        return {"type": "http.disconnect"}

    return app(scope, receive, on_send)


def test_sse_is_streamed_chunk_by_chunk_not_buffered(gw):
    async def go():
        gate = asyncio.Event()
        stream = _GatedStream(b"data: one\n\n", b"data: two\n\n", gate)
        gw.upstream_transport = _GatedTransport(stream)
        app, upstream = gw.build()
        sent: list[dict] = []

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body") == b"data: one\n\n":
                gate.set()  # only now may the machine produce part two

        # A buffering proxy would wait for the whole upstream body before sending part one,
        # so the gate would never open and this would time out.
        await asyncio.wait_for(_drive_asgi(app, json.dumps(_tools_list()).encode(), send, asyncio.Event()), 5)
        await upstream.aclose()
        return sent, stream

    sent, stream = asyncio.run(go())
    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    assert (b"mcp-session-id", b"sess-live") in start["headers"]
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
    assert bodies == [b"data: one\n\n", b"data: two\n\n", b""]
    assert stream.closed


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
            gw.registry_path.unlink()  # deleted registry = empty = deny all (never allow all)
            deleted = await c.post(f"/{MACHINE}/mcp", json=_tools_list(), headers=auth)
        await upstream.aclose()
        return broken.status_code, deleted.status_code

    assert asyncio.run(go()) == (200, 401)


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
    assert "machine-key" not in listing.output

    assert _cli(tmp_path, "deny", CONSUMER, MACHINE).exit_code == 0
    assert load_registry(registry_path).consumers[CONSUMER].machines == []
    assert _cli(tmp_path, "unexpose", MACHINE).exit_code == 0
    assert load_registry(registry_path).exposed == []
    revoke = _cli(tmp_path, "consumer", "revoke", CONSUMER, "--sha256", digest)
    assert revoke.exit_code == 0
    assert load_registry(registry_path).consumers[CONSUMER].sha256 == []


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


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "0"])
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
