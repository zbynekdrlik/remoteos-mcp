---
paths:
  - "src/remoteos/gateway/**"
  - "tests/test_gateway*.py"
  - "tests/gateway_harness.py"
  - "tests/conftest.py"
  - "tests/fleet_harness.py"
  - "deploy/remoteos-gateway*"
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

## Every project through the gateway (#51): key store, manifest, sync-configs, check

- **Modules:** `keystore.py` (machines.json I/O, `set_key`, the import), `manifest.py`
  (projects.toml), `sync.py` (the generator), `targets.py` (local / `sudo -n -u` /
  `ssh -o BatchMode=yes`), `fileops.py` (atomic write + backup + lock), `probe.py` (the
  `initialize` probe), `check.py` (the watchdog), `cli_fleet.py` (the commands, registered on
  `cli.main`). The dependency direction is one way: `registry` ← `keystore` ← `sync` /
  `check` ← `cli_fleet` ← `cli`.
- **The key-store shape lives in `registry.py`** (`parse_keystore`, `MachineEntry`,
  `RepairRecipe`), because `load_machines` must read it; `keystore.py` builds on it.
  - `load_machines` tells the shapes apart by the `mcpServers` key: the legacy one skips bad
    entries with a warning (a shared file), the key store refuses the WHOLE file on one bad
    entry (the gateway keeps its last good copy).
  - `--machines` defaults to the key store, so after an upgrade the gateway must not restart
    before `machine import` ran (README migration order).
- **`fileops.py` is stdlib-only, py3.8+, with no package imports:** `targets.py` runs its
  SOURCE as `python3 -c` under sudo or over ssh, so the three writers share one tested
  implementation.
  - File contents go on stdin/stdout only. An argv holds the source, an op and a path.
  - `~` is the account's passwd home, never `$HOME` (sudo may keep the caller's).
  - A symlink is written through (`realpath`): a project's `.mcp.json` symlink stays one.
  - A same-day backup is never overwritten (`name.1`, `.2`, ...).
