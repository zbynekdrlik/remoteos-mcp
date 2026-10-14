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
    masking is on. Do not rely on that path.
- **Every tool is `@reports_errors`** (`remoteos.errors`), directly under `@mcp.tool(...)`.
  - Any exception becomes `ToolError("<Tool> error: <e>")`, the text the tools always used.
    So a tool body or a helper just raises (`ValueError`, `RuntimeError`, `LookupError`…).
  - A message that must NOT get the prefix (`File not found: …`, `Unknown action: …`) is
    raised as `ToolError(...)` directly.
  - `functools.wraps` keeps the schema. All 44 `tools/list` entries were diffed
    byte-identical against main when the decorator went in.
- **`DesktopUnavailable` passes through verbatim, with no tool prefix.** The Linux
  headless/Wayland stub text is a contract (cam1..4) and must stay byte-for-byte. Raise it
  as `DesktopUnavailable(_unavailable())`, never `RuntimeError`, or it gains `"Click error: "`.
- **The task wrapper keeps `[task:<id>]`.** `taskmanager.wrap_sync_tool` re-raises a
  `ToolError` as `"[task:<id>] <text>"`. An unexpected exception becomes `"[task:<id>]
  Error in <tool>: …"`. The lock-wait timeout and a cancel before or during execution are
  `ToolError`s too. The task ends FAILED or CANCELLED. CancelTask, GetTaskStatus and
  GetRunningTasks are not wrapped, so they have no prefix.
- **Logs stay payload-free (the #31 invariant also covers error replies).** An error message
  can echo an argument: a path, a window title, a process name.
  - Log the tool, the task id and `type(e).__name__`, plus `errors.stack_frames(e)` for a
    non-`ToolError`. Those are the frames and the type, never the message.
  - `tests/test_startup_flags.py::test_debug_log_never_carries_tool_arguments_or_replies`
    catches a leak end to end.
  - In a leak test, build the marker at run time. The frames show SOURCE lines, so a literal
    in the raising line is "found" in the log.
- **Providers' clipboard functions raise on failure.** `text_entry.paste_text` catches the read
  (a failed read = nothing to restore). It wraps a failed write as "could not put the text on
  the clipboard: …". A failed restore after a good paste stays a successful reply with the
  warning.
- **A non-zero exit of a command a tool runs is its answer, not a failure:** Shell and the
  `_run` / `_ps` service helpers (`[Exit code: N]`), as upstream PR 83. A closed or silent
  port is a PortCheck result, too.
- **`diagnose(capture: bool) -> str | None` on every desktop provider.**
  - The tools call it through `_desktop_preflight`, the decorator's `preflight=` hook.
  - `@_capture_tool` = Snapshot, AnnotatedSnapshot, OCR, ScreenRecord.
  - `@_input_tool` = Click, Move, Type, Scroll, Shortcut, MinimizeAll.
  - The clipboard, window, App, LockScreen and Notification tools have NO preflight. They
    work on a locked Windows session.
- **Never a false positive.** Only a positive finding is a diagnosis. A probe that raises, an
  API that is missing (an older OS) or an unreadable state = "usable". Each `diagnose()`
  wraps its probes and logs `desktop probe failed`.
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
    16 on x86 and x64 (`_WTSINFOEX_HEAD`, locked by a test).
  - Set `restype = c_void_p` for HDESK/HWINSTA, pass handles back as `c_void_p(h)`, free
    every WTS buffer, and close the desktop handle.
- **A disconnected Windows session** is reconnected by the capture tools, through
  `_ensure_session_connected` (`query session` + `tscon`), and then re-probed. That is
  Snapshot's old self-heal. Input tools report it ("run ReconnectSession") and never move the
  session on their own.
- **macOS:**
  - `AXIsProcessTrusted` is in HIServices. `pyobjc-framework-Quartz` does not wrap it (checked
    in the 12.2.2 wheel), so it goes through `ctypes.CDLL(ApplicationServices)` via
    `_load_application_services`, which the tests replace.
  - `CGPreflightScreenCaptureAccess`, `CGSessionCopyCurrentDictionary` and
    `kCGSessionOnConsoleKey` ARE in Quartz. Read them with `getattr(…, None)` (macOS 10.15+).
  - The lock key is the undocumented `CGSSessionScreenIsLocked`.
  - A capture needs Screen Recording, input needs Accessibility. Never require both, or
    Snapshot false-positives on a Mac that only lacks Accessibility.
- **Linux:** `diagnose()` = `_unavailable()` without X11, and logind `LockedHint`
  (`loginctl show-session <id> -p LockedHint --value`) for an X11 session with an id. A fake
  X world must answer that `_run_capture` call: see `_install_fake_x11` in
  `tests/test_coordinate_contract_tools.py`.
- **Live check (supervisor, after deploy):**
  - `Shell(cwd="C:\\no such dir")` on a Windows box must show as a tool error in Claude.
  - A healthy `Snapshot` must still succeed. That proves the preflight does not
    false-positive.
