---
paths:
  - "src/remoteos/cli_call.py"
  - "tests/test_cli_call.py"
---

# `remoteos-call`, the script client (#70) — gotchas

- **One module, `remoteos/cli_call.py`**: `plan()` (the command line's shape, checked BEFORE the
  config is read, so a usage error is always 2) → config resolution → fastmcp `Client` over
  `StreamableHttpTransport` → reply parsing → exit code. The exit-code contract (Shell = remote
  code, 0, 125 tool error, 126 no result, 130 interrupted, 141 broken pipe, 2 usage) is what
  camera-box#1480 and restreamer#422 build on: never renumber it. `main` turns ANY other
  exception into one `remoteos-call: unexpected <Type>: <first line>` line (the token scrubbed)
  and 126, and Ctrl-C into 130 (`sys.exit`, not a re-raised SIGINT: a re-raise would kill a
  `CliRunner` test process, and the README tells bash loops to check for 130): never a traceback,
  never exit 1 (a remote 1) — except a Ctrl-C during the imports, before `main` runs.
- **`write_atomically` follows a symlink only after `os.stat` did in the kernel**, so
  `fs.protected_symlinks` still applies (a link planted in a sticky dir is EACCES -> 126); a
  dangling link is refused, like GNU `cp`.
- **The config is read with the gateway's helpers** (`gateway/registry.py`: `read_json_file`,
  `authorization_header` (any spelling, the last one wins), `is_http_url` (urlsplit can RAISE)),
  so the CLI and the gateway agree on what a valid entry is.
- **The task manager prefixes `[task:<id>] ` to the FIRST TEXT block** (`taskmanager.wrap_sync_tool`),
  success and error alike — not always the first block (Snapshot: image first), and a reply
  without text gets a prefix-only text block in front. `result_text` strips it ONCE there and drops
  a block it empties; nothing downstream strips again (a stdout that itself starts with
  `[task:x] ` must survive — tested).
- **Shell's structured content wins (`shell_reply`, #67):** `stdout`, `stderr`, `exit_code`,
  exact, printed with `nl=False`. Anything else in `structured_content` (an older server's
  `{"result": text}`) falls back to the parser, one trailing newline per stream.
- **Fallback: Shell's reply is `ChildOutput.reply()` text**, parsed back by `parse_shell_reply`: the exit
  code only from a FINAL `[Exit code: N]` line, the split at the FIRST `\n[STDERR] ` (or a leading
  `[STDERR] ` when stdout is empty), `(no output)` = nothing. Plain text cannot round-trip every
  output (the four cases are in the README); a change to `reply()` breaks this parser: change
  both, with the pure parser tests.
