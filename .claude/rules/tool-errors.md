---
paths:
  - "src/remoteos/errors.py"
  - "src/remoteos/__main__.py"
  - "src/remoteos/taskmanager.py"
  - "src/remoteos/text_entry.py"
  - "src/remoteos/platform/**/*.py"
  - "src/remoteos/network.py"
  - "src/remoteos/ocr.py"
  - "src/remoteos/process_mgr.py"
  - "tests/test_tool_errors.py"
  - "tests/test_desktop_diagnose.py"
  - "tests/test_helper_failures.py"
  - "tests/fake_windows_session.py"
---

# Tool errors as MCP errors, and the desktop diagnoses (#32) — gotchas

- **A failing tool RAISES. It never returns error text.** FastMCP 4.0.3 turns a raised
  `fastmcp.exceptions.ToolError` into `CallToolResult(isError=True)` whose only text block is
  `str(e)`.
  - `server.py` `call_tool` re-raises a `FastMCPError` BEFORE the generic `except Exception`.
    Only that generic branch applies `mask_error_details`, so a `ToolError` message is
    **never masked**.
  - `mcp_operations.py` `_on_call_tool` then returns the error result.
  - A plain exception would be wrapped as `"Error calling tool 'X': …"`, or masked when
    masking is on. FastMCP logs that path WITH the message, so never let a tool reach it.
- **Every tool is `@reports_errors`** (`remoteos.errors`), directly under `@mcp.tool(...)`.
  `test_every_registered_tool_reports_its_failures_as_mcp_errors` checks the
  `__remoteos_reports_errors__` marker on every registered tool.
  - Any exception becomes `ToolError("<Tool> error: <e>")`, the text the tools always used.
    So a tool body or a helper just raises (`ValueError`, `RuntimeError`, `LookupError`…).
  - A message that must NOT get the prefix (`File not found: …`, `Unknown action: …`) is
    `raise failure("…")`. That sets the log level to WARNING; a bare `ToolError` logs at
    ERROR.
  - `functools.wraps` keeps the schema. All 44 `tools/list` entries were diffed against
    main; only the Type description changed, and on purpose.
