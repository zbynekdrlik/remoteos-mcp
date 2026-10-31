"""The fleet server (#51 Phase 2): ``/fleet/mcp``, every machine this token may use, in three tools.

Claude Code reads ``.mcp.json`` only at session start, so a machine added to a project reaches no
running session as a new server. This server's tools never change, so ``sync-configs`` writes it
once per project; after that a machine is usable the moment the manifest and key store hold it:

- ``ListMachines()``: the machines this token may use, with OS, reachability and version.
- ``MachineTools(machine)``: that machine's tools with their schemas.
- ``Call(machine, tool, arguments)``: one tool call; its result comes back as the machine gave it.

**There is no second access path.** ``Call`` and ``MachineTools`` are a fastmcp ``Client`` speaking
to ``Gateway.handle`` in-process with the consumer's own bearer token, exactly as if the consumer
had called ``/<machine>/mcp``: same allow-list and ``unexpose`` kill switch, same body cap, key
swap, stale-connection retry and access-log lines. The checks in :meth:`FleetEndpoint._admit` only
give a clear error (and an audit line) without opening a session; ``handle`` decides again.

``httpx2.ASGITransport`` buffers: the machine's reply is held in memory until the call ends, and
progress notifications do not reach the consumer. That is the cost of the single path; a tool
result is one message either way. The reply is capped (``MAX_REPLY_BYTES``), because the unit's
``MemoryMax`` is 512 MB and an uncapped FileDownload would take every project's gateway down.

Outside the tools, :meth:`FleetEndpoint.__call__` keeps the gateway's decision order (401, then
413, no body read before the token is known) and writes one access-log line per request. The
log holds the rpc method and the fleet tool's name, never ``arguments``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_request
from mcp.types import CallToolResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from remoteos.errors import failure, redact_fastmcp_argument_logs
from remoteos.gateway.probe import (
    STATE_OK,
    STATE_UNREACHABLE,
    ProbeResult,
    gather_probes,
    probe_version,
)
from remoteos.gateway.registry import FLEET_SERVER, is_valid_machine_name

if TYPE_CHECKING:
    from remoteos.gateway.app import Gateway

logger = logging.getLogger("remoteos.gateway.fleet")

FLEET_MCP_PATH = f"/{FLEET_SERVER}/mcp"
_INTERNAL_BASE = "http://gateway.internal"  # never dialled: the transport is in-process

# ListMachines must answer even when half the fleet is down: probes run in parallel, and each is
# cut off here. A box that answers later reads as unreachable for this one listing.
PROBE_BOUND_SECONDS = 4.0
_PROBE_TIMEOUT = httpx2.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0)
# A reply is held in memory three times over (transport, parsed, serialised again).
MAX_REPLY_BYTES = 64 * 1024 * 1024
# A session to a machine that accepts the connection and then says nothing.
_INIT_TIMEOUT_SECONDS = 30.0
# Above UPSTREAM_TIMEOUT.read (3700 s), so the gateway's own error is what the caller sees.
_CALL_TIMEOUT_SECONDS = 3760.0

_INSTRUCTIONS = (
    "The machines of the remoteos fleet this token may use. ListMachines shows them, MachineTools"
    " shows one machine's tools, Call runs one. Use this for a machine that has no server of its"
    " own in this session (it was added after the session started); a machine with its own"
    " server is the same machine, with typed tools."
)

# Debug logging of these prints whole JSON-RPC messages, which hold tool arguments and results.
_PAYLOAD_LOGGERS = ("mcp", "fastmcp", "httpcore2", "sse_starlette")


def _keep_arguments_out_of_the_logs() -> None:
    redact_fastmcp_argument_logs()
    for name in _PAYLOAD_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)


class _ReplyTooLarge(Exception):
    pass


class _ReplyCap:
    """ASGI wrapper: stops a reply at ``MAX_REPLY_BYTES`` instead of buffering it to the end."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.exceeded = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        size = 0

        async def capped_send(message: Message) -> None:
            nonlocal size
            size += len(message.get("body", b""))
            if size > MAX_REPLY_BYTES:
                self.exceeded = True
                raise _ReplyTooLarge
            await send(message)

        await self.app(scope, receive, capped_send)


def _failure_text(machine: str, exc: Exception, statuses: list[int], cap: _ReplyCap) -> str:
    if cap.exceeded:
        return f"Machine {machine!r}: the reply is over {MAX_REPLY_BYTES} bytes, too large for the fleet server"
    if statuses and statuses[-1] in (401, 403):
        return f"Machine {machine!r} rejected the gateway's key (a stale key: `remoteos-gateway check` repairs it)"
    return f"Machine {machine!r}: {str(exc) or type(exc).__name__}"


