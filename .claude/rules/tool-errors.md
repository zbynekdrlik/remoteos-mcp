---
paths:
  - "src/remoteos/errors.py"
  - "src/remoteos/app.py"
  - "src/remoteos/tools/**"
  - "src/remoteos/taskmanager.py"
  - "src/remoteos/text_entry.py"
  - "src/remoteos/platform/**/*.py"
  - "src/remoteos/network.py"
  - "src/remoteos/ocr.py"
  - "src/remoteos/process_mgr.py"
  - "tests/test_tool_errors.py"
  - "tests/test_desktop_diagnose.py"
  - "tests/test_helper_failures.py"
  - "tests/test_windows_tool_errors.py"
  - "tests/fake_windows_session.py"
  - "tests/tool_test_support.py"
  - "tests/conftest.py"
---

# Tool errors as MCP errors, and the desktop diagnoses (#32) — gotchas

- **A failing tool RAISES. It never returns error text.** FastMCP 4.x (4.1.2 since #39) turns a raised
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
    main: input schemas and annotations are identical. Only the Type and ReconnectSession
    descriptions changed, both on purpose.
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
    `errors.RedactInvalidArguments` keeps only loc/type/msg. `app` installs it on
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
- **Starting a child and decoding its output** (Shell, `run_command`, OCR, Ping: the UTF-8
  console trampoline, `run_child` and its timeout tree kill, `decode_output`, the own console,
  the Windows system language) is in
  `.claude/rules/child-output.md` (#48).
- **The stale-task sweep** (`_cleanup_old`, `STALE_AFTER_S` = 300) cancels a RUNNING task, and
  a cancelled task's reply is an error. A call that declares its runtime through a numeric
  argument in `taskmanager._RUNTIME_ARGS` (Shell's `timeout`, Wait's `seconds`) extends its
  own limit to that runtime + 60 s, so a long command or wait that finishes inside it is never
  reported as cancelled. A new tool that can legitimately run past 300 s must declare its
  runtime the same way (round 8: `Wait(seconds=400)` was swept).
- **Providers' clipboard functions raise on failure.** `text_entry.paste_text` catches the read
  (a failed read = nothing to restore). It wraps a failed write as "could not put the text on
  the clipboard: …". A failed restore after a good paste stays a successful reply with the
  warning.
- **`diagnose(need: str) -> str | None`** lives in `platform/<os>/availability.py`. Each
  desktop provider re-exports it, so `get_desktop().diagnose` works.
  - `need` is one of three values:
    - `"capture"`: read the screen;
    - `"input"`: send events;
    - `"windows"`: only the window list. That means the user's desktop, locked or not:
      - Windows: refused only in session 0 and a non-interactive window station;
      - macOS: refused only for "no GUI session" and a session that is not on the screen (fast
        user switching lists nothing on screen). Without Screen Recording, macOS hides other
        apps' window titles, so the list shows their app names;
      - Linux: the stub.
  - The tools call it through `_desktop_preflight(need)`, the decorator's `preflight=` hook.
  - `@_capture_tool` = AnnotatedSnapshot, Zoom, OCR, ScreenRecord.
  - `@_display_tool` = DisplayInventory, CursorPosition (`"windows"`: both are readable on a locked desktop), ElementTree.
  - WaitFor calls it itself: `"capture"` for text, `"windows"` for a window.
  - Snapshot calls it itself: `"capture"` for the screenshot (`use_vision`), else `"windows"`.
    The window list works on a disconnected or locked session (live on win-pz-server), but
    outside the user's desktop it would be a hollow empty success.
  - `@_input_tool` = Click, ClickElement, Move, Type, TypeElement, Scroll, Shortcut, HoldKey, MinimizeAll.
  - The clipboard, notification, LockScreen, FocusWindow and App tools have NO preflight. The
    clipboard and notifications work on a locked session. FocusWindow and App switch/resize
    fail there with their own `SetForegroundWindow` error.
- **A new need** (e.g. #37's AX-tree tools, which need `AXIsProcessTrusted`) is a new `need`
  value, handled in each `availability.py`. An unknown need passes every session check that
  applies to all needs.
- **Never a false positive.** Only a positive finding is a diagnosis. A probe that raises, an
  API that is missing (an older OS) or an unreadable state = "usable". Each `diagnose()`
  wraps its probes, AND `_desktop_preflight` catches a raising `diagnose()`. Both log
  `desktop probe failed`.
- **The preflight never changes the session.** A disconnected Windows session is reported
  ("run ReconnectSession"). Only two callers run `reconnect_to_console`, because a tscon puts
  the user's desktop on the machine's physical screen:
  - the explicit ReconnectSession tool;
  - the capture retry in `_capture_with_retry` (Snapshot, AnnotatedSnapshot and Zoom), which
    tscons only when the session went disconnected between the preflight and the grab.
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
  - `conftest.win_session` imports the Windows provider against it. It is used by
    `test_desktop_diagnose.py` and `test_windows_tool_errors.py`.
  - The `test_provider_geometry` win fixture also uses the fake session HEALTHY, so its tool
    tests run the preflight's real healthy path.
  - New Windows error tests go into `test_windows_tool_errors.py`, not into
    `test_provider_geometry.py`. That module is about geometry and sits just under the
    ~1000-line budget.
  - The helper and tool-error tests fake the stdlib `subprocess.run` / `Popen`, never an
    internal `_run`. `tests/test_helper_failures.py` has one test per raise path; check a
    new one against a mutant that returns the message instead of raising.
  - `tests/tool_surface.py` gives `server`: the tools and helpers by name across `__main__`, `app` and
    `tools/*`. `monkeypatch.setattr(server, "is_windows", …)` patches every real module that binds the
    name, so a fake cannot miss one (#49).
  - `tests/tool_test_support.py` holds `tool_error` (the text of a tool's ToolError) and
    `purge_module` (re-import a provider against the current fakes) for every OS.
  - A macOS input test records the PROVIDER's `pyautogui` too (`_GuiRecorder` in
    `test_desktop_diagnose.py`): `type_text` clicks, clears and pastes through it, so a
    "nothing was clicked" check on `server.pyautogui` alone can never fail.
- **Live check (supervisor, after deploy):**
  - `Shell(cwd="C:\\no such dir")` on a Windows box must show as a tool error in Claude.
  - A healthy `Snapshot` must still succeed. That proves the preflight does not
    false-positive.