- **`DesktopUnavailable` passes through verbatim, with no tool prefix.** The Linux
  headless/Wayland stub text is a contract (cam1..4) and must stay byte-for-byte. Raise it
  as `DesktopUnavailable(_unavailable())`, never `RuntimeError`, or it gains `"Click error: "`.
  - The stub itself is unchanged. The tool-level text of Snapshot, ScreenRecord and OCR
    without X11 did change: it used to be `<Tool> error: <stub>` (OCR: an "OCR failed.
    Errors: …" chain), and it is the bare stub now, as AnnotatedSnapshot always was. These
    tools are excluded on the headless boxes at startup (`tiers.LINUX_DISPLAY_TOOLS`).
- **The task wrapper keeps `[task:<id>]`.** `taskmanager.wrap_sync_tool` re-raises a
  `ToolError` as `"[task:<id>] <text>"` at the same log level. An unexpected exception becomes
  `"[task:<id>] Error in <tool>: …"`. The lock-wait timeout and a cancel before or during
  execution are `ToolError`s too. The task ends FAILED or CANCELLED. CancelTask,
  GetTaskStatus and GetRunningTasks are not wrapped, so they have no prefix.
- **Logs stay payload-free (the #31 invariant also covers error replies).** An error message
  can echo an argument: a path, a window title, a process name.
  - Log the tool, the task id and `type(e).__name__`, plus `errors.stack_frames(e)` for a
    non-`ToolError`. Those are the frames and the type, never the message.
  - Helpers log counts and pids, never names (`process_mgr.kill_process`). Shell logs the
    cwd problem, never the cwd; FileUpload logs sizes, never the path.
  - FastMCP itself logs a bad call's pydantic errors WITH their `input` values ("Invalid
    arguments for tool …", WARNING, on its own non-propagating `fastmcp` handler).
    `errors.RedactInvalidArguments` keeps only loc/type/msg. `__main__` installs it on
    `fastmcp.server.server`. A test that checks FastMCP's logs must add caplog's handler to
    the `fastmcp` logger.
  - `tests/test_startup_flags.py::test_debug_log_never_carries_tool_arguments_or_replies`
    catches a leak end to end.
  - In a leak test, build the marker at run time. The frames show SOURCE lines, so a literal
    in the raising line is "found" in the log.
- **Only a real failure raises.** Each of these is a result, not an error:
  - an empty listing ("No files matching", "No connections found");
  - a closed or silent port;
  - a clipboard without text, which is `""` on every OS. Windows checks
    `IsClipboardFormatAvailable`, and Linux maps xclip's "target … not available";
  - a non-zero exit of a command whose exit code IS the answer: Shell, listings, EventLog
    (`[Exit code: N]`).
- **A command that CHANGES state and exits non-zero failed:**
  `platform.command.run_command(…, check=True)`, the one runner of the service, task and
  `defaults` helpers. It raises `"<label> failed (exit N): <stderr>"`.
  - Uses: service start/stop, task create/delete/enable, daemon-reload, launchctl load,
    `defaults` read/write.
  - Exceptions:
    - launchctl unload, and the Linux timer disable before the unit files are removed, stay
      answers;
    - a Linux TaskDelete with no unit files is an error.
  - The Windows provider's PowerShell calls (launch_app, show_notification) and `tscon` use it
    with `check=True` too.
  - It decodes with `errors="replace"`: a localized console writes the OEM code page
    (cp852), and strict decoding turned the real failure into a `UnicodeDecodeError`.
  - Windows OCR runs with `$ErrorActionPreference = 'Stop'`. A failing step can still exit 0
    with empty stdout, so "stderr and no text" is a failure as well.
- **The stale-task sweep** (`_cleanup_old`, `STALE_AFTER_S` = 300) cancels a RUNNING task, and
  a cancelled task's reply is an error. A call with a numeric `timeout` argument (Shell)
  extends its own limit to `timeout + 60` s, so a long command that finishes inside its
  timeout is never reported as cancelled.
- **Providers' clipboard functions raise on failure.** `text_entry.paste_text` catches the read
  (a failed read = nothing to restore). It wraps a failed write as "could not put the text on
  the clipboard: …". A failed restore after a good paste stays a successful reply with the
  warning.
- **`diagnose(capture: bool) -> str | None`** lives in `platform/<os>/availability.py`. Each
  desktop provider re-exports it, so `get_desktop().diagnose` works.
  - The tools call it through `_desktop_preflight`, the decorator's `preflight=` hook.
  - `@_capture_tool` = AnnotatedSnapshot, OCR, ScreenRecord.
  - Snapshot calls `_desktop_preflight(True)` itself, only for the screenshot (`use_vision`).
    `use_vision=False` reads only geometry and windows, which work on a disconnected or locked
    session (live on win-pz-server).
  - `@_input_tool` = Click, Move, Type, Scroll, Shortcut, MinimizeAll.
  - The clipboard, notification, LockScreen, FocusWindow and App tools have NO preflight. The
    clipboard and notifications work on a locked session. FocusWindow and App switch/resize
    fail there with their own `SetForegroundWindow` error.
- **A preflight for another need** (e.g. #37's AX-tree tools, which need `AXIsProcessTrusted`)
  means turning `capture: bool` into a kind (capture / input / accessibility). Do not
  overload the bool.
- **Never a false positive.** Only a positive finding is a diagnosis. A probe that raises, an
  API that is missing (an older OS) or an unreadable state = "usable". Each `diagnose()`
  wraps its probes, AND `_desktop_preflight` catches a raising `diagnose()`. Both log
  `desktop probe failed`.
- **The preflight never changes the session.** A disconnected Windows session is reported
  ("run ReconnectSession"). Only two callers run `reconnect_to_console`, because a tscon puts
  the user's desktop on the machine's physical screen:
  - the explicit ReconnectSession tool;
  - the Snapshot capture retry in `_capture_snapshot_frame`, which tscons only when the
    session went disconnected between the preflight and the grab.
- **`reconnect_to_console`** reconnects the server's OWN session: `ProcessIdToSessionId`,
  `WTSConnectState`, `tscon <id> /dest:console`, then a re-check.
  - It returns `"reattached"` or `"attached"` (it already was: console or remote desktop) and
    raises when the state is unreadable or the session is still disconnected.
  - The replies and the retry's log/error are worded from that status, so they never claim
    a reconnect that did not happen.
  - Never parse `query session`. A disconnected session's line has a blank SESSIONNAME
    (`>   drlikzbynek   2  Disc`, live on win-pz-server), which the old parser skipped. The
    tool then answered "connected" while nothing happened.
- **Windows probe order** (all read-only ctypes):
  1. `ProcessIdToSessionId` → 0 = session 0.
  2. The `GetProcessWindowStation` name ≠ `WinSta0`.
  3. `WTSQuerySessionInformationW(WTSConnectState=8)` = `WTSDisconnected` (4).
  4. `OpenInputDesktop(0, False, DESKTOP_READOBJECTS)`: NULL or a name ≠ `Default` means the
     secure desktop is up.
  5. Only on that blocked path, `WTSSessionInfoEx` (25) `SessionFlags` == 0 (LOCK) decides
     LOCKED over the UAC/secure desktop. Windows 7/2008 R2 report the flag inverted; it only
     picks the message.
  - Never treat "the console session ≠ mine" as no session: an active RDP session works
    without the console.
  - `WTSINFOEXW`'s union holds LARGE_INTEGERs, so it is 8-aligned. SessionFlags is at offset
    16 on x86 and x64. This was confirmed live on real hardware and is locked by a test.
  - Set `restype = c_void_p` for HDESK/HWINSTA, pass handles back as `c_void_p(h)`, free
    every WTS buffer, and close the desktop handle.
  - Live (win-resolume, win-stream-snv): session 1, WinSta0, state 0, `Default`, flags 1 →
    usable.
- **macOS:**
  - Input = `CGPreflightPostEventAccess`, the predicate the event-posting code
    (`_require_post_event_access`) gates on. Do NOT use `AXIsProcessTrusted`: it is a
    different TCC service, and it would refuse a process whose events are delivered.
  - Capture = `CGPreflightScreenCaptureAccess`.
  - Session = `CGSessionCopyCurrentDictionary` (None = no GUI session,
    `kCGSessionOnConsoleKey`, and the undocumented `CGSSessionScreenIsLocked`).
  - All of these are in pyobjc Quartz. Read them with `getattr(…, None)` (macOS 10.15+), and
    import Quartz at call time.
- **Linux:** `diagnose()` = `_unavailable()` without X11. Nothing else, and no subprocess.
  - A locked X11 screen is NOT refused. The screenshot truthfully shows the locker, and
    xdotool input reaches it, which is the only remote way to unlock.
- **Tests:**
  - `tests/fake_windows_session.py` is the shared fake logon session.
  - The `test_provider_geometry` win fixture uses it HEALTHY, so its tool tests run the
    preflight's real healthy path.
  - The helper tests fake the stdlib `subprocess.run`.
- **Live check (supervisor, after deploy):**
  - `Shell(cwd="C:\\no such dir")` on a Windows box must show as a tool error in Claude.
  - A healthy `Snapshot` must still succeed. That proves the preflight does not
    false-positive.
