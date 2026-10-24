---
paths:
  - "src/remoteos/cli_call.py"
  - "tests/test_cli_call.py"
---

# `remoteos-call`, the script client (#70) — gotchas

- **One module, `remoteos/cli_call.py`**: config resolution → fastmcp `Client` over
  `StreamableHttpTransport` → reply parsing → exit code. The exit-code contract (Shell = remote
  code, 0, 125 tool error, 126 no call, 2 usage) is what camera-box#1480 and restreamer#422 build
  on: never renumber it.
- **Every tool reply starts with `[task:<id>] `** (`taskmanager.wrap_sync_tool`), success and
  error alike. `strip_task_prefix` removes it before any parsing or printing.
- **Shell's reply is `ChildOutput.reply()` text**, parsed back by `parse_shell_reply`: the exit
  code only from a FINAL `[Exit code: N]` line, the split at the FIRST `\n[STDERR] ` (or a leading
  `[STDERR] ` when stdout is empty), `(no output)` = nothing. A change to `reply()` breaks this
  parser: change both, with the pure parser tests. Structured Shell output would remove the
  ambiguity (a follow-up, not done).
- **The MCP SDK hides HTTP statuses.** A non-2xx answer to a request becomes a JSON-RPC error
  "Server returned an error response" (mcp `client/streamable_http.py`). So the CLI builds the
  httpx client itself (`httpx_client_factory`) with a response hook (`_HttpErrors`) that records
  the first status >= 400; 401/403/404/413 get their own one-line reason from it.
- **125 vs 126 after the session is up:** an `MCPError` with no HTTP error status is the server's
  own JSON-RPC answer (125), except `REQUEST_TIMEOUT` (-32001, the client's wait ran out) and
  `CONNECTION_CLOSED` (-32000), which are 126. An `isError` result is always 125.
- **`mode="legacy"`** (the plain initialize handshake): every fleet machine and the gateway's
  probe speak it; `"auto"` would probe `server/discover` on every call first.
- **The client's wait:** Shell = its `timeout` + `SHELL_WAIT_MARGIN_S` (60, the same margin as the
  server's stale-task sweep); put/get/`--json` = `--timeout`, default 300 s.
- **The token** stays in `Machine.authorization` (`repr=False`) and the request header. Every
  shown message goes through `_scrub` (the token and the whole `Bearer …` value) and
  `_display_url` (no userinfo, no query). The mcp/httpcore DEBUG loggers log tool ARGUMENTS and
  replies (file contents too): the CLI configures no logging, so they stay silent; never add a
  `--debug` that turns them on.
- **Tests (`tests/test_cli_call.py`) run the REAL stack:** `python -m remoteos --enable-all` keyed
  through `REMOTEOS_AUTH_KEY`, and `remoteos-gateway serve` in front of it, both module-scoped on
  free loopback ports with a hermetic env (no `REMOTEOS_*`/`FASTMCP_*`, temp HOME). The `run`
  fixture captures DEBUG from EVERY logger and checks that neither the token nor the machine key
  reaches stdout, stderr, a log record, the gateway access log or either server's log. The
  console script itself is run once as a process (`Path(sys.executable).with_name("remoteos-call")`).
- **put through the gateway is capped at 16 MiB of request** (`gateway/app.py` `MAX_BODY_BYTES`),
  about 12 MB of file: 413 → 126. A larger transfer needs chunked FileUpload (a follow-up).
