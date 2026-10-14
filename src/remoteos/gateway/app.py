"""The gateway ASGI app — per-consumer auth in front of a transparent streaming proxy.

Routes:

- ``GET /healthz`` — unauthenticated, ``{"status": "ok", "version": ...}`` only.
- ``GET|POST|DELETE /{machine}/mcp`` — the MCP streamable-HTTP endpoint of *machine*.

Decision order for ``/{machine}/mcp`` (the design's numbered order):

1. bearer token missing, or its sha256 matches no registered consumer → **401**
2. consumer known, *machine* not in its allow-list                    → **403**
3. machine allowed but not exposed (the ``unexpose`` kill switch)      → **404**
4. otherwise proxy: the consumer's ``Authorization`` is REPLACED by the machine key from the
   master MCP config and the upstream response (JSON or ``text/event-stream``) is streamed
   back byte-for-byte with its headers (``Mcp-Session-Id`` included).

Upstream unreachable (connect error / timeout / any transport error before headers) → **503**
with a JSON-RPC ``-32000`` error naming the machine, so an MCP client stays connected.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import json
import logging
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from remoteos import __version__
from remoteos.gateway.accesslog import AccessLog
from remoteos.gateway.registry import LiveConfig

logger = logging.getLogger("remoteos.gateway")

# connect/pool: a machine that is down must fail fast into the 503 path.
# read: generous, between bytes — a long tool call (Wait, ScreenRecord) or an idle SSE stream
#   must not be cut by the gateway, but a hung machine cannot hold a connection forever.
UPSTREAM_TIMEOUT = httpx2.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

# JSON-RPC messages are small; FileUpload carries base64 content, so the cap is generous.
MAX_BODY_BYTES = 64 * 1024 * 1024

_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)
# Never forwarded upstream: the consumer's own credential, values httpx recomputes, and
# client-controlled forwarding headers (the machine sees dev1 as the client, nothing else).
_STRIP_REQUEST = _HOP_BY_HOP | {
    b"host",
    b"authorization",
    b"content-length",
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-port",
    b"x-forwarded-proto",
    b"x-real-ip",
}
_STRIP_RESPONSE = _HOP_BY_HOP


def _connection_tokens(raw_headers: list[tuple[bytes, bytes]]) -> set[bytes]:
    """Header names listed in ``Connection:`` — hop-by-hop per RFC 9110 §7.6.1."""
    tokens: set[bytes] = set()
    for name, value in raw_headers:
        if name.lower() == b"connection":
            tokens.update(t.strip().lower() for t in value.split(b",") if t.strip())
    return tokens


def forward_request_headers(
    raw_headers: list[tuple[bytes, bytes]], authorization: str | None
) -> list[tuple[bytes, bytes]]:
    """Consumer headers minus hop-by-hop/credential ones, plus the machine's ``Authorization``."""
    drop = _STRIP_REQUEST | _connection_tokens(raw_headers)
    headers = [(name.lower(), value) for name, value in raw_headers if name.lower() not in drop]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("latin-1")))
    return headers


