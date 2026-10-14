"""Shared harness for the gateway tests (#25): fixture constants, the fake machine, Harness.

Hermetic: the "machine" is an in-process Starlette app reached through
``httpx2.ASGITransport`` (or a transport that simulates a dead machine / a slow stream);
the consumer side talks to the gateway app in-process as well. No real keys, and every file
lives under ``tmp_path`` — the real registry / ~/.claude are never touched.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from remoteos.gateway.accesslog import AccessLog
from remoteos.gateway.app import create_app
from remoteos.gateway.registry import LiveConfig, token_sha256

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
            if message.get("method") == "test/reject-key":  # a machine whose key was rotated
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if message.get("method") == "test/hop-headers":
                return JSONResponse(
                    {"ok": True},
                    headers={"keep-alive": "timeout=5", "connection": "x-upstream-hop", "x-upstream-hop": "1"},
                )
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


def _tools_list(request_id=1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}


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


class _BreakingStream(httpx2.AsyncByteStream):
    """Upstream body that sends one event and then the machine's connection drops."""

    async def __aiter__(self):
        yield b"data: one\n\n"
        raise httpx2.ReadError("connection reset by machine")


class _BreakingTransport(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=_BreakingStream())
