---
paths:
  - "src/remoteos/__main__.py"
  - "src/remoteos/taskmanager.py"
  - "src/remoteos/platform/linux/shell_env.py"
  - "tests/test_startup_flags.py"
  - "tests/test_taskmanager.py"
  - "tests/test_shell_env.py"
  - "tests/test_shell_tool.py"
---

# Server start, logging, Shell/FileUpload, task states (#31) — gotchas

- **FastMCP 4.0.3 `run_http_async` takes `uvicorn_config`, never `uvicorn_args`.** A wrong
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
- **Startup tests start the real server in a subprocess.** The env must be hermetic (see
  `_child_env`):
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
      default. Do not grow `desktop.py` for it (#49).
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
    - A live root-owned socket proves the display is up, not whose session it is: a display
      manager (lightdm, sddm) that reuses `:N` for ANOTHER user's login gets the euid's DISPLAY
      and its stale cookie, which X auth refuses. No access is gained (review round 3).
    - Limitation (provider-wide): the cache is filled at start-up, and the unit starts at boot. A
      graphical login after that start is seen neither by Shell's DISPLAY nor by the display-tool
      gating until the service restarts. The runtime dir and the bus do not depend on the session.
  - A failing session lookup (even an unimportable provider) costs only the display variables:
    one WARNING per exception TYPE, not one per call. One INFO line per change names the added
    variables, never a value.
  - Known exception to "never another user's session" (it predates #52): `desktop._load_atspi`
    writes the session user's `DBUS_SESSION_BUS_ADDRESS` / `DISPLAY` / `XAUTHORITY` into
    `os.environ` for a moment. A Shell call that copies `os.environ` in that window (a root
    service included) passes them on. Closing it needs a lock shared with `desktop.py`.
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
    round 3). A scratch mutation pass is a sample, not a proof: 31 chosen mutants of
    `shell_env.py` were all caught at round 3, after round 3's own 41 found gaps.
  - `conftest._linux_session_detected_at_start_up` gives EVERY test a headless session cache, as
    a started server has. With an empty cache, the first Linux Shell call ran the host's real
    `loginctl`, and a test that records `subprocess.Popen` caught those calls.
- **FileUpload checks the cap and decodes with `validate=True` BEFORE any mkdir/write.** Only
  whitespace is dropped (`str.translate`). Catch `ValueError`: non-ASCII input raises a plain
  one, not `binascii.Error`.
- **Task status writes go through `TaskManager._transition`.** It checks `is_cancelled` and
  writes the status in one step under `_lock`, the same lock `cancel_task` and
  `_cleanup_old` use. Never write `task.status` directly in the wrapper.