def forward_response_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Upstream headers minus hop-by-hop ones. The body is relayed raw, so length/encoding hold."""
    drop = _STRIP_RESPONSE | _connection_tokens(raw_headers)
    return [(name.lower(), value) for name, value in raw_headers if name.lower() not in drop]


def bearer_token(header_value: str | None) -> str | None:
    if not header_value:
        return None
    scheme, _, token = header_value.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


@dataclass
class RpcSummary:
    """What the access log may know about a request body: method + tool name, never params."""

    method: str | None = None
    tool: str | None = None
    request_id: Any = None


def summarize_rpc(body: bytes) -> RpcSummary:
    if not body:
        return RpcSummary()
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return RpcSummary(method="<non-json>")
    if isinstance(message, dict):
        return _summarize_one(message)
    if isinstance(message, list):  # JSON-RPC batch (older MCP protocol revisions)
        parts = [_summarize_one(m) for m in message if isinstance(m, dict)]
        methods = [p.method for p in parts if p.method]
        tools = [p.tool for p in parts if p.tool]
        return RpcSummary(
            method="batch:" + ",".join(methods) if methods else "batch",
            tool=",".join(tools) or None,
        )
    return RpcSummary(method="<non-object>")


def _summarize_one(message: dict) -> RpcSummary:
    method = message.get("method")
    method = method if isinstance(method, str) else None
    tool = None
    if method == "tools/call":
        params = message.get("params")
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            tool = params["name"]
    request_id = message.get("id")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        request_id = None
    return RpcSummary(method=method, tool=tool, request_id=request_id)


def jsonrpc_error(status_code: int, message: str, request_id: Any = None) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": message}},
        status_code=status_code,
    )


@dataclass
class _Entry:
    """One request's access-log record, filled in as the request progresses."""

    machine: str
    http_method: str
    started: float
    consumer: str | None = None
    rpc_method: str | None = None
    tool: str | None = None
    status: int | None = None
    outcome: str = "client_disconnect"  # overwritten by every path that knows better
    recorded: bool = False


class _UpstreamResponse(StreamingResponse):
    """A StreamingResponse that ALWAYS closes the upstream stream and writes the log line.

    Starlette runs ``background`` only on a clean finish; wrapping ``__call__`` covers the
    client-disconnect and error paths too.
    """

    def __init__(self, upstream: httpx2.Response, body: AsyncIterator[bytes], on_done) -> None:
        super().__init__(body, status_code=upstream.status_code)
        self.raw_headers = forward_response_headers(upstream.headers.raw)
        self._upstream = upstream
        self._on_done = on_done

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_done()
            await self._upstream.aclose()