- **What sync owns:** an entry whose url is one sync writes (`<gateway>/<name>/mcp`, or in
  direct mode the key store's url). A manifest machine is always (re)written. An owned entry
  whose machine left the manifest is removed. ANY other entry is kept, even when its name is in
  the key store: dev2's `linux-presenter` / `win-marek-nb` may not be reachable from dev1, and
  deleting them would break those projects.
- **Byte-equal non-fleet entries:** `sync.parse_config` walks `mcpServers` with
  `JSONDecoder.raw_decode` and keeps each value's source slice. `render_config` re-joins the
  slices; only generated entries are serialised (`json.dumps(indent=2)` re-indented to the
  entry's column). Never "load + json.dump" a project config: that rewrote `reaperiem.mcp.json`
  (not in indent-2 form).
- **Token order (no window with an unknown token):** register the new hash (keep the old) →
  save → write the file → prune to `[hash]` → save. A write that REPORTS failure may still have
  landed (an ssh drop after the remote finished, a lost reply, an fsync error after the
  rename), so it is NEVER rolled back (review round 1): both hashes stay, and the next run
  reuses whichever token the file holds. A failing project's allow-list only ever NARROWS. A
  token is reused only when its hash is registered for THIS managed consumer; an unregistered
  token in a file is never adopted.
- **`managed_by: "sync-configs"`** marks a consumer that sync owns. A managed consumer whose
  project left the manifest keeps its hash with `machines = []` (never broader than the
  manifest; undoing a mistaken edit needs no new token and no restart); `--prune` deletes it.
  An empty manifest is refused outright. A same-name consumer sync did not create is refused.
  `Consumer.to_json` omits the field when it is empty, so hand-made consumers keep the #25 shape.
- **Exposure is a kill switch sync never undoes:** sync warns about an unexposed manifest
  machine. `machine import` and `machine set-key --url` expose only machines NEW to the key store.
- **Import never downgrades the key store:** when no copy of a machine verifies, the key
  store's current entry is kept (a set-key made while the box was down), never the most
  common config copy. For a machine already in the key store, only copies at ITS url count:
  a 200 from another url only proves the key fits that url (a stale IP reused by another box).
  Other-url copies are listed, never probed (the key is not sent there), unless
  `--accept-url-change`. `apply_import(snapshot=...)` leaves a machine whose entry changed
  while the import probed.
- **`consumer add` / `allow` refuse a sync-managed consumer** (`Registry._refuse_if_managed`):
  its allow-list must stay the manifest's. `deny` / `revoke` (narrowing) still work.
- **`edit_keystore` saves only when the content changed:** every save makes the running
  gateway reload, and a no-op (the repair race's early return) must not.
- **`check`:**
  - `notified: false` survives a failed notification, so the next run retries it.
  - **`airuleset.py notify --body` exits 0 even when the Discord post failed** (review round 2):
    it PRINTS `send()`'s status (`sent` / `dedup` / `dry-run` / `no-config` / `error`). Only a
    printed `sent` / `dedup` is delivered. Every attempt passes its own `--dedup-key`, so a
    retry is never deduped away. The test fake prints a status and exits 0, like the real tool.
  - A check that cannot run because of an invalid key store is announced once per distinct
    failure, and its recovery is announced too. An UNWRITABLE status file is announced on every
    run: there is nowhere to remember that it was. Validation messages never repeat a value (a
    key pasted into `url` would reach Discord): length and scheme only.
  - `urlsplit` RAISES `ValueError` on some strings (`http://[bad/mcp`, an invalid IPv6 literal),
    and its `.port` raises on a bad port (`:99999`, `:80a`). `registry._is_http_url` evaluates
    both inside one `try` and requires a hostname, so such a url is just "not http(s)" in the
    key store, the gateway reload and import (review rounds 3-4: each crashed check silently).
    Second line of defence: `probe.gather_probes` turns any probe that still raises into an
    `error` result for that machine alone.
  - airuleset CUTS a body at 1900 characters (mention included) and still prints `sent`: a
    fleet-wide change goes out as several messages of at most `CHUNK_LIMIT` (1700), and only the
    machines of a DELIVERED message count as notified. While an announcement is still owed, the
    record keeps the detail of the change (`repaired: ...`), so a retry still says what happened.
- **Dry run = one choke point:** `sync._Saver(dry_run=True).commit()` never writes. Registry
  edits of a dry run (narrowing, withdrawing, `--prune`) happen in memory only. Never add a
  second save path around it.
- **Import writes no empty key store** (the gateway would then serve nothing, where with no
  key store it refuses to start): zero machines, or an unreadable `--from`, exits 1. A NEW
  machine a consumer already allows but that is not exposed (a kill switch from before #51)
  stays unexposed, with a warning; the dry run lists what it would expose.
  - A machine first seen `ok` is not announced.
  - A repair stores the re-read key only if the machine accepts it AND the stored url + key did
    not change meanwhile (a concurrent `set-key` wins).
  - No single machine can blind the run (review rounds 4-5): a probe that raises
    (`gather_probes`), a key store the repair cannot write (`OSError`), a key file nested too
    deep (`RecursionError`), or any other repair exception (the guard in `observe`) becomes a
    result / 401 detail for THAT machine; the status is still written and the rest announced.
  - The notification (Slovak, phone-readable) goes out through
    `airuleset.py notify --body [--owner-name]`. The unit sets
    `REMOTEOS_GATEWAY_NOTIFY_OWNER`: a headless caller has no tmux pane for airuleset to find
    the owner from.
- **Tests:** `tests/fleet_harness.py`.
  - `FakeFleetMachine` is a REAL `ThreadingHTTPServer` on 127.0.0.1, so CLI commands that run
    their own `asyncio.run` can probe it from any thread.
  - The fake `ssh` / `sudo` / `airuleset.py` log their argv. Assert that no secret is in those
    argvs: that is the "contents on stdin only" proof.
  - A CLI call from inside a running loop (the live-reload test) goes through
    `asyncio.to_thread`.
