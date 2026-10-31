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
result is one message either way. Replies are capped, one and all together (``_ReplyBudget``),
because the unit's ``MemoryMax`` is 512 MB and an uncapped FileDownload would take every project's
gateway down.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_request
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult
from pydantic import TypeAdapter
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
from remoteos.mcp_sse import allow_large_replies

if TYPE_CHECKING:
    from remoteos.gateway.app import Gateway

logger = logging.getLogger("remoteos.gateway.fleet")

FLEET_MCP_PATH = f"/{FLEET_SERVER}/mcp"
_INTERNAL_BASE = "http://gateway.internal"  # never dialled: the transport is in-process

# ListMachines must answer even when half the fleet is down: probes run in parallel, and each is
# cut off here. A box that answers later reads as unreachable for this one listing.
PROBE_BOUND_SECONDS = 4.0
_PROBE_TIMEOUT = httpx2.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0)
# A reply costs about 11 times its size at peak (the transport's buffer, the parsed model, the
# serialised copy, base64 on top; measured in the #51 review), against MemoryMax=512M with ~85 MB
# at rest: 32 MB in flight is the most that stays under it.
MAX_REPLY_BYTES = 20 * 1024 * 1024
MAX_BUFFERED_BYTES = 32 * 1024 * 1024
# MachineTools is a quick question; a box that goes silent after initialize gets no hour.
_TOOLS_TIMEOUT_SECONDS = 30.0
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

_CALL_RESULT = TypeAdapter(CallToolResult)


def keep_arguments_out_of_the_logs() -> None:
    """Never below INFO, and never louder than the level the gateway was started with."""
    redact_fastmcp_argument_logs()
    level = max(logging.INFO, logging.getLogger().getEffectiveLevel())
    for name in _PAYLOAD_LOGGERS:
        logging.getLogger(name).setLevel(level)


class _ReplyTooLarge(Exception):
    pass


@dataclass
class _ReplyBudget:
    """The bytes of machine replies this gateway holds right now, across all fleet calls."""

    used: int = 0


class _ReplyCap:
    """ASGI wrapper: stops a reply at ``MAX_REPLY_BYTES``, or when all replies together would pass
    ``MAX_BUFFERED_BYTES``, instead of buffering it to the end."""

    def __init__(self, app: ASGIApp, budget: _ReplyBudget) -> None:
        self.app = app
        self.budget = budget
        self.refused: str | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        size = 0

        async def capped_send(message: Message) -> None:
            nonlocal size
            chunk = len(message.get("body", b""))
            if size + chunk > MAX_REPLY_BYTES:
                self.refused = f"the reply is over {MAX_REPLY_BYTES} bytes, too large for the fleet server"
            elif self.budget.used + chunk > MAX_BUFFERED_BYTES:
                self.refused = "the fleet server holds too many large replies right now, try again"
            if self.refused is not None:
                raise _ReplyTooLarge
            size += chunk
            self.budget.used += chunk
            await send(message)

        try:
            await self.app(scope, receive, capped_send)
        finally:
            self.budget.used -= size


def _failure_text(machine: str, exc: Exception, statuses: list[int], cap: _ReplyCap) -> str:
    if cap.refused is not None:
        return f"Machine {machine!r}: {cap.refused}"
    if statuses and statuses[-1] in (401, 403):
        return f"Machine {machine!r} rejected the gateway's key (a stale key: `remoteos-gateway check` repairs it)"
    return f"Machine {machine!r}: {str(exc) or type(exc).__name__}"


class FleetEndpoint:
    """The ASGI app at ``/fleet/mcp``: the fleet FastMCP app behind the gateway's request handling."""

    def __init__(self, gateway: Gateway) -> None:
        keep_arguments_out_of_the_logs()
        allow_large_replies()  # our own caps below decide, not the SDK's 1 MiB
        self.gateway = gateway
        self.budget = _ReplyBudget()
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
            gateway.finish(entry, status, _outcome(status))

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
                tools = await asyncio.wait_for(client.list_tools(), _TOOLS_TIMEOUT_SECONDS)
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
                # Not client.call_tool_mcp: its SDK path validates the result with a tools/list
                # AFTER the tool ran, so a box that fails that second step turns a finished call
                # into an error, and the caller runs it again. The result is the machine's anyway.
                request = CallToolRequest(params=CallToolRequestParams(name=tool, arguments=arguments or {}))
                return await client.session.send_request(
                    request, _CALL_RESULT, request_read_timeout_seconds=_CALL_TIMEOUT_SECONDS
                )

        return server

    def _refusal(self, consumer: str | None, machine: str) -> tuple[int, str, str] | None:
        """Why *consumer* may not use *machine* now: (status, outcome, message), or None. In the
        gateway's order, not allowed before not exposed: a consumer cannot probe what exists."""
        registry, _ = self.gateway.config.snapshot()
        if consumer is None or consumer not in registry.consumers:  # revoked while on its way
            return 401, "denied_unknown_token", "Unauthorized"
        if not is_valid_machine_name(machine) or machine not in registry.consumers[consumer].machines:
            return 403, "denied_not_allowed", f"Machine {machine!r} is not allowed for this token"
        if machine not in registry.exposed:
            return 404, "denied_unexposed", f"Machine {machine!r} is not available"
        return None

    def _consumer(self, authorization: str | None) -> str | None:
        registry, _ = self.gateway.config.snapshot()
        return self.gateway.identify(authorization, registry)

    def _admit(self, machine: str, tool: str) -> str:
        """The consumer's ``Authorization`` value, or a tool error."""
        authorization = get_http_request().headers.get("authorization")
        consumer = self._consumer(authorization)
        refusal = self._refusal(consumer, machine)
        if refusal is None and authorization is not None:
            return authorization
        status, outcome, message = refusal or (401, "denied_unknown_token", "Unauthorized")
        if consumer is not None:
            self.gateway.record_refusal(consumer, machine, tool, status, outcome)
        raise failure(message)

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
        cap = _ReplyCap(self.machine_app, self.budget)
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
            # A 401 from Gateway.handle itself: the consumer lost the machine since _admit.
            refusal = self._refusal(self._consumer(authorization), machine)
            raise failure(refusal[2] if refusal else _failure_text(machine, exc, statuses, cap)) from None

    async def _list_machines(self) -> list[dict[str, Any]]:
        consumer = self._consumer(get_http_request().headers.get("authorization"))
        registry, machines = self.gateway.config.snapshot()
        if consumer is None or consumer not in registry.consumers:
            raise failure("Unauthorized")
        names = sorted(
            name for name in registry.consumers[consumer].machines if name in registry.exposed and name in machines
        )
        async with httpx2.AsyncClient(timeout=_PROBE_TIMEOUT, trust_env=False, follow_redirects=False) as client:
            results = await gather_probes(_bounded_probe(client, machines[name].url) for name in names)
        return [_listing(name, result) for name, result in zip(names, results)]


def _outcome(status: int | None) -> str:
    if status is None:
        return "client_disconnect"  # no answer started: the consumer left
    if status < 400:
        return "ok"
    return "fleet_refused" if status < 500 else "fleet_error"


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
