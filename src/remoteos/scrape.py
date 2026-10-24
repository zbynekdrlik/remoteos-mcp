"""The Scrape tool's fetch: one URL as markdown, bounded and (optionally) public-only (#39).

- Only ``http://`` and ``https://``. ``urlopen`` also opens ``file://`` and ``ftp://``: a tier-2
  Scrape of ``file:///…`` would read files the tier-3 FileRead guards.
- At most ``SCRAPE_MAX_BYTES`` (5 MiB) are read: ``resp.read(cap + 1)``, and a body over the cap is
  an error, never a silent cut. (The old ``resp.read()`` held the whole body in memory first; the
  50 000-character markdown cut came after.) A ``Content-Length`` over the cap fails before reading.
- Private / LAN / loopback addresses are ALLOWED by default: fleet agents scrape LAN pages. With
  ``--scrape-block-private`` (``security.scrape_block_private``) they are refused: the host is
  resolved with ``getaddrinfo`` and EVERY address must be public, for the URL and for each redirect.
  Known limit: the check and the connection resolve separately, so a DNS answer that changes in
  between (rebinding) is not caught.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
import urllib.request

log = logging.getLogger("remoteos.scrape")

SCRAPE_MAX_BYTES = 5 * 1024 * 1024
MARKDOWN_MAX_CHARS = 50000
TIMEOUT_S = 15

# Set at start-up from --scrape-block-private / security.scrape_block_private.
BLOCK_PRIVATE = False

_SCHEMES = ("http", "https")


def configure(block_private: bool) -> None:
    """Set the server-wide private-address policy (``cli()`` at start-up)."""
    global BLOCK_PRIVATE
    BLOCK_PRIVATE = bool(block_private)


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # an IPv6 zone id is not part of the address
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def refuse_private(url: str) -> None:
    """Raise ``ValueError`` unless every address ``url``'s host resolves to is public."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        raise ValueError("the URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve the host: {e}") from None
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public(a) for a in addresses):
        log.info("Scrape refused a private or local address (%d address(es) checked)", len(addresses))
        raise ValueError("refusing a private or local address (--scrape-block-private is on)")


def _check_scheme(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme.lower() not in _SCHEMES:
        raise ValueError("only http:// and https:// URLs can be scraped")


class _RefusePrivateRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect target: scheme, and the private-address policy when it is on."""

    def __init__(self, block_private: bool = True) -> None:
        super().__init__()
        self.block_private = block_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_scheme(newurl)
        if self.block_private:
            refuse_private(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_capped(resp, cap: int = SCRAPE_MAX_BYTES) -> bytes:
    """At most ``cap`` bytes of ``resp``; a longer body (or a ``Content-Length`` over it) raises."""
    too_big = ValueError(f"the response is larger than {cap} bytes ({cap // (1024 * 1024)} MiB)")
    length = resp.headers.get("Content-Length")
    if length is not None and length.strip().isdigit() and int(length) > cap:
        raise too_big
    body = resp.read(cap + 1)
    if len(body) > cap:
        raise too_big
    return body


def fetch_markdown(url: str, block_private: bool | None = None) -> str:
    """Fetch ``url`` and return it as markdown (``block_private`` defaults to the server's policy)."""
    from markdownify import markdownify

    block = BLOCK_PRIVATE if block_private is None else block_private
    _check_scheme(url)
    if block:
        refuse_private(url)
    opener = urllib.request.build_opener(_RefusePrivateRedirects(block))
    req = urllib.request.Request(url, headers={"User-Agent": "remoteos-mcp/0.3"})
    with opener.open(req, timeout=TIMEOUT_S) as resp:
        html = read_capped(resp).decode("utf-8", errors="replace")
    md = markdownify(html, heading_style="ATX", strip=["script", "style"])
    if len(md) > MARKDOWN_MAX_CHARS:
        md = md[:MARKDOWN_MAX_CHARS] + "\n\n[... truncated]"
    return md