class Gateway:
    def __init__(self, config: LiveConfig, access_log: AccessLog, client: httpx2.AsyncClient) -> None:
        self.config = config
        self.access_log = access_log
        self.client = client

    async def handle(self, request: Request) -> Response:
        entry = _Entry(machine=request.path_params["machine"], http_method=request.method, started=time.monotonic())
        registry, machines = self.config.snapshot()

        token = bearer_token(request.headers.get("authorization"))
        consumer = registry.authenticate(token) if token else None
        if consumer is None:
            outcome = "denied_unknown_token" if token else "denied_missing_token"
            return self._deny(entry, 401, outcome, {"error": "Unauthorized"})
        entry.consumer = consumer

        if entry.machine not in registry.consumers[consumer].machines:
            return self._deny(
                entry, 403, "denied_not_allowed", {"error": "Forbidden: consumer is not allowed this machine"}
            )
        if entry.machine not in registry.exposed:
            return self._deny(entry, 404, "denied_unexposed", {"error": "Not Found"})

        body = await _read_body(request)
        if body is None:
            return self._deny(
                entry, 413, "payload_too_large", {"error": f"Payload Too Large (max {MAX_BODY_BYTES} bytes)"}
            )
        rpc = summarize_rpc(body)
        entry.rpc_method, entry.tool = rpc.method, rpc.tool

        upstream = machines.get(entry.machine)
        if upstream is None:
            logger.error(
                "machine %r is exposed and allowed for %r but missing from the machine source %s",
                entry.machine,
                consumer,
                self.config.machines_path,
            )
            entry.status, entry.outcome = 503, "upstream_not_configured"
            self._record(entry)
            return jsonrpc_error(503, f"Machine '{entry.machine}' is not configured on the gateway", rpc.request_id)

        url = upstream.url
        if request.url.query:
            url = f"{url}?{request.url.query}"
        upstream_request = httpx2.Request(
            request.method,
            url,
            headers=forward_request_headers(request.headers.raw, upstream.authorization),
            content=body or None,
        )
        logger.debug(
            "forwarding consumer=%s machine=%s %s rpc=%s tool=%s body=%dB",
            consumer,
            entry.machine,
            request.method,
            rpc.method,
            rpc.tool,
            len(body),
        )
        try:
            upstream_response = await self.client.send(upstream_request, stream=True)
        except httpx2.RequestError as exc:
            logger.warning(
                "machine %r unreachable (consumer=%s rpc=%s tool=%s): %s: %s",
                entry.machine,
                consumer,
                rpc.method,
                rpc.tool,
                type(exc).__name__,
                exc,
            )
            entry.status, entry.outcome = 503, "upstream_unreachable"
            self._record(entry)
            return jsonrpc_error(
                503,
                f"Machine '{entry.machine}' is unreachable through the gateway ({type(exc).__name__})",
                rpc.request_id,
            )

        entry.status = upstream_response.status_code
        return _UpstreamResponse(
            upstream_response,
            self._relay(upstream_response, entry),
            on_done=functools.partial(self._record, entry),
        )

    async def _relay(self, upstream_response: httpx2.Response, entry: _Entry) -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        except httpx2.RequestError as exc:
            entry.outcome = "upstream_stream_error"
            logger.warning(
                "machine %r stream broke mid-response (consumer=%s rpc=%s): %s: %s",
                entry.machine,
                entry.consumer,
                entry.rpc_method,
                type(exc).__name__,
                exc,
            )
            return
        entry.outcome = "ok"

    def _deny(self, entry: _Entry, status: int, outcome: str, payload: dict) -> JSONResponse:
        entry.status, entry.outcome = status, outcome
        self._record(entry)
        return JSONResponse(payload, status_code=status)

    def _record(self, entry: _Entry) -> None:
        if entry.recorded:
            return
        entry.recorded = True
        duration_ms = round((time.monotonic() - entry.started) * 1000, 1)
        self.access_log.write(
            {
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
                "consumer": entry.consumer,
                "machine": entry.machine,
                "http_method": entry.http_method,
                "rpc_method": entry.rpc_method,
                "tool": entry.tool,
                "status": entry.status,
                "outcome": entry.outcome,
                "duration_ms": duration_ms,
            }
        )
        level = logging.WARNING if entry.outcome.startswith("denied") else logging.INFO
        logger.log(
            level,
            "%s /%s/mcp consumer=%s rpc=%s tool=%s status=%s outcome=%s duration_ms=%.1f",
            entry.http_method,
            entry.machine,
            entry.consumer,
            entry.rpc_method,
            entry.tool,
            entry.status,
            entry.outcome,
            duration_ms,
        )


async def _read_body(request: Request) -> bytes | None:
    """The request body, or None when it exceeds ``MAX_BODY_BYTES``."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


def create_app(
    config: LiveConfig,
    access_log: AccessLog,
    *,
    upstream_client: httpx2.AsyncClient | None = None,
) -> Starlette:
    """Build the gateway app. Tests inject *upstream_client* (an in-process transport)."""
    client = upstream_client or httpx2.AsyncClient(timeout=UPSTREAM_TIMEOUT, trust_env=False, follow_redirects=False)
    gateway = Gateway(config, access_log, client)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        loop = asyncio.get_running_loop()
        sighup = getattr(signal, "SIGHUP", None)
        installed = False
        if sighup is not None:
            try:
                loop.add_signal_handler(sighup, _on_sighup, config)
                installed = True
            except (NotImplementedError, RuntimeError) as exc:
                logger.warning("SIGHUP reload unavailable (%s) — file-change reload still active", exc)
        logger.info("remoteos-gateway v%s started (SIGHUP reload %s)", __version__, "on" if installed else "off")
        try:
            yield
        finally:
            if installed:
                loop.remove_signal_handler(sighup)
            await client.aclose()
            logger.info("remoteos-gateway stopped")

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/{machine}/mcp", gateway.handle, methods=["GET", "POST", "DELETE"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.gateway = gateway
    return app


def _on_sighup(config: LiveConfig) -> None:
    logger.info("SIGHUP received — reloading registry and machine source")
    config.reload(force=True)
