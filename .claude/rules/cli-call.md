---
paths:
  - "src/remoteos/cli_call.py"
  - "tests/test_cli_call.py"
---

# `remoteos-call`, the script client (#70) — gotchas

- **One module, `remoteos/cli_call.py`**: `plan()` (the command line's shape, checked BEFORE the
  config is read, so a usage error is always 2) → config resolution → fastmcp `Client` over
  `StreamableHttpTransport` → reply parsing → exit code. The exit-code contract (Shell = remote
  code, 0, 125 tool error, 126 no result, 141 broken pipe, 2 usage) is what camera-box#1480 and
  restreamer#422 build on: never renumber it. `main` turns ANY other exception into one
  `remoteos-call: unexpected <Type>` line and 126: never a traceback, never exit 1 (a remote 1).
- **The config is read with the gateway's helpers** (`gateway/registry.py`: `read_json_file`,
  `authorization_header` (any spelling, the last one wins), `is_http_url` (urlsplit can RAISE)),
  so the CLI and the gateway agree on what a valid entry is.
- **Every tool reply starts with `[task:<id>] `** (`taskmanager.wrap_sync_tool`), success and
  error alike. `strip_task_prefix` removes it before any parsing or printing.
- **Shell's reply is `ChildOutput.reply()` text**, parsed back by `parse_shell_reply`: the exit
  code only from a FINAL `[Exit code: N]` line, the split at the FIRST `\n[STDERR] ` (or a leading
  `[STDERR] ` when stdout is empty), `(no output)` = nothing. Plain text cannot round-trip every
  output (the four cases are in the README); a change to `reply()` breaks this parser: change
  both, with the pure parser tests.
- **One reply = ONE server-sent event, and httpx2's `EventSource` refuses an event over 1 MiB
  by default** (`SSEError`, surfacing as "SSE stream ended without a response"): a FileDownload of
  ~780 KB or 1 MB of Shell output failed. `mcp.client.streamable_http` builds `EventSource(response)`
  with no option, so `_allow_large_replies()` swaps that module's `EventSource` name for a subclass
  defaulting to `MAX_REPLY_EVENT_BYTES` (256 MiB) — a process-local patch, idempotent, and it
  RAISES if the SDK stops using `httpx2.EventSource` (never a silent fallback to 1 MiB). The 2.5 MB
  Shell and 5 MB get tests fail without it.
- **Request size: a machine takes 4 MiB per request** (mcp `transport_security.DEFAULT_MAX_REQUEST_BODY_SIZE`;
  the gateway's own cap is 16 MiB), so a `put` carries about 3 MB and the 413 text says so. Both
  the gateway and the direct entry are tested at 3.5 MB.
- **The MCP SDK hides HTTP statuses.** A non-2xx answer to a request becomes a JSON-RPC error
  "Server returned an error response" (mcp `client/streamable_http.py`). So the CLI builds the
  httpx client itself (`httpx_client_factory`) with a response hook (`_HttpErrors`) that records
  the first POST status >= 400; 401/403/404/413 get their own one-line reason from it. That
  client sets `trust_env=False`, like the gateway's own clients: `HTTP(S)_PROXY` would otherwise
  carry the bearer to a proxy, and `~/.netrc` could add credentials.
- **125 vs 126 after the session is up:** an `MCPError` with no HTTP error status is the server's
  own JSON-RPC answer (125), except `REQUEST_TIMEOUT` (-32001, the client's wait ran out) and
  `CONNECTION_CLOSED` (-32000), which are 126. An `isError` result is always 125. A handshake
  timeout is fastmcp's `RuntimeError` from a builtin `TimeoutError`: also "timed out".
- **`mode="legacy"`** (the plain initialize handshake): every fleet machine and the gateway's
  probe speak it; `"auto"` would probe `server/discover` on every call first.
- **The client's wait = `taskmanager.declared_runtime()`** (the declared `timeout`/`seconds` + 60,
  the server's own stale-task sweep margin) for Shell and for a `--json` call that declares one;
  else `--timeout`, default 300 s. Through the gateway a call must answer within its 600 s
  upstream read timeout.
- **Output:** `_echo` = `click.echo(..., color=True)`: plain `click.echo` strips ANSI escapes when
  stdout is not a tty, which changed remote output. A closed stdout is caught in `main`
  (`sys.stdout.flush()` inside the try), stdout is pointed at /dev/null so the interpreter's
  exit flush stays silent, and the exit code is 141.
- **The token** stays in `Machine.authorization` (`repr=False`) and the request header. Every
  shown message goes through `_scrub` (the token and the whole `Bearer …` value) and
  `_display_url` (no userinfo, no query). The fastmcp/mcp/httpx2/httpcore2 loggers log tool
  ARGUMENTS and replies at DEBUG, and fastmcp attaches its own stderr handler at import
  (`FASTMCP_LOG_LEVEL`): `_quiet_library_logging()` sets them above CRITICAL after the import.
  Never add a `--debug` that turns them on.
- **Tests (`tests/test_cli_call.py`) run the REAL stack:** `python -m remoteos --enable-all` keyed
  through `REMOTEOS_AUTH_KEY`, and `remoteos-gateway serve` in front of it, both module-scoped on
  free loopback ports with a hermetic env (no `REMOTEOS_*`/`FASTMCP_*`, temp HOME). The `run`
  fixture checks that neither the token nor the machine key reaches stdout, stderr, a captured log
  record (propagating loggers at DEBUG; the library loggers are silenced by the CLI itself), the
  gateway access log or either server's log. The console script runs as a real process twice
  (with `FASTMCP_LOG_LEVEL=DEBUG`, and into a closed pipe). Reply checks that a real server cannot
  produce (a size mismatch) fake the remote reply at `cli_call._run`, the network boundary.
  The CLI's two process-wide settings (library logger levels, the SDK's `EventSource`) persist
  in the pytest process after an in-process run; the autouse
  `_restore_process_wide_cli_settings` fixture puts them back (without it,
  `test_tool_errors`' fastmcp log check failed after this module).
