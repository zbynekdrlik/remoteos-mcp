---
paths:
  - "src/remoteos/__main__.py"
  - "src/remoteos/taskmanager.py"
  - "tests/test_startup_flags.py"
  - "tests/test_taskmanager.py"
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
- **Shell `cwd` goes to `Popen(cwd=…)`, never into the command text.** It is expanded with
  `expandvars(expanduser())`, and a path that is not a directory is refused before anything runs.
- **FileUpload checks the cap and decodes with `validate=True` BEFORE any mkdir/write.** Only
  whitespace is dropped (`str.translate`). Catch `ValueError`: non-ASCII input raises a plain
  one, not `binascii.Error`.
- **Task status writes go through `TaskManager._transition`.** It checks `is_cancelled` and
  writes the status in one step under `_lock`, the same lock `cancel_task` and
  `_cleanup_old` use. Never write `task.status` directly in the wrapper.
