"""Gateway ↔ machine connection reuse: stale keep-alive connections (#25 reopened).

The reopened #25 bug: a call after ~4.9 s idle got 503 ``upstream_unreachable``
(``RemoteProtocolError: Server disconnected without sending a response``). Some tests here
drive the gateway's PRODUCTION client (built by ``create_app``, no injected client) against a
real loopback socket, so the real httpcore connection pool is what is under test.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx2
import pytest
from gateway_harness import (
    CONSUMER,
    MACHINE,
    MACHINE_KEY,
    MACHINE_KEY_VALUE,
    SSE_BODY,
    TOKEN,
    _BreakingTransport,
    _drive_asgi,
    _tools_list,
    _write_json,
)

from remoteos.gateway import app as gateway_app
from remoteos.gateway.accesslog import AccessLog, read_access_log
from remoteos.gateway.app import create_app
from remoteos.gateway.registry import LiveConfig

# -- stale keep-alive connections (#25 reopened) ---------------------------------------
#
# The machines run uvicorn with its default timeout_keep_alive of 5 s. When the gateway reuses
# a pooled connection just as the machine closes it, the request is written to a socket the
# machine has already given up on: the machine never reads it, and httpcore raises
# RemoteProtocolError("Server disconnected without sending a response.").


class _LoopbackMachine:
    """A REAL loopback HTTP/1.1 server — the gateway's own connection pool talks to it.

    ``drop`` decides, per request, whether the machine "closes the connection instead of
    answering": the request is read off the socket and the connection is closed with no
    response. That is what a client sees when uvicorn's keep-alive deadline fires as a request
    arrives on an idle connection. A dropped request was never answered, i.e. never handed to
    a tool, so it is not counted in ``answered``.
    """

    def __init__(self, drop: str = "never") -> None:
        assert drop in ("never", "on_reuse", "always")
        self.drop = drop
        self.connections = 0
        self.dropped = 0
        self.answered: list[dict] = []

    def _should_drop(self, served_on_this_connection: int) -> bool:
        if self.drop == "always":
            return True
        return self.drop == "on_reuse" and served_on_this_connection > 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        served = 0
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                message = json.loads(await reader.readexactly(length)) if length else {}
                if self._should_drop(served):
                    self.dropped += 1
                    return  # close without a response (the whole request was read: FIN, not RST)
                served += 1
                self.answered.append(message)
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": [{"name": "Snapshot"}]}}
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                    + f"content-length: {len(payload)}\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return  # the client closed the connection
        finally:
            writer.close()


def _through_real_pool(gw, machine: _LoopbackMachine, idle_gaps: list[float]) -> list:
    """One gateway built by ``create_app`` WITHOUT an injected client (the production client and
    its connection pool), in front of *machine*; one call per entry in *idle_gaps*, each made
    after that many seconds of idle."""

    async def go():
        server = await asyncio.start_server(machine.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _write_json(
            gw.machines_path,
            {
                "mcpServers": {
                    MACHINE: {
                        "type": "http",
                        "url": f"http://127.0.0.1:{port}/mcp",
                        "headers": {"Authorization": MACHINE_KEY},
                    }
                }
            },
        )
        app = create_app(LiveConfig(gw.registry_path, gw.machines_path), AccessLog(gw.access_log_path))
        responses = []
        try:
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gateway.test") as c:
                for i, idle in enumerate(idle_gaps):
                    await asyncio.sleep(idle)
                    resp = await c.post(
                        f"/{MACHINE}/mcp",
                        json=_tools_list(request_id=i + 1),
                        headers={"Authorization": f"Bearer {TOKEN}"},
                    )
                    await resp.aread()
                    responses.append(resp)
        finally:
            await app.state.gateway.client.aclose()
            server.close()
            await asyncio.wait_for(server.wait_closed(), 5)
        return responses

    return asyncio.run(go())


def test_machine_closing_a_pooled_connection_as_the_request_arrives_is_retried_not_503(gw, caplog):
    """The reported bug: 503 upstream_unreachable after ~4.9 s idle (david3@subdev, 17:42:09)."""
    caplog.set_level("INFO", logger="remoteos.gateway")
    machine = _LoopbackMachine(drop="on_reuse")

    first, second = _through_real_pool(gw, machine, [0, 0])

    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert second.json()["id"] == 2
    # the second call went out on the pooled connection, was dropped unread, and was sent once
    # more on a NEW connection — where the machine answered it exactly once
    assert (machine.connections, machine.dropped) == (2, 1)
    assert [m["id"] for m in machine.answered] == [1, 2]
    entries = read_access_log(gw.access_log_path)
    assert [(e["status"], e["outcome"]) for e in entries] == [(200, "ok"), (200, "ok")]
    retries = [r for r in caplog.records if r.name == "remoteos.gateway" and "retrying" in r.getMessage()]
    assert len(retries) == 1 and retries[0].levelno == logging.INFO


def test_a_failure_on_a_freshly_opened_connection_is_not_retried(gw):
    """A machine that drops a request on a connection opened FOR it may have run the call:
    only a reused pooled connection is the stale-keep-alive case."""
    machine = _LoopbackMachine(drop="always")

    [resp] = _through_real_pool(gw, machine, [0])

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == -32000
    assert (machine.connections, machine.dropped) == (1, 1)
    [entry] = read_access_log(gw.access_log_path)
    assert (entry["status"], entry["outcome"]) == (503, "upstream_unreachable")


def test_idle_pooled_connections_expire_well_before_the_machines_keep_alive():
    assert gateway_app.UPSTREAM_LIMITS.keepalive_expiry == 2.0
    # the other two limits stay at httpx2's defaults — only the expiry changes
    assert gateway_app.UPSTREAM_LIMITS.max_connections == 100
    assert gateway_app.UPSTREAM_LIMITS.max_keepalive_connections == 20


def test_the_production_client_drops_a_pooled_connection_after_2s_idle(gw):
    machine = _LoopbackMachine(drop="never")

    responses = _through_real_pool(gw, machine, [0, 1.0, 2.4])

    assert [r.status_code for r in responses] == [200, 200, 200]
    # 1.0 s idle: still pooled (same connection); 2.4 s idle: expired, a new connection
    assert machine.connections == 2
    assert machine.dropped == 0


class _FailingTransport(httpx2.AsyncBaseTransport):
    """Fails the first *failures* attempts before any response, then delegates to *inner*."""

    def __init__(self, exc_type: type[httpx2.RequestError], inner: httpx2.AsyncBaseTransport, failures: int) -> None:
        self.exc_type, self.inner, self.failures = exc_type, inner, failures
        self.attempts = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc_type(f"simulated {self.exc_type.__name__} #{self.attempts}", request=request)
        return await self.inner.handle_async_request(request)


@pytest.mark.parametrize("exc_type", [httpx2.RemoteProtocolError, httpx2.ReadError, httpx2.WriteError])
def test_one_stale_connection_failure_before_the_response_is_retried_once(gw, caplog, exc_type):
    caplog.set_level("DEBUG", logger="remoteos.gateway")
    transport = _FailingTransport(exc_type, httpx2.ASGITransport(app=gw.machine.app()), failures=1)
    gw.upstream_transport = transport
    call = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "Shell", "arguments": {"command": "echo ARG-SECRET-do-not-log"}},
    }

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=call)

    assert resp.status_code == 200
    assert resp.content == SSE_BODY
    assert transport.attempts == 2
    [seen] = gw.machine.received  # the tool ran exactly once
    assert dict(seen["headers"])["authorization"] == MACHINE_KEY  # the retry carries the machine key
    assert json.loads(seen["body"]) == call  # ... and the full original body
    [entry] = read_access_log(gw.access_log_path)
    assert (entry["status"], entry["outcome"], entry["tool"]) == (200, "ok", "Shell")
    [retry] = [r for r in caplog.records if r.name == "remoteos.gateway" and "retrying" in r.getMessage()]
    message = retry.getMessage()
    assert retry.levelno == logging.INFO
    assert all(part in message for part in (repr(MACHINE), CONSUMER, "'tools/call'", "'Shell'", exc_type.__name__))
    for secret in (TOKEN, MACHINE_KEY_VALUE, "ARG-SECRET"):
        assert secret not in message


def test_two_consecutive_stale_connection_failures_are_503_after_exactly_one_retry(gw):
    transport = _FailingTransport(httpx2.RemoteProtocolError, httpx2.ASGITransport(app=gw.machine.app()), failures=2)
    gw.upstream_transport = transport

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list(request_id=4))

    assert resp.status_code == 503
    assert resp.json()["id"] == 4
    assert "RemoteProtocolError" in resp.json()["error"]["message"]
    assert transport.attempts == 2
    assert gw.machine.received == []
    [entry] = read_access_log(gw.access_log_path)
    assert (entry["status"], entry["outcome"]) == (503, "upstream_unreachable")


@pytest.mark.parametrize(
    "exc_type",
    [httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ReadTimeout, httpx2.WriteTimeout, httpx2.PoolTimeout],
)
def test_connect_errors_and_timeouts_are_never_retried(gw, exc_type):
    """A down machine must fail fast, and a ReadTimeout means the machine may be RUNNING the call."""
    transport = _FailingTransport(exc_type, httpx2.ASGITransport(app=gw.machine.app()), failures=1)
    gw.upstream_transport = transport

    resp = gw.call("POST", f"/{MACHINE}/mcp", json_body=_tools_list())

    assert resp.status_code == 503
    assert transport.attempts == 1
    assert gw.machine.received == []


class _CountingBreakingTransport(_BreakingTransport):
    def __init__(self) -> None:
        self.attempts = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.attempts += 1
        return await super().handle_async_request(request)


def test_a_failure_after_the_response_started_is_never_retried(gw):
    async def go():
        transport = _CountingBreakingTransport()
        gw.upstream_transport = transport
        app, upstream = gw.build()

        async def send(message):
            pass

        with pytest.raises(httpx2.ReadError):  # still aborts, exactly as before
            await asyncio.wait_for(_drive_asgi(app, json.dumps(_tools_list()).encode(), send, asyncio.Event()), 5)
        await upstream.aclose()
        return transport.attempts

    assert asyncio.run(go()) == 1
    [record] = read_access_log(gw.access_log_path)
    assert record["outcome"] == "upstream_stream_error"
