"""Server hardening from the upstream security sync (#39): OAuth, Scrape, the bind guard, TOML booleans.

- OAuth: PKCE is S256 only; a redirect URI is ``https://`` or loopback ``http://``; when the operator
  configured a client secret, the token endpoint requires it and registration never hands it out.
- Scrape: http(s) only, at most ``SCRAPE_MAX_BYTES`` (5 MiB) read; private/LAN addresses are allowed
  by default (fleet agents scrape LAN pages) and refused with ``--scrape-block-private``, every
  resolved address checked, redirects included.
- Bind guard: a non-loopback bind with no auth key refuses to start when Tier 3 is enabled; Tier 1/2
  only keeps the warning.
- TOML booleans: real booleans or the strings true/false/1/0/yes/no; anything else names the key
  and never echoes the value.

Real HTTP where it matters (a local ``http.server`` for Scrape, Starlette's TestClient for OAuth).
Expected values are literals.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socket
import threading
import tracemalloc
import urllib.request

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from tool_surface import server
from tool_test_support import tool_error

from remoteos import scrape
from remoteos.config import load_config
from remoteos.oauth import OAuthStore, build_oauth_routes
from remoteos.security import check_bind_exposure, is_loopback_host

# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

VERIFIER = "v" * 64
S256_CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
OP_SECRET = "op-" + secrets.token_hex(8)  # made at run time: no literal credential in the source
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _oauth_client(client_id: str | None = None, client_secret: str | None = None) -> TestClient:
    routes = build_oauth_routes(
        OAuthStore(), "http://127.0.0.1:8092", configured_client_id=client_id, configured_client_secret=client_secret
    )
    app = Starlette(routes=[Route(path, handler, methods=methods) for path, (handler, methods) in routes.items()])
    return TestClient(app)


def _authorize(client: TestClient, client_id: str, method: str = "S256", challenge: str = S256_CHALLENGE,
               redirect_uri: str = REDIRECT):
    return client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": method,
            "state": "s",
        },
        follow_redirects=False,
    )


def _code(response) -> str:
    assert response.status_code == 302, response.text
    return response.headers["location"].split("code=")[1].split("&")[0]


def _token(client: TestClient, code: str, client_id: str, **extra):
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "client_id": client_id,
            "redirect_uri": REDIRECT, **extra}
    return client.post("/oauth/token", data=data)


def test_metadata_advertises_s256_only():
    meta = _oauth_client().get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none", "client_secret_post"]


def test_metadata_with_a_configured_secret_advertises_only_the_secret_method():
    meta = _oauth_client(client_secret=OP_SECRET).get("/.well-known/oauth-authorization-server").json()
    assert meta["token_endpoint_auth_methods_supported"] == ["client_secret_post"]


def test_plain_pkce_is_refused_at_authorize():
    response = _authorize(_oauth_client(), "c1", method="plain", challenge=VERIFIER)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_plain_pkce_verification_is_refused():
    from remoteos.oauth import _verify_pkce

    assert _verify_pkce(VERIFIER, VERIFIER, "plain") is False
    assert _verify_pkce(VERIFIER, S256_CHALLENGE, "S256") is True


def test_s256_flow_issues_a_token():
    client = _oauth_client()
    response = _token(client, _code(_authorize(client, "c1")), "c1")
    assert response.status_code == 200, response.text
    assert response.json()["token_type"] == "Bearer"


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/cb",
        "http://127.0.0.1@evil.example/cb",
        "javascript:alert(1)",
        "ftp://example.com/cb",
        "https:///cb",
        "https://claude.ai/cb#frag",
        "cb",
    ],
)
def test_register_refuses_a_redirect_uri_that_is_not_https_or_loopback(uri):
    response = _oauth_client().post("/oauth/register", json={"redirect_uris": [REDIRECT, uri]})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "uri",
    [REDIRECT, "http://127.0.0.1:33418/callback", "http://localhost/cb", "http://[::1]:9/cb", "https://x.example"],
)
def test_register_accepts_https_and_loopback_redirect_uris(uri):
    response = _oauth_client().post("/oauth/register", json={"redirect_uris": [uri]})
    assert response.status_code == 201, response.text
    assert response.json()["redirect_uris"] == [uri]


def test_authorize_refuses_a_redirect_uri_that_is_not_https_or_loopback():
    response = _authorize(_oauth_client(), "c1", redirect_uri="http://evil.example/cb")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_register_never_hands_out_the_configured_secret():
    response = _oauth_client("op-client", OP_SECRET).post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    assert response.status_code == 201
    assert response.json()["client_id"] == "op-client"
    assert OP_SECRET not in response.text
    assert "client_secret" not in response.json()


@pytest.mark.parametrize("client_id", [None, "op-client"])
def test_a_configured_secret_is_required_at_the_token_endpoint(client_id):
    cid = client_id or "c1"
    client = _oauth_client(client_id, OP_SECRET)
    assert _token(client, _code(_authorize(client, cid)), cid).status_code == 401
    assert _token(client, _code(_authorize(client, cid)), cid, client_secret="wrong").status_code == 401
    good = _token(client, _code(_authorize(client, cid)), cid, client_secret=OP_SECRET)
    assert good.status_code == 200, good.text


@pytest.mark.parametrize(
    "body",
    [["not", "a", "dict"], {"grant_type": "authorization_code", "code": ["x"]},
     {"grant_type": "authorization_code", "code": "c", "client_id": {"a": 1}}],
    ids=["list-body", "list-code", "dict-client-id"],
)
def test_a_malformed_token_request_is_a_400_never_a_500(body):
    response = _oauth_client().post("/oauth/token", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_a_token_request_with_invalid_json_is_a_400_never_a_500():
    client = TestClient(_oauth_client().app, raise_server_exceptions=False)
    response = client.post("/oauth/token", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_without_a_configured_secret_a_public_client_needs_none():
    client = _oauth_client()
    assert _token(client, _code(_authorize(client, "c1")), "c1").status_code == 200


# ---------------------------------------------------------------------------
# Scrape
# ---------------------------------------------------------------------------


class _Pages(http.server.BaseHTTPRequestHandler):
    bodies: dict[str, bytes] = {}
    hits: list[str] = []
    redirect_body = b""  # the body a 302 carries (no Content-Length: read until the connection ends)

    def do_GET(self):  # noqa: N802 (http.server's name)
        self.hits.append(self.path)
        body = self.bodies.get(self.path)
        if body is None:
            self.send_response(302)
            self.send_header("Location", self.path[len("/redirect"):] or "/")
            self.end_headers()
            try:
                self.wfile.write(self.redirect_body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client never reads a redirect's body: it closed the connection
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if not self.path.startswith("/nolength"):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def pages():
    _Pages.bodies = {}
    _Pages.hits = []
    _Pages.redirect_body = b""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", _Pages
    httpd.shutdown()
    httpd.server_close()


def test_scrape_max_bytes_is_5_mib():
    assert scrape.SCRAPE_MAX_BYTES == 5 * 1024 * 1024


def test_scrape_of_a_lan_page_is_allowed_by_default(pages):
    base, handler = pages
    handler.bodies["/p"] = b"<h1>Ahoj</h1><p>svet</p>"
    assert server.Scrape(f"{base}/p").strip() == "# Ahoj\n\nsvet"


@pytest.mark.parametrize("path", ["/big", "/nolength-big"], ids=["content-length", "no-content-length"])
def test_scrape_of_a_body_over_the_cap_fails(pages, path):
    base, handler = pages
    handler.bodies[path] = b"a" * (5 * 1024 * 1024 + 1)
    text = tool_error(lambda: server.Scrape(f"{base}{path}"))
    assert text == "Scrape error: the response is larger than 5242880 bytes (5 MiB)"


def test_scrape_of_a_body_exactly_at_the_cap_is_read(pages):
    base, handler = pages
    handler.bodies["/full"] = b"a" * (5 * 1024 * 1024)
    assert server.Scrape(f"{base}/full").endswith("[... truncated]")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/x", "data:text/html,<b>x</b>"])
def test_scrape_refuses_a_scheme_other_than_http(url):
    text = tool_error(lambda: server.Scrape(url))
    assert text.startswith("Scrape error: only http:// and https:// URLs can be scraped")


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "192.168.1.10", "172.16.0.1", "169.254.169.254", "[::1]",
                                  "[::ffff:127.0.0.1]", "0.0.0.0", "localhost"])
def test_scrape_block_private_refuses_private_addresses(monkeypatch, host):
    monkeypatch.setattr(scrape, "BLOCK_PRIVATE", True)
    connects = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connects.append(a))
    text = tool_error(lambda: server.Scrape(f"http://{host}:8/x"))
    assert text.startswith("Scrape error: refusing a private or local address")
    assert connects == []


def test_scrape_block_private_checks_every_resolved_address(monkeypatch):
    monkeypatch.setattr(scrape, "BLOCK_PRIVATE", True)

    def _resolve(host, port, *args, **kwargs):
        assert host == "mixed.example"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    text = tool_error(lambda: server.Scrape("http://mixed.example/x"))
    assert text.startswith("Scrape error: refusing a private or local address")


def test_scrape_block_private_refuses_a_redirect_to_a_private_address(monkeypatch):
    request = urllib.request.Request("http://public.example/x")
    with pytest.raises(ValueError, match="refusing a private or local address"):
        scrape._RefusePrivateRedirects().redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/admin")


def test_scrape_never_reads_a_redirect_s_own_body(pages):
    # review: CPython's redirect handler read the 30x body with a bare fp.read(), past the cap
    base, handler = pages
    handler.bodies["/target"] = b"<p>tu</p>"
    handler.redirect_body = b"r" * (40 * 1024 * 1024)
    was_tracing = tracemalloc.is_tracing()  # PYTHONTRACEMALLOC or a plugin may already trace
    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        assert server.Scrape(f"{base}/redirect/target").strip() == "tu"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"{peak} bytes allocated: the redirect body was read"


def test_scrape_default_follows_a_redirect_on_the_lan(pages):
    base, handler = pages
    handler.bodies["/target"] = b"<p>tu</p>"
    assert server.Scrape(f"{base}/redirect/target").strip() == "tu"
    assert handler.hits == ["/redirect/target", "/target"]


# ---------------------------------------------------------------------------
# Bind guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "localhost", "LOCALHOST", "::1", "[::1]"])
def test_loopback_hosts(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "192.168.1.5", "100.104.8.125", "dev1", "[::]"])
def test_non_loopback_hosts(host):
    assert is_loopback_host(host) is False


def test_an_unauthenticated_non_loopback_bind_with_tier3_is_refused():
    with pytest.raises(ValueError, match=r"^refusing to bind 0\.0\.0\.0 without an auth key while Tier 3"):
        check_bind_exposure("0.0.0.0", None, tier3_enabled=True)


@pytest.mark.parametrize(
    ("host", "key", "tier3", "warning"),
    [
        ("0.0.0.0", "k", True, None),
        ("127.0.0.1", None, True, None),
        ("127.0.0.1", None, False, None),
        ("0.0.0.0", None, False, "open to the network without auth"),
        ("192.168.1.5", "", False, "open to the network without auth"),
    ],
)
def test_bind_exposure_allows_a_key_loopback_or_tier_1_2(host, key, tier3, warning):
    result = check_bind_exposure(host, key, tier3_enabled=tier3)
    if warning is None:
        assert result is None
    else:
        assert warning in result


def test_an_empty_key_counts_as_no_key():
    with pytest.raises(ValueError):
        check_bind_exposure("0.0.0.0", "", tier3_enabled=True)


# ---------------------------------------------------------------------------
# TOML booleans
# ---------------------------------------------------------------------------


def _config(tmp_path, text: str):
    path = tmp_path / "remoteos.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


@pytest.mark.parametrize(
    ("raw", "value"),
    [("true", True), ("false", False), ('"false"', False), ('"FALSE"', False), ('"0"', False), ('"no"', False),
     ('"true"', True), ('"1"', True), ('"Yes"', True), ('" yes "', True)],
)
def test_toml_booleans_parse_strictly(tmp_path, raw, value):
    cfg = _config(tmp_path, f"[security]\nenable_tier3 = {raw}\ndisable_tier2 = {raw}\nscrape_block_private = {raw}\n")
    assert cfg.security.enable_tier3 is value
    assert cfg.security.disable_tier2 is value
    assert cfg.security.scrape_block_private is value


@pytest.mark.parametrize("raw", ['"maybe"', '""', "1", "0", "[true]", '"tru e"', '"hunter2-secret"'])
def test_a_toml_boolean_that_is_not_one_names_the_key_and_never_echoes_the_value(tmp_path, raw):
    with pytest.raises(ValueError) as exc:
        _config(tmp_path, f"[security]\nenable_tier3 = {raw}\n")
    # one fixed text whatever the value: it can never echo it
    assert str(exc.value) == (
        "security.enable_tier3 must be a boolean: true or false, or one of the strings true, false, 1, 0, yes, no"
    )


def test_scrape_block_private_defaults_to_off(tmp_path):
    assert _config(tmp_path, "").security.scrape_block_private is False
