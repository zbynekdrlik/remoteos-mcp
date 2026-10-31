---
paths:
  - "src/remoteos/__main__.py"
  - "src/remoteos/app.py"
  - "src/remoteos/tools/**"
  - "src/remoteos/taskmanager.py"
  - "src/remoteos/platform/linux/shell_env.py"
  - "src/remoteos/identity.py"
  - "tests/test_identity.py"
  - "tests/test_startup_flags.py"
  - "tests/test_taskmanager.py"
  - "tests/test_shell_env.py"
  - "tests/test_shell_tool.py"
---

# Server start, logging, Shell/FileUpload, task states (#31) — gotchas

- **Where the server lives (#49).** `app.py` = the `mcp` instance, `/health`, the identity, the
  helpers several tool modules share, and the Windows DPI + pyautogui set-up. `tools/*.py` = the
  tools by area, registered on `mcp` when `tools/__init__.py` imports them. `__main__.py` = the CLI,
  the start-up and the task-wrapping pass; only importing `remoteos.__main__` wraps the tools.
  - **`tools/list` order = registration order = the import order in `tools/__init__.py`, then the
    order inside each module.** That is why EventLog stays at the end of `tools/network.py`.
    `tests/test_tools_list_golden.py` pins names, schemas, annotations, descriptions and order.
    A deliberate tool change regenerates `tests/golden/tools_list.json` (see that test's docstring).
  - The Linux provider follows the same split: `platform/linux/desktop.py` is the facade over
    `session.py`, `windows.py`, `x11_capture.py` and `x11_input.py`.
- **FastMCP 4.x (4.1.2 since #39) `run_http_async` takes `uvicorn_config`, never `uvicorn_args`.** A wrong
  keyword is a TypeError at start, before the port binds. Check the real signature in
  `fastmcp/server/mixins/transport.py` when you add a run kwarg.
- **Never pass `log_level="debug"` to `mcp.run`.** It turns FastMCP to DEBUG, and FastMCP and
  sse_starlette then log every tool call's arguments and reply: typed text, clipboard, Shell
  commands, FileUpload data.
  - `--debug` = a stderr handler on the `remoteos` logger tree only, plus uvicorn's
    `uvicorn_config["log_level"]`.
  - Our own DEBUG lines must stay payload-free too. The Linux `x-run` line goes through
    `_loggable`, which shows `xdotool type` text as `<N chars>`.
- **`--reload` cannot work.** FastMCP hands uvicorn an app OBJECT, and uvicorn reloads only an
  import-string app via `uvicorn.run`. The flag only prints a warning.
- **stdio: stdout IS the JSON-RPC stream.** Every CLI status line goes to stderr
  (`click.echo(..., err=True)`). `test_stdio_stdout_carries_only_json_rpc` locks this.
- **The server names its machine: `remoteos/identity.py` (#59).** `app` reads
  `machine_identity()` ONCE at import (`_IDENTITY`) and passes `server_instructions(**_IDENTITY)`
  to `FastMCP(instructions=)`, which goes out in every `initialize` result; `/health` adds `os` +
  `hostname`. Why: a box's MCP name is only a label (stream-snv kept `win-stream-snv` after its
  Ubuntu reinstall, and clients sent it PowerShell).
  - `/health`'s `status` and `version` are a contract: the installers' self-check reads
    `.version` (install-linux.sh, install.sh, install.ps1). Only ADD keys there.
  - `/health` needs no key (`auth._PUBLIC_PATHS`): every key added there is public on the port.
    Hostname and OS are accepted (#59); nothing secret or per-user goes there.
  - `admin` (#57, `identity.has_admin`): Windows the elevated token, else root or `sudo -n true`.
    Cached 60 s, never read once at import: the installers write the sudoers drop-in AFTER their
    proof and restart nothing. Cached because `/health` is public; a thread, because sudo blocks.
  - Windows OS name = `Windows <release> (build <version>)`, but a Server edition
    (`platform.win32_edition()` contains "Server") is `Windows Server (build <version>)`: CPython
    3.12's `release()` says "2022Server" for every Server build from 20348 up (windows-latest,
    Server 2025, included).
  - The POSIX shells are `identity.MACOS_SHELL` / `LINUX_SHELL`, and `Shell` builds its argv from
    them; the Windows text describes `powershell_argv`. Changing what Shell runs means changing
    `identity` too — `test_shell_runs_exactly_the_shell_identity_announces` fails otherwise.
  - `identity` imports `is_windows`/`is_macos` by name: a test that fakes the OS patches BOTH
    `server` and `identity`.
  - Linux OS name = os-release `PRETTY_NAME`, else `platform.system() release()`. CPython caches
    os-release and defaults `PRETTY_NAME` to "Linux", so the fallback fires only without a file,
    with a file that is not UTF-8 (`UnicodeDecodeError`, caught as `ValueError`: `identity` runs
    at import and must never stop the server), or with an empty value.
  - Real-OS checks: `test_startup_flags` (initialize over HTTP on the Linux runner) and
    `tests_windows/test_identity_windows.py`.
- **Startup tests start the real server in a subprocess.** The env must be hermetic (see
  `tests/fleet_harness.py` `hermetic_child_env`, shared with `test_cli_call`):
  - `FASTMCP_CHECK_FOR_UPDATES=off`; otherwise the banner calls pypi.org and writes
    `~/.local/share/fastmcp/version_cache.json`.
  - A temp HOME and XDG_DATA_HOME, `--config` with an empty file, and this checkout's
    `src` first on PYTHONPATH.
- **Shell `cwd` never goes into the command text.** It is expanded with
  `expandvars(expanduser())`, and a path that is not a directory is refused before anything runs.
  - POSIX: it goes to the child's `Popen(cwd=…)` (through `run_child`).
  - Windows: it goes to `powershell_argv(command, cwd=…)` and `run_child` gets none. The console
    trampoline then runs in the server's directory, where `CreateProcess` looks for `powershell`,
    and starts PowerShell in `cwd` (#48 round 2). See `.claude/rules/child-output.md`.
- **Linux Shell child environment = `platform/linux/shell_env.shell_env()` (#52)**, passed as
  `run_child(…, env=)` (the child's WHOLE environment; `None` inherits). Windows and macOS pass
  nothing. Why: the server is a system unit with `User=`, which has no login-session environment,
  so `systemctl --user` failed ("$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined",
  live on stream-pp) although `/run/user/<uid>/bus` existed.
  - The contract: the server's own environment, plus, for its OWN effective uid only, each
    variable that is unset or EMPTY, and only after what it names is checked:
    - `XDG_RUNTIME_DIR=/run/user/<euid>`: a directory owned by the euid;
    - `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<euid>/bus`: that dir, and `bus` is a socket;
    - `DISPLAY`: the provider's cached session (`desktop.get_session()`) when its `uid` is the
      euid, and only a local display (`:N`, `:N.S`, `unix:N`) whose `/tmp/.X11-unix/X<N>` socket
      exists, is owned by root (Xorg) or the euid (rootless Xorg, Xwayland), and is no symlink
      (`os.lstat`, review round 2). That directory is world-writable: after a logout another local
      user could plant a socket FILE there. An ABSTRACT socket of the same name, which libxcb
      tries first on Linux, is beyond any file check;
    - `XAUTHORITY`: that session's file, if it exists and the child's DISPLAY is the session's
      CHECKED display, also when the DISPLAY is the server's own (review round 2: an `ssh -X`
      server's `localhost:10.0` got the provider's guessed gdm cookie);
    - `WAYLAND_DISPLAY=wayland-0`: a Wayland session of the euid whose `wayland-0` socket is in
      the child's runtime dir. `SessionInfo` has no Wayland name; `wayland-0` is libwayland's own
      default. Do not grow the provider for it (#49).
  - As root (euid 0) NOTHING is added and the session is never read: never another user's bus or
    display (the X tools' `sudo -u` wrap exists for that).
  - **Shell never forces a re-detection of the provider's cache** (review round 1). The design's
    "re-detect when the cached display's X socket is gone" was removed: `get_session(force=True)`
    at a logout wrote `headless` into the SHARED cache, and every desktop tool then refused on
    `mode != "x11"` before `_run_x`'s own self-heal retry could run, until the service restarted.
    Only an EMPTY cache (the start-up detection failed) is filled by `get_session()`'s own first
    use. The socket checks keep a logout honest: no socket, no DISPLAY; the same display back,
    DISPLAY back. A new session on ANOTHER display reaches Shell when the X tools' retry
    re-detects it, or at the next start. Never call `get_session(force=True)` from a consumer of
    the cache.
    - An existing root-owned socket shows at most that a root X server created it: not that it
      is live (a crashed Xorg leaves its file; review round 4), nor whose session it is. A display
      manager (lightdm, sddm) that reuses `:N` for ANOTHER user's login gets the euid's DISPLAY and
      its stale cookie, which X auth refuses. No access is gained (review round 3).
    - Limitation (provider-wide): the cache is filled at start-up, and the unit starts at boot. A
      graphical login after that start is seen neither by Shell's DISPLAY nor by the display-tool
      gating until the service restarts. The runtime dir and the bus do not depend on the session.
  - A failing session lookup (even an unimportable provider) costs only the display variables:
    one WARNING per exception TYPE, not one per call. One INFO line per change names the added
    variables, never a value. Both de-dups run under one lock (`_log_lock`): up to 3 Shell calls
    run at once. The log call stays inside the lock on purpose (review round 5 asked): logged
    outside, two threads could write their lines in swapped order and the last line would name a
    stale set; a stalled stderr already blocks every thread on the logging handler's own lock.
  - **Copy `os.environ` key by key, never `dict(os.environ)`** (`_snapshot`, review round 4).
    `dict()` lists the keys, then reads each; `windows._load_atspi`'s `finally` pops keys in
    another thread, and a key gone in between raised KeyError and failed the Shell call.
    `Popen(env=None)` copied the C environ and never could.
  - Known exception to "never another user's session" (it predates #52): `windows._load_atspi`
    writes the session user's `DBUS_SESSION_BUS_ADDRESS` / `DISPLAY` / `XAUTHORITY` into
    `os.environ` for a moment. A Shell call that copies `os.environ` in that window (a root
    service included) passes them on. Closing it needs a lock shared with `platform/linux/windows.py`.
  - With DISPLAY set, a command that would prompt (`ssh` host key / passphrase, `sudo -A`) may
    open `ssh-askpass` on the session's screen and wait there until the Shell timeout, where it
    used to fail at once (review round 4). The README and the Shell description tell clients to
    run prompting commands non-interactively (`ssh -o BatchMode=yes`).
    - The bus alone does the same (review round 5): a secret-service client (git's libsecret
      helper, `secret-tool`) reaching a LOCKED login keyring (autologin boxes start locked) makes
      gnome-keyring show its unlock dialog on the screen. `BatchMode` does not help: run it with
      `env -u DBUS_SESSION_BUS_ADDRESS` or a short `timeout`.
  - The provider's `/proc` scan can now find a detached Shell-started program's STALE
    `DISPLAY` / `XAUTHORITY` after a re-login on another display (it takes the first euid
    process with both; a resident `xclip` already could). Fixing it belongs in `platform/linux/session.py`
    (match `/proc/<pid>/sessionid`), a follow-up candidate of #52 (see the linux-desktop skill).
  - **A GUI program started from Shell must be detached**: `setsid -f obs >/dev/null 2>&1` (or
    `systemd-run --user obs`, which works now that the bus is set). A plain `obs &` keeps the
    call's stdout/stderr pipes, so the call runs to its timeout and `run_child`'s process-group
    kill takes the program with it. Both halves are pinned with real bash
    (`test_a_gui_program_outlives_the_shell_call_only_when_detached`,
    `test_a_plain_background_program_dies_with_the_shell_call`).
  - Tests (`tests/test_shell_env.py`): a fake `/run/user` + X socket dir under a SHORT
    `mkdtemp(dir="/tmp")` with REAL AF_UNIX sockets (an AF_UNIX path must fit in 108 bytes; pytest's
    `tmp_path` may not), `loginctl` faked at `subprocess.run` to prove a Shell call never reaches
    it, the effective uid, and the owner `os.stat` AND `os.lstat` report (`report_owner`), faked
    only where a test needs another uid (root, another user's dir or socket). A failing detection
    is `pwd.getpwuid` raising. A logout is `tree.unbind(path)`: the socket closes and its file
    goes. Positive display tests use more than display 0 (a GDM Xorg session is often `:1`), and
    every "never override" branch has a case where only that check keeps the value (review
    round 3); a Wayland session's Xwayland display and a two-digit display are pinned too (round
    4). A mutation pass is a sample, not a proof: each review round ran its own and found gaps the
    lane's had not; the lane's 34 chosen mutants of `shell_env.py` were all caught at round 4.
  - `conftest._linux_session_detected_at_start_up` gives EVERY test a headless session cache, as
    a started server has. With an empty cache, the first Linux Shell call ran the host's real
    `loginctl`, and a test that records `subprocess.Popen` caught those calls.
- **FileUpload checks the cap and decodes with `validate=True` BEFORE any mkdir/write.** Only
  whitespace is dropped (`str.translate`). Catch `ValueError`: non-ASCII input raises a plain
  one, not `binascii.Error`.
  - Over HTTP the 100 MB cap is never reached: the mcp transport refuses a request body over
    4 MiB (`mcp/server/transport_security.py` `DEFAULT_MAX_REQUEST_BODY_SIZE`, 413), so one
    FileUpload carries about 3 MB of file, and a larger one goes in pieces with `append` (#67).
    fastmcp 4.1.2 does not pass mcp's `max_request_body_size` through. Replies have no such cap on the server side; the mcp
    CLIENT's 1 MiB SSE event limit is in `.claude/rules/cli-call.md`.
- **`FileRename` (`file_transfer.replace_file`, #67) is `put`'s last step**, so it keeps what an
  in-place write kept: an existing target's mode and group (owner too as root; a group the account
  is not in is logged, not fatal), a symlink written through, an unwritable target refused (a
  rename would replace it where the write failed). ACLs and xattrs are NOT kept: a new inode. It
  fsyncs the data, renames, fsyncs the directory (POSIX). `os.replace` fails across filesystems (a symlink to another mount):
  the target stays, no copy fallback, because a copy is not atomic.
- **Structured tool content: return `taskmanager.StructuredText(text, fields)` (#67, Shell).**
  It is a `str`, so direct test calls compare as before; `wrap_sync_tool` turns it into a
  `ToolResult` with the task prefix on the text. A plain `ToolResult` from a tool would lose the
  prefix. The fields get `task_id` too: Claude Code shows `structuredContent` INSTEAD of the text
  blocks when present (it already did for `{"result": ...}`, so this is no new loss of text),
  and the prefix would be gone otherwise. A reply is twice its output on the wire. `-> str` tools declare fastmcp's `{"result": ...}` output schema, and a client may
  validate against it: a tool with its own fields MUST pass `output_schema=` too.
- **A long Shell blocks others:** the task manager runs 3 Shell calls at once and a fourth waits
  60 s. One-hour calls through the gateway (#67) make that reachable.
- **Task status writes go through `TaskManager._transition`.** It checks `is_cancelled` and
  writes the status in one step under `_lock`, the same lock `cancel_task` and
  `_cleanup_old` use. Never write `task.status` directly in the wrapper.
