"""Security helpers: the bind guard, IP allowlist parsing + middleware."""

from __future__ import annotations

import ipaddress

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def is_loopback_host(host: str) -> bool:
    """``host`` binds only the loopback interface: ``localhost``, ``127.0.0.0/8`` or ``::1``.

    Anything else counts as reachable from the network: ``0.0.0.0``/``::``/"" (every interface),
    a LAN or tailnet address, or a host NAME (it is not resolved here).
    """
    name = host.strip().strip("[]")
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def check_bind_exposure(host: str, auth_key: str | None, tier3_enabled: bool) -> str | None:
    """The bind guard (#39, upstream PR #76's rule).

    - A non-loopback bind with NO auth key while Tier 3 is enabled raises ``ValueError``: that is
      an open full shell (Shell, files, services) on the network. (OAuth is no substitute: its
      authorize endpoint approves every client.)
    - The same bind with Tier 1/2 only returns a warning line, as before.
    - Otherwise ``None``. ``--enable-all`` WITH a key, the way every installer starts, is fine.
    """
    if auth_key or is_loopback_host(host):
        return None
    if tier3_enabled:
        raise ValueError(
            f"refusing to bind {host or '(all interfaces)'} without an auth key while Tier 3 tools "
            "(Shell, files, services) are enabled: set --auth-key / REMOTEOS_AUTH_KEY, bind 127.0.0.1, "
            "or drop Tier 3"
        )
    return f"open to the network without auth on {host or '(all interfaces)'}! Use --auth-key for security."


def parse_ip_allowlist(raw_entries: list[str]) -> list[ipaddress._BaseNetwork]:
    """Parse allowlist entries of single IPs and CIDR ranges."""
    parsed: list[ipaddress._BaseNetwork] = []
    errors: list[str] = []

    for entry in raw_entries:
        value = entry.strip()
        if not value:
            continue
        try:
            if "/" in value:
                parsed.append(ipaddress.ip_network(value, strict=False))
            else:
                ip_obj = ipaddress.ip_address(value)
                suffix = "/32" if ip_obj.version == 4 else "/128"
                parsed.append(ipaddress.ip_network(f"{ip_obj}{suffix}", strict=False))
        except ValueError as exc:
            errors.append(f"{entry}: {exc}")

    if errors:
        raise ValueError("Invalid IP allowlist entries: " + "; ".join(errors))

    return parsed


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Restrict access to configured client IP networks."""

    def __init__(self, app, allowlist: list[ipaddress._BaseNetwork]):
        super().__init__(app)
        self.allowlist = allowlist

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        client = request.client
        if client is None:
            return JSONResponse({"error": "Forbidden: missing client address"}, status_code=403)

        try:
            client_ip = ipaddress.ip_address(client.host)
        except ValueError:
            return JSONResponse({"error": f"Forbidden: invalid client address {client.host}"}, status_code=403)

        if not any(client_ip in net for net in self.allowlist):
            return JSONResponse(
                {
                    "error": f"Forbidden: client IP {client_ip} is not in allowlist",
                },
                status_code=403,
            )

        return await call_next(request)
