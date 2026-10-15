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
  - "tests/test_windows_tool_errors.py"
  - "tests/fake_windows_session.py"
  - "tests/tool_test_support.py"
  - "tests/conftest.py"
  - "tests/fake_console_child.py"
  - "tests/test_output_encoding.py"
  - "tests/test_shell_tool.py"
  - "tests/test_win_system_language.py"
---

# Tool errors as MCP errors, the desktop diagnoses (#32), and child output encoding (#48) — gotchas

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
- **A child process is started and decoded in `platform/command.py`, nowhere else (#32, #48).**
  - **Every PowerShell child = `powershell_argv(script)`**: `powershell -NoProfile -Command`
    with `UTF8_PREFIX` on its own first line (`$OutputEncoding` and `[Console]::OutputEncoding`
    = UTF-8 without a BOM, via `New-Object` so it runs on every Windows PowerShell version),
    then the script verbatim. Users: Shell, services/tasks/EventLog `_ps`, `launch_app`,
    `show_notification`, Windows OCR.
    - Never hand-build a `["powershell", …]` argv:
      `test_every_powershell_child_is_started_by_the_one_builder` fails on one.
    - PowerShell writes its output through `[Console]::OutputEncoding`, which is the console's
      OEM page. cp437 (resolume-pp, en_150) best-fitted `Žltý kôň` to `Zlty kôn`, and cp852
      has no `€` or `–`. Setting it also switches the child's console (SetConsoleOutputCP),
      so a native program started FROM that PowerShell (`cmd /c echo`, `schtasks`) writes
      UTF-8 too.
    - The limit: a script that must begin with `param(...)` or a `using` statement cannot
      follow the prefix, because PowerShell requires those first. The Shell docstring says so.
      PowerShell error positions are one line lower (`At line:2`).
  - **Every child runs in its OWN hidden console** (`own_console()` →
    `creationflags=CREATE_NO_WINDOW`, 0x08000000). This is why switching the child's console
    to UTF-8 is safe. When every child shared the server's console (the hidden cmd console of
    `start-remoteos.bat`), one `chcp 65001` or `[Console]::OutputEncoding = …UTF8` in a Shell
    call garbled every later reply for every client (live, #32 round 5: `A─Ź┼í┼żB`). Never
    switch the encoding in a child that shares the server's console.
  - **Binary mode + `decode_output(raw, windows, stream)`**, used by Shell's `Popen` and by
    `run_child`, which `run_command` and Windows OCR share:
    - Windows: strict UTF-8 first. Bytes that are not UTF-8 are read in the OEM page
      (`"oem"`, `errors="replace"`), for two cases:
      - output written before the prefix ran: PowerShell's own PARSE error, printed in OEM
        before any statement executes;
      - a native program started directly: `tscon` via `run_command`.
      The fallback is decided per stream and logged with the stream and the replaced count,
      never the content: INFO when nothing was lost, WARNING when something was.
    - Elsewhere: `locale.getpreferredencoding(False)` with `errors="replace"`, which is what
      a text-mode run used. macOS/Linux output is unchanged.
    - Newlines: `\r\n` and a lone `\r` become `\n`, as a text-mode run gave them. Binary
      mode drops subprocess's own translation, and PowerShell writes `\r\n`.
    - `errors="replace"` everywhere: an undecodable byte never loses the output. In text mode
      on Windows, a decode error in subprocess's reader thread left stdout `None` (#32 round 4).
  - **`console_child()`** = text mode, `encoding="oem"`, in its own console. It is only for a
    NATIVE program started directly whose text fits the OEM page: Ping. Python's default for
    a pipe, the ANSI page (cp1250), turned `AčšžB` into `Aźç§B` (#32 round 4). Read
    `result.stdout or ""` after a text-mode run. Never pass a bare `text=True` to a new
    console call.
  - Windows OCR runs with `$ErrorActionPreference = 'Stop'` as the first line after the
    prefix. A failing step can still exit 0 with empty stdout, so "stderr and no text" is a
    failure as well.
  - **Tests** (`tests/test_output_encoding.py`) run on Linux and fake only the OS boundary:
    - `fake_console_child.FakeChild` (the `windows_child` fixture) replaces `subprocess.Popen`
      with a REAL Python child that writes the given bytes, so subprocess's own text and
      binary handling runs;
    - `oem_code_page(page)` registers Python's Windows-only `oem` codec as cp437 / cp852 /
      cp932. Set `child.oem` before the first decode: the codec registry caches a lookup, and
      `codecs.unregister` clears it;
    - a faked `subprocess.run` returns `run_result(kwargs, …)`: bytes for a binary call and
      text for a text-mode one, as the real one does;
    - the prefix literal in tests is `fake_console_child.POWERSHELL_UTF8_PREFIX`, never
      imported from the code.
- **The Snapshot "System Language"** comes from kernel32 `GetUserDefaultLocaleName` (an
  85-wchar buffer), then `locale.getlocale()[0]`, then `en_US`. The `locale` module's
  default-locale getter is removed in Python 3.15 (#48). `test_no_getdefaultlocale_call_is_left`
  greps for its name, so do not name it in a source file, not even in a comment.
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
  - `@_capture_tool` = AnnotatedSnapshot, OCR, ScreenRecord.
  - Snapshot calls it itself: `"capture"` for the screenshot (`use_vision`), else `"windows"`.
    The window list works on a disconnected or locked session (live on win-pz-server), but
    outside the user's desktop it would be a hollow empty success.
  - `@_input_tool` = Click, Move, Type, Scroll, Shortcut, MinimizeAll.
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
  - the capture retry in `_capture_snapshot_frame` (Snapshot AND AnnotatedSnapshot), which
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
  - `tests/tool_test_support.py` holds `tool_error` (the text of a tool's ToolError) and
    `purge_module` (re-import a provider against the current fakes) for every OS.
  - A macOS input test records the PROVIDER's `pyautogui` too (`_GuiRecorder` in
    `test_desktop_diagnose.py`): `type_text` clicks, clears and pastes through it, so a
    "nothing was clicked" check on `server.pyautogui` alone can never fail.
- **Live check (supervisor, after deploy):**
  - `Shell(cwd="C:\\no such dir")` on a Windows box must show as a tool error in Claude.
  - A healthy `Snapshot` must still succeed. That proves the preflight does not
    false-positive.
