"""The permanent enrollment endpoint (#101): ``remoteos-gateway enroll-listen``.

A box runs the ONE fixed README line, its installer posts the registration here, and it waits in
the queue (``enroll_queue.py``) until the operator accepts it. The listener binds 127.0.0.1 only;
dev1's named cloudflared tunnel publishes it as ``https://enroll-dev1.newlevel.media``.

Routes, nothing else (Starlette answers 404 / 405):

- ``POST /enroll/`` (and ``/enroll``) — the registration JSON the installers already post (#26):
  at most ``REGISTRATION_LIMIT`` bytes, validated by ``parse_registration``; answers ``ok`` (the
  installers count only that body as success).
- ``POST /enroll/log`` — an install log tail, plain text, at most ``LOG_LIMIT`` bytes; the box,
  OS and exit code come in ``X-Remoteos-Host`` / ``X-Remoteos-Os`` / ``X-Remoteos-Exit``.
- ``GET /healthz`` — 204, for the tunnel check.

Anyone can post (the URL is public): a per-address rate limit (``RATE_PER_IP`` per
``RATE_WINDOW_S``) and a global one bound the work, and the queue has its own size cap. The
address is Cloudflare's ``CF-Connecting-IP``, trusted only when the peer is loopback (cloudflared
on this box). Nothing the box sent is ever logged but the validated hostname / OS / version, and
error answers name the field, never its value: the key never reaches a log line or a reply.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import deque
from collections.abc import Callable

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from remoteos.gateway.enroll import EnrollError, parse_registration
from remoteos.gateway.enroll_queue import EnrollQueue, QueueFull, log_meta, source_text

logger = logging.getLogger("remoteos.gateway.enroll_listen")

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8797
REGISTRATION_LIMIT = 16 * 1024
LOG_LIMIT = 256 * 1024
RATE_WINDOW_S = 600.0
RATE_PER_IP = 10
RATE_GLOBAL = 120
_MAX_TRACKED = 10_000


class _TooLarge(Exception):
    pass


class RateLimiter:
    """A sliding window per address plus one for everyone (a flood from many addresses)."""

    def __init__(self, *, per_ip: int = RATE_PER_IP, overall: int = RATE_GLOBAL, window_s: float = RATE_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic):
        self.per_ip, self.overall, self.window_s, self._clock = per_ip, overall, window_s, clock
        self._seen: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _trim(self, times: deque[float], now: float) -> None:
        while times and now - times[0] > self.window_s:
            times.popleft()

    def allow(self, address: str) -> bool:
        now = self._clock()
        self._trim(self._all, now)
        times = self._seen.get(address)
        if times is None:
            if len(self._seen) >= _MAX_TRACKED:  # forget the quiet ones before the map grows further
                for key in [k for k, v in self._seen.items() if not v or now - v[-1] > self.window_s]:
                    del self._seen[key]
            times = self._seen.setdefault(address, deque())
        self._trim(times, now)
        if len(times) >= self.per_ip or len(self._all) >= self.overall:
            return False
        times.append(now)
        self._all.append(now)
        return True


def client_address(request: Request) -> str:
    """Cloudflare's view of the poster when cloudflared (loopback) is the peer, else the peer."""
    peer = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        forwarded = source_text(request.headers.get("cf-connecting-ip", "").strip())
        if forwarded != "unknown":
            return forwarded
    return source_text(peer)


async def read_capped(request: Request, limit: int) -> bytes:
    """The body, refused (``_TooLarge``) as soon as it passes *limit*, whatever Content-Length said."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _TooLarge
    return bytes(body)


def _text(code: int, message: str) -> PlainTextResponse:
    return PlainTextResponse(message + "\n" if code != 200 else message, status_code=code)


def create_app(queue: EnrollQueue, *, limiter: RateLimiter | None = None) -> Starlette:
    rate = limiter or RateLimiter()

    async def guarded_body(request: Request, limit: int) -> tuple[str, bytes | Response]:
        address = client_address(request)
        if not rate.allow(address):
            logger.warning("enroll: %s %s refused from %s: rate limit", request.method, request.url.path, address)
            return address, _text(429, "too many requests")
        try:
            return address, await read_capped(request, limit)
        except _TooLarge:
            logger.warning("enroll: %s refused from %s: body over %d bytes", request.url.path, address, limit)
            return address, _text(413, "body too large")
        except ClientDisconnect:
            return address, _text(400, "incomplete body")

    async def register(request: Request) -> Response:
        address, body = await guarded_body(request, REGISTRATION_LIMIT)
        if isinstance(body, Response):
            return body
        try:
            registration = parse_registration(body)
        except EnrollError as exc:  # names the field, never its value
            logger.warning("enroll: invalid registration from %s: %s", address, exc)
            return _text(400, str(exc))
        try:
            entry, created = queue.add(registration, address)
        except QueueFull as exc:
            logger.warning("enroll: registration of %s from %s refused: %s", registration.hostname, address, exc)
            return _text(503, "the enrollment queue is full")
        except OSError as exc:
            logger.error("enroll: the queue cannot be written (%s)", type(exc).__name__)
            return _text(503, "the enrollment queue cannot be written")
        logger.info("enroll: %s registration %s from %s: hostname %s, %s, v%s, %d private address(es)",
                    "new" if created else "repeated", entry.id, address, registration.hostname, registration.os,
                    registration.version, len(registration.addresses))
        return _text(200, "ok")

    async def install_log(request: Request) -> Response:
        address, body = await guarded_body(request, LOG_LIMIT)
        if isinstance(body, Response):
            return body
        host, system, code = log_meta(request.headers.get("x-remoteos-host"), request.headers.get("x-remoteos-os"),
                                      request.headers.get("x-remoteos-exit"))
        try:
            log_id = queue.add_log(body.decode("utf-8", errors="replace"), hostname=host, os_name=system,
                                   exit_code=code, source=address)
        except OSError as exc:
            logger.error("enroll: the install log cannot be written (%s)", type(exc).__name__)
            return _text(503, "the install log cannot be written")
        logger.warning("enroll: install log %s from %s: host %s (%s), exit %s, %d bytes", log_id, address, host,
                       system, code or "?", len(body))
        return _text(200, "ok")

    async def healthz(request: Request) -> Response:
        return Response(status_code=204)

    return Starlette(routes=[
        Route("/enroll/", register, methods=["POST"]),
        Route("/enroll", register, methods=["POST"]),
        Route("/enroll/log", install_log, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
    ])
