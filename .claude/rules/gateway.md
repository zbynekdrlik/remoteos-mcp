---
paths:
  - "src/remoteos/gateway/**"
  - "tests/test_gateway*.py"
  - "tests/gateway_harness.py"
  - "tests/conftest.py"
  - "deploy/remoteos-gateway.service"
---

# remoteos-gateway (dev1 MCP gateway, #25) — gotchas

- **HTTP client is `httpx2`, not `httpx`.** `httpx2` is what fastmcp and mcp use at runtime,
  and it is pinned directly. Plain `httpx` exists only in the `test` extra.
  - `client.send(httpx2.Request(...), stream=True)` sends the request AS-IS. No client default
    headers are added (no `Accept-Encoding`, no cookies), which is what keeps the proxy
    transparent. Do not switch to `client.build_request()`.
  - Relay the body with `aiter_raw()`, never `aiter_bytes()`. The body must pass through raw,
    so `Content-Encoding` and `Content-Length` stay valid.
- **`httpx2.ASGITransport` buffers the whole response body.** A test through it cannot prove
  streaming. Drive the ASGI app directly with a custom `send` (see
  `test_sse_is_streamed_chunk_by_chunk_not_buffered`): the upstream yields part 2 only after
  the consumer has received part 1, so a buffering proxy deadlocks into the 5 s timeout.
- **uvicorn advertises ASGI spec_version 2.3.** So Starlette's `StreamingResponse` runs
  `listen_for_disconnect` and cancels the stream on disconnect.
  - Its `background=` task does NOT run when the stream raises. That is why
    `_UpstreamResponse.__call__` closes the upstream and writes the log line in a `finally`.
  - `test_upstream_is_closed_even_when_the_consumer_vanishes_before_headers` locks this in.
- **Upstream keep-alive (#25 reopened).** `UPSTREAM_LIMITS.keepalive_expiry` (2.0 s) MUST stay
  well below the machines' uvicorn `timeout_keep_alive` (5 s). With equal values, a call after
  ~4.9 s idle reused a connection the machine was closing, and the consumer got 503
  `RemoteProtocolError: Server disconnected without sending a response`.
  - `Gateway._send_upstream` retries ONCE, only for `RemoteProtocolError`/`ReadError`/`WriteError`
    raised by `send()` (pre-headers), and only when that attempt REUSED a pooled connection
    (httpcore `trace` extension: no `connection.connect_*` event). Never timeouts, connect errors,
    a freshly dialled connection, or mid-stream.
  - The retry goes through `retry_client` (`max_keepalive_connections=0`), so it always dials. The
    main pool would hand it the next stale connection after a machine restart.
  - Double-run safety rests on two machine properties: uvicorn discards bytes racing its
    keep-alive close, and machines answer `tools/call` as SSE (headers before the tool runs).
    Never start a machine with fastmcp `json_response` on (`FASTMCP_JSON_RESPONSE`).
  - Pool tests run a REAL loopback server against the production clients
    (`tests/test_gateway_upstream_pool.py`). Pin a long expiry (`long_keepalive`) in tests that
    are about the retry, so a stalled runner cannot expire a connection between calls.
- **Decision order:** malformed machine name → 404 (before auth, so the raw value never
  reaches a log), then 401 → 403 → 404 (the design order), then 413 (body over the cap).
  - The 403 comes before the 404 so an authenticated consumer cannot probe which other
    machines are exposed.
  - The body is read only after the allow-list and exposure checks pass.
- **Consumer-supplied strings** (`rpc_method`, `tool`) go into operational log lines only via
  `%r`. Machine and consumer names are `fullmatch`-validated: `$` would accept a trailing
  `\n`.
- **Never log or forward the consumer token, the machine key, or tool arguments.**
  - `Upstream.authorization` is `repr=False`.
  - The access log keeps only `rpc_method` and the `tools/call` `params.name`.
  - Tests assert all three never appear.
- **Fail closed.** A deleted registry is an empty registry (deny all). A broken registry or
  machine source at reload keeps the last good copy. At startup, `serve` refuses to start
  on a broken file.
- **Test fixtures:** `block-sensitive-staging.sh` flags `TOKEN = ...` in tests even though the
  values come from `secrets.token_hex`. Append `# airuleset:secret-ok <reason>` to BOTH the
  `git add` and the `git commit`.
- **Rollout (dev1, owner account):** follow the README section "MCP gateway (dev1)".
  - Install into a DEDICATED venv, `~/.local/share/remoteos-gateway/venv`, with
    `-c constraints.txt`. That venv is the unit's `ExecStart`.
  - NEVER `pip install --user`: it would upgrade `fastmcp`/`mcp` in the account's shared user
    site, which is the #12 fastmcp-slim collision class.
  - Upgrade = the same `pip install` line with `--force-reinstall`, then
    `systemctl --user restart remoteos-gateway`, then check the version on `/healthz`.
  - After install: `remoteos-gateway expose <machine>`, then verify from another tailnet node:
    200 tools/list, 401 wrong token, 403 not allowed.
- **Unit hardening:** only `NoNewPrivileges`, `RestrictAddressFamilies` and `MemoryMax` are
  enforced for a `--user` unit on dev1.
  - `ProtectSystem`/`ProtectHome`/`PrivateTmp` are VERIFIED-INERT there:
    `apparmor_restrict_unprivileged_userns=1` blocks the mount namespace they need.
  - Never claim them as isolation the gateway has.