class FleetEndpoint:
    """The ASGI app at ``/fleet/mcp``: the fleet FastMCP app behind the gateway's request handling."""

    def __init__(self, gateway: Gateway) -> None:
        _keep_arguments_out_of_the_logs()
        self.gateway = gateway
        self.machine_app = Starlette(routes=[Route("/{machine}/mcp", gateway.handle, methods=["POST", "DELETE"])])
        self.mcp = self._build_server()
        # stateless + SSE answers, like the machines (Gateway._send_upstream). The bearer token is
        # the gate here; the Host/Origin guard is for unauthenticated local servers.
        self.http = self.mcp.http_app(
            path=FLEET_MCP_PATH, stateless_http=True, json_response=False, host_origin_protection=False
        )

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self.http.router.lifespan_context(self.http):
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        gateway = self.gateway
        entry = gateway.begin(FLEET_SERVER, request.method)
        registry, _ = gateway.config.snapshot()
        denied = gateway.authenticate(request, entry, registry)
        if denied is None:
            read = await gateway.read_rpc(request, entry)
            denied = read if isinstance(read, JSONResponse) else None
        if denied is not None:
            await denied(scope, receive, send)
            return
        body, _ = read
        replay: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]

        async def replay_receive() -> Message:
            return replay.pop() if replay else await receive()

        status: int | None = None

        async def tracked_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.http(scope, replay_receive, tracked_send)
        finally:
            gateway.finish(entry, status, "ok" if status is not None and status < 500 else "fleet_error")

    # -- the tools --------------------------------------------------------------------------

    def _build_server(self) -> FastMCP:
        server = FastMCP("remoteos-fleet", instructions=_INSTRUCTIONS)

        @server.tool(name="ListMachines", annotations={"readOnlyHint": True})
        async def list_machines() -> list[dict[str, Any]]:
            """The machines this token may use: name, reachable, os, hostname, version.

            A machine that is down is listed with reachable=false; the list never waits for it.
            """
            return await self._list_machines()

        @server.tool(name="MachineTools", annotations={"readOnlyHint": True})
        async def machine_tools(machine: str) -> list[dict[str, Any]]:
            """The tools of *machine*, each with its description and inputSchema."""
            authorization = self._admit(machine, "MachineTools")
            async with self._session(machine, authorization) as client:
                tools = await client.list_tools()
            return [
                {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                for tool in tools
            ]

        @server.tool(name="Call")
        async def call(machine: str, tool: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
            """Run *tool* on *machine* with *arguments*. The result is the machine's own: text, images
            and structured content pass through, and a tool's failure stays a tool error."""
            authorization = self._admit(machine, "Call")
            async with self._session(machine, authorization) as client:
                return await client.call_tool_mcp(tool, arguments or {})

        return server

    def _caller(self) -> tuple[str | None, str | None]:
        """(the consumer, its ``Authorization`` value) of the request in progress, read live."""
        authorization = get_http_request().headers.get("authorization")
        registry, _ = self.gateway.config.snapshot()
        return self.gateway.identify(authorization, registry), authorization

    def _admit(self, machine: str, tool: str) -> str:
        """The consumer's ``Authorization`` value, or a tool error in the gateway's decision order:
        not allowed before not exposed, so a consumer cannot probe which other machines exist."""
        consumer, authorization = self._caller()
        if consumer is None or authorization is None:  # revoked while the request was on its way
            raise failure("Unauthorized")
        registry, _ = self.gateway.config.snapshot()
        allowed = registry.consumers[consumer].machines if consumer in registry.consumers else []
        if not is_valid_machine_name(machine) or machine not in allowed:
            self.gateway.record_refusal(consumer, machine, tool, 403, "denied_not_allowed")
            raise failure(f"Machine {machine!r} is not allowed for this token")
        if machine not in registry.exposed:
            self.gateway.record_refusal(consumer, machine, tool, 404, "denied_unexposed")
            raise failure(f"Machine {machine!r} is not available")
        return authorization

    def _http_client(self, cap: _ReplyCap, on_status, **kwargs: Any) -> httpx2.AsyncClient:
        """What the MCP client speaks through: ``Gateway.handle``, in-process."""
        kwargs.pop("follow_redirects", None)  # a redirect from a machine is not followed

        async def record(response: httpx2.Response) -> None:
            on_status(response.status_code)

        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=cap),
            follow_redirects=False,
            event_hooks={"response": [record]},
            **kwargs,
        )

    @asynccontextmanager
    async def _session(self, machine: str, authorization: str) -> AsyncIterator[Client]:
        statuses: list[int] = []
        cap = _ReplyCap(self.machine_app)
        transport = StreamableHttpTransport(
            f"{_INTERNAL_BASE}/{machine}/mcp",
            headers={"Authorization": authorization},
            httpx_client_factory=functools.partial(self._http_client, cap, statuses.append),
        )
        client = Client(transport, timeout=_CALL_TIMEOUT_SECONDS, init_timeout=_INIT_TIMEOUT_SECONDS, mode="legacy")
        try:
            async with client:
                yield client
        except Exception as exc:
            logger.warning("fleet call to machine %r failed: %s", machine, type(exc).__name__)
            raise failure(_failure_text(machine, exc, statuses, cap)) from None

    async def _list_machines(self) -> list[dict[str, Any]]:
        consumer, _ = self._caller()
        registry, machines = self.gateway.config.snapshot()
        if consumer is None or consumer not in registry.consumers:
            raise failure("Unauthorized")
        names = sorted(
            name for name in registry.consumers[consumer].machines if name in registry.exposed and name in machines
        )
        async with httpx2.AsyncClient(timeout=_PROBE_TIMEOUT, trust_env=False, follow_redirects=False) as client:
            results = await gather_probes(_bounded_probe(client, machines[name].url) for name in names)
        return [_listing(name, result) for name, result in zip(names, results)]


async def _bounded_probe(client: httpx2.AsyncClient, url: str) -> ProbeResult:
    try:
        return await asyncio.wait_for(probe_version(client, url), PROBE_BOUND_SECONDS)
    except asyncio.TimeoutError:
        return ProbeResult(STATE_UNREACHABLE, None, "TimeoutError")


def _listing(name: str, result: ProbeResult) -> dict[str, Any]:
    """One machine for ``ListMachines``. Any HTTP answer means the box is up, even without a
    version: an older server, or a broken /health."""
    return {
        "machine": name,
        "reachable": result.status is not None,
        "os": result.os,
        "hostname": result.hostname,
        "version": result.detail if result.state == STATE_OK else None,
        "detail": None if result.state == STATE_OK else result.detail,
    }