- **One reply = ONE server-sent event, and httpx2's `EventSource` refuses an event over 1 MiB
  by default** (`SSEError`, surfacing as "SSE stream ended without a response"): a FileDownload of
  ~780 KB or 1 MB of Shell output failed. `mcp.client.streamable_http` reads a POST reply through
  its module-level `EventSource` name: mcp 2.2 (constraints.txt, what the installers ship) as
  `EventSource(response)`, mcp 2.3 (what CI's unconstrained `pip install -e .[test]` resolves) with
  `max_event_size=self.max_sse_event_size`, which fastmcp 4.1.2 leaves at 1 MiB. So
  `_allow_large_replies()` swaps that name for a subclass that IGNORES the passed value and always
  uses `MAX_REPLY_EVENT_BYTES` (256 MiB) — a first version that only changed the DEFAULT passed
  locally (2.2) and failed in CI (2.3). Run `test_cli_call` against both before touching it. A
  process-local patch, idempotent, and an SDK
  that stops using `httpx2.EventSource` is a named `CallFailed` ("this mcp SDK is incompatible"),
  never a silent fallback to 1 MiB (tested). The 2.5 MB Shell and 5 MB get tests fail without it.
  This swap and the logger silencing are `prepare_process()`, called by `main` only (never by
  `call_tool`, so an in-process caller keeps its own logging and SDK).
- **Request size: a machine takes 4 MiB per request** (mcp `transport_security.DEFAULT_MAX_REQUEST_BODY_SIZE`;
  the gateway's own cap is 16 MiB). Both are tested at 3.5 MB, by raising `PUT_CHUNK_BYTES`.
- **`put` (#67):** `PUT_CHUNK_BYTES` (2.5 MB) chunks to `<remote>.part`, then FileHash, then
  FileRename. The first chunk always carries `append=False` and `permissions_from=<remote>`,
  explicitly: the server drops a leftover `.part` (even a read-only one), starts the new one with
  the target's mode so a 0600 target's content is never in a 0644 file, and refuses a directory
  target before any data moves. An older server refuses the unknown arguments before writing
  anything (`unexpected_keyword_argument`, named in one line). Each call is its own `_run` (session), so
  `--timeout` is per call, and a test can cut a real put at the network boundary. `_run` per chunk
  costs a handshake each: nothing next to the chunk itself.
- **The MCP SDK hides HTTP statuses.** A non-2xx answer to a request becomes a JSON-RPC error
  "Server returned an error response" (mcp `client/streamable_http.py`). So the CLI builds the
  httpx client itself (`httpx_client_factory`) with a response hook (`_HttpErrors`) that records
  the first POST status >= 400; 401/403/404/413 get their own one-line reason from it. That
  client sets `trust_env=False`, like the gateway's own clients: `HTTP(S)_PROXY` would otherwise
  carry the bearer to a proxy, and `~/.netrc` could add credentials.
- **125 vs 126 after the session is up:** an `MCPError` with no HTTP error status is the server's
  own JSON-RPC answer (125), except `REQUEST_TIMEOUT` (-32001, the client's wait ran out) and
  `CONNECTION_CLOSED` (-32000, "connection closed before the reply (the command may have run)":
  a dropped stream, the gateway's 3700 s cutoff, a reply over 256 MiB), which are 126. An
  `isError` result is always 125. A handshake timeout is fastmcp's `RuntimeError` from a builtin
  `TimeoutError`: also "timed out".
- **`write_atomically` (get) behaves like `cp`:** a symlink is written through (`realpath`), an
  existing file keeps its mode, a new one is created `O_CREAT|O_EXCL` with `0o666` so the KERNEL
  applies the umask (never `os.umask` read-and-reset); data fsynced, renamed, directory fsynced.
- **`mode="legacy"`** (the plain initialize handshake): every fleet machine and the gateway's
  probe speak it; `"auto"` would probe `server/discover` on every call first.
- **The client's wait = `taskmanager.declared_runtime()`** (the declared `timeout`/`seconds` + 60,
  the server's own stale-task sweep margin) for Shell and for a `--json` call that declares one;
  else `--timeout`, default 300 s. Through the gateway a call must answer within its 3700 s
  upstream read timeout (`--timeout 3600` for Shell, #67).
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
  free loopback ports with a hermetic env (`fleet_harness.free_port` / `hermetic_child_env`,
  shared with `test_startup_flags`: no `REMOTEOS_*`/`FASTMCP_*`, temp HOME). The `run`
  fixture checks that neither the token nor the machine key reaches stdout, stderr, a captured log
  record (propagating loggers at DEBUG; the library loggers are silenced by the CLI itself), the
  gateway access log or either server's log. The console script runs as a real process twice
  (with `FASTMCP_LOG_LEVEL=DEBUG`, and into a closed pipe). Reply checks that a real server cannot
  produce (a size mismatch) fake the remote reply at `cli_call._run`, the network boundary.
  The CLI's two process-wide settings (library logger levels, the SDK's `EventSource`) persist
  in the pytest process after an in-process run; the autouse
  `_restore_process_wide_cli_settings` fixture puts them back (without it,
  `test_tool_errors`' fastmcp log check failed after this module).
