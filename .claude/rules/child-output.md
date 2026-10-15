---
paths:
  - "src/remoteos/platform/command.py"
  - "src/remoteos/__main__.py"
  - "src/remoteos/ocr.py"
  - "src/remoteos/network.py"
  - "src/remoteos/platform/win/services.py"
  - "src/remoteos/platform/win/desktop.py"
  - "src/remoteos/platform/win/availability.py"
  - "tests/fake_console_child.py"
  - "tests/test_output_encoding.py"
  - "tests/test_shell_tool.py"
  - "tests/test_helper_failures.py"
  - "tests/test_win_system_language.py"
  - "tests/conftest.py"
---

# Starting a child and decoding its output (#32, #48) — gotchas

- **A Windows console child whose output is a tool reply is started and decoded in
  `platform/command.py` (#32, #48).** (Other children, e.g. taskkill, install/uninstall,
  the Linux/macOS OCR and desktop helpers, have their own runs.)
  - **Every PowerShell child = `powershell_argv(script)`**: `powershell -NoProfile -Command`
    with `UTF8_PREFIX` on its own first line, then the script verbatim. Users: Shell,
    services/tasks/EventLog `_ps`, `launch_app`, `show_notification`, Windows OCR.
    - The prefix sets `$OutputEncoding` and `[Console]::OutputEncoding` to UTF-8 without a
      BOM. It uses `New-Object`, so it runs on every Windows PowerShell version.
    - It is wrapped in `try { … } catch { }`, so a box that refuses the switch (Constrained
      Language Mode) still runs the command with no error on every reply. That output takes
      the OEM fallback. The caught error stays in `$Error`, which only a script that reads
      `$Error` sees. It is not cleared, because `$Error.Clear()` is itself a method call that
      CLM may refuse, which would bring the stderr noise back (review round 2).
    - Never hand-build a PowerShell argv. `test_every_powershell_child_is_started_by_the_one_builder`
      AST-scans for an argv list/tuple starting with powershell/pwsh(.exe), or a
      `"powershell -…"` string, outside `command.py`.
    - PowerShell writes its output through `[Console]::OutputEncoding`, which is the console's
      OEM page. cp437 (resolume-pp, en_150) best-fitted `Žltý kôň` to `Zlty kôn`, and cp852
      has no `€` or `–`.
    - Setting it also switches the child's console (SetConsoleOutputCP). A native program that
      writes through the console's output page then writes UTF-8 too.
    - **A native program that writes the OEM page regardless** (review round 2):
      - Written straight into our pipe, it is read line by line by the OEM fallback.
      - CAPTURED by PowerShell (a pipeline, an assignment, `| Out-String`, `2>&1`), PowerShell
        itself decodes it with the switched `[Console]::OutputEncoding` = UTF-8, and its
        non-ASCII text becomes U+FFFD before `decode_output` sees it. Nothing is logged.
      - A program that writes the ANSI page (e.g. a Python child) is not exact either way.
      - Which fleet tools follow the switch is verified live only, never in the tests. The
        live list is on #48: ping/cmd through `| Out-String`, `net user`, schtasks stderr.
    - The switched page also governs grandchildren: cmd.exe reads a `.bat` file in the
      console page, so a cp852 batch file with a diacritic is now read as UTF-8, and a UTF-8
      one now reads correctly. Verified live only.
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
    - Windows: the stream is tried as strict UTF-8 first. If it is not UTF-8, each LINE
      (ended by `\r\n`, `\n` or a lone `\r`, review round 2) is read on its own: UTF-8, or
      else the OEM page (`"oem"`, `errors="replace"`).
      - It goes line by line (review round 1): a per-stream fallback turned PowerShell's valid
        UTF-8 lines into mojibake as soon as one native line in the same pipe was not UTF-8.
      - 0x0A and 0x0D are never inside a UTF-8 or double-byte OEM character (DBCS trail
        bytes are >= 0x40), so the split is safe. Two encodings on the SAME line are read
        together in the OEM page.
      - Lines come back non-UTF-8 from PowerShell's own PARSE error (printed before the prefix
        runs), from a native program started directly (`tscon` via `run_command`), and from a
        native program started from PowerShell that writes its own page.
      - One log line per stream (`"<stream>: N line(s) not UTF-8 …; M undecodable …"`) gives
        the fallback lines and the replaced characters, never the content: INFO when nothing
        was lost, WARNING when something was. The tests lock the stream label for Shell and
        `run_child`.
      - Known limit: a short OEM line that happens to be valid UTF-8 (cp852 `Ďá` = D2 A0) is
        read as UTF-8.
      - Whether PowerShell's error stream (`Write-Error`) follows the switch depends on when
        ConsoleHost made its stderr writer, so verify it live. If it does not, it is OEM:
        lossy outside that page, but never garbled.
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
