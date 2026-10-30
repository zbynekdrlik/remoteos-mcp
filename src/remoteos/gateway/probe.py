"""Probe a machine's MCP endpoint DIRECTLY with its key: one ``initialize`` call (#51).

Used by ``machine set-key`` (is the new key right?), ``machine import`` (which copy of a
key is the live one?) and ``check`` (the drift watchdog). The answer is reduced to a state:

- ``ok``          — HTTP 200: the key is accepted.
- ``401``         — HTTP 401 or 403: the machine rejects the key (stale / rotated).
- ``unreachable`` — no HTTP answer: connect refused, timeout, DNS, TLS, reset.
- ``error``       — any other HTTP status.

The key travels only in the ``Authorization`` header; ``detail`` never carries it, and the
response body is never read (a machine answers ``initialize`` as SSE).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx2

from remoteos import __version__

logger = logging.getLogger("remoteos.gateway.probe")

# A machine that is down must not hold an unattended timer run for long.
PROBE_TIMEOUT = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

STATE_OK = "ok"
STATE_UNAUTHORIZED = "401"
STATE_UNREACHABLE = "unreachable"
STATE_ERROR = "error"

# What a machine's /health may report: it ends up in the status file and an owner message.
_VERSION_TEXT = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


@dataclass(frozen=True)
class ProbeResult:
    state: str
    status: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK


def _initialize_body() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "remoteos-gateway-probe", "version": __version__},
            },
        }
    ).encode()


def _classify(status: int) -> ProbeResult:
    if status == 200:
        return ProbeResult(STATE_OK, status, "HTTP 200")
    if status in (401, 403):
        return ProbeResult(STATE_UNAUTHORIZED, status, f"HTTP {status}: the machine rejects the key")
    return ProbeResult(STATE_ERROR, status, f"HTTP {status}")


def new_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False, follow_redirects=False)


async def probe_initialize(client: httpx2.AsyncClient, url: str, authorization: str | None) -> ProbeResult:
    """POST ``initialize`` to *url* with *authorization*; never raises for a network failure."""
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if authorization is not None:
        headers["authorization"] = authorization
    try:
        request = httpx2.Request("POST", url, headers=headers, content=_initialize_body())
    except (httpx2.InvalidURL, ValueError) as exc:
        return ProbeResult(STATE_ERROR, None, f"invalid url ({type(exc).__name__})")
    try:
        response = await client.send(request, stream=True)
    except httpx2.RequestError as exc:
        return ProbeResult(STATE_UNREACHABLE, None, f"{type(exc).__name__}: {exc}")
    try:
        return _classify(response.status_code)
    finally:
        await response.aclose()


async def probe_healthz(client: httpx2.AsyncClient, base_url: str) -> ProbeResult:
    """GET the gateway's own ``/healthz`` (unauthenticated)."""
    try:
        response = await client.get(base_url.rstrip("/") + "/healthz")
    except httpx2.RequestError as exc:
        return ProbeResult(STATE_UNREACHABLE, None, f"{type(exc).__name__}: {exc}")
    if response.status_code != 200:
        return ProbeResult(STATE_ERROR, response.status_code, f"HTTP {response.status_code}")
    try:
        version = response.json().get("version", "?")
    except (ValueError, AttributeError):
        return ProbeResult(STATE_ERROR, 200, "HTTP 200 but not the gateway's /healthz JSON")
    return ProbeResult(STATE_OK, 200, f"version {version}")


def health_url(mcp_url: str) -> str:
    """The machine's public ``/health``, next to its ``/mcp`` endpoint."""
    parts = urlsplit(mcp_url)
    base = parts.path[: -len("/mcp")] if parts.path.endswith("/mcp") else parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, base + "/health", "", ""))


async def probe_version(client: httpx2.AsyncClient, mcp_url: str) -> ProbeResult:
    """GET a machine's ``/health`` (public: no key is sent); ``detail`` is its version when ``ok``."""
    try:
        response = await client.get(health_url(mcp_url))
    except (httpx2.RequestError, httpx2.InvalidURL, ValueError) as exc:
        return ProbeResult(STATE_UNREACHABLE, None, type(exc).__name__)
    if response.status_code != 200:
        return ProbeResult(STATE_ERROR, response.status_code, f"HTTP {response.status_code}")
    try:
        version = response.json().get("version")
    except (ValueError, AttributeError):
        version = None
    if not isinstance(version, str) or _VERSION_TEXT.fullmatch(version) is None:
        return ProbeResult(STATE_ERROR, 200, "no version in /health")
    return ProbeResult(STATE_OK, 200, version)


async def gather_probes(probes: Iterable[Awaitable[ProbeResult]]) -> list[ProbeResult]:
    """Run probes concurrently. One that RAISES anyway (a transport may still throw, e.g. an
    OverflowError from a socket call) becomes an ``error`` result for that machine alone — it
    never takes the other probes, the import or the watchdog run down with it."""
    results = await asyncio.gather(*probes, return_exceptions=True)
    out: list[ProbeResult] = []
    for result in results:
        if isinstance(result, Exception):
            logger.error("probe failed unexpectedly: %s", type(result).__name__)
            out.append(ProbeResult(STATE_ERROR, None, f"probe failed: {type(result).__name__}"))
        elif isinstance(result, BaseException):  # cancellation / interrupt: never swallowed
            raise result
        else:
            out.append(result)
    return out


async def probe_many(targets: Iterable[tuple[object, str, str | None]]) -> dict[object, ProbeResult]:
    """Probe every ``(id, url, authorization)`` concurrently; returns ``{id: ProbeResult}``."""
    targets = list(targets)
    async with new_client() as client:
        results = await gather_probes(probe_initialize(client, url, auth) for _, url, auth in targets)
    return {target[0]: result for target, result in zip(targets, results)}


def probe_one(url: str, authorization: str | None) -> ProbeResult:
    """Synchronous single probe (the CLI)."""
    return asyncio.run(probe_many([(0, url, authorization)]))[0]
