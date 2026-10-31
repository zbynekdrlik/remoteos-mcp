---
paths:
  - "src/remoteos/platform/command.py"
  - "src/remoteos/tools/shell.py"
  - "src/remoteos/ocr.py"
  - "src/remoteos/network.py"
  - "src/remoteos/platform/win/services.py"
  - "src/remoteos/platform/win/desktop.py"
  - "src/remoteos/platform/win/availability.py"
  - "tests/fake_console_child.py"
  - "tests/test_output_encoding.py"
  - "tests/test_child_timeout.py"
  - "tests/test_shell_tool.py"
  - "tests/test_helper_failures.py"
  - "tests/test_win_system_language.py"
  - "tests/conftest.py"
  - "tests_windows/*.py"
---

# Starting a child and decoding its output (#32, #48) — gotchas

- **A Windows console child whose output is a tool reply is started and decoded in
  `platform/command.py` (#32, #48).** (Other children, e.g. taskkill, install/uninstall,
  the Linux/macOS OCR and desktop helpers, have their own runs.)
  - **Every PowerShell child = `powershell_argv(script)`, the console trampoline (#48 round 2):**
    `[<python.exe>, "-I", "-S", "-X", "utf8", "-c", UTF8_CONSOLE_TRAMPOLINE, cwd or "",
    "powershell", "-NoProfile", "-Command", script]`. Users: Shell, services/tasks/EventLog `_ps`,
    `launch_app`, `show_notification`, Windows OCR.
    - **Why:** PowerShell 5.1 takes `[Console]::OutputEncoding` from its console's output page
      AT START-UP. That page is the OEM page: cp437 (resolume-pp, en_150) best-fitted `Žltý kôň`
      to `Zlty kôn`, and cp852 has no `€` or `–`. Started in a console that is ALREADY 65001,
      it writes UTF-8 with no BOM and no code of ours (live on resolume-pp: `'Ž€'` →
      `C5 BD E2 82 AC 0D 0A`).
    - **The program** (fixed text, never any user data) runs in the child's OWN hidden console:
      - `SetConsoleOutputCP(65001)` and `SetConsoleCP(65001)` on that console;
      - `subprocess.call(argv[2:], cwd=argv[1] or None, stdout=1, stderr=2)`: the same
        console (no creationflags), its own stdout/stderr handed down explicitly;
      - `sys.exit` with PowerShell's exact exit code. Windows codes are 32-bit unsigned and
        `sys.exit` takes a C long, so a code >= 2**31 goes over as the same bits, signed
        (`exit -1` → 4294967295, as before). Both sides of 2**31 are pinned.
      - A refused page (never seen) writes one line naming it on stderr (`remoteos: the console
        output|input stays in the OEM code page …`) and still runs the command; refused output
        takes the OEM fallback. PowerShell that cannot be started (no powershell.exe, a cwd that
        vanished after Shell checked it) is one `remoteos: could not start …` line
        (`_COULD_NOT_START`) and exit 1, never a Python traceback. `run_child` raises that
        line as a `RuntimeError` (review round 3): PowerShell never ran, so it is a failure, an
        MCP error for Shell, never `[Exit code: 1]`, which PowerShell's own failures use. Only a
        trampolined argv with exit 1 qualifies; page-refusal lines before it are skipped (round 4).
      - `-X utf8` (review round 2): the trampoline's own messages reach the pipe as UTF-8. A
        pipe would otherwise get the ANSI page, and a localized OSError text would be misread
        by the OEM fallback.
    - **The trampoline costs about 700 characters plus the cwd** of Windows' 32 767-character
      command line (python.exe's path, the flags, the program). A script that fit the limit
      before #48 by less than that now fails loudly (WinError 206).
    - **The cwd goes INTO the argv, never to `run_child` (review round 1).** `CreateProcess`
      looks for `powershell` in the parent's exe directory, then the PARENT's current
      directory. The trampoline runs in the server's directory, as the server itself did when
      it was PowerShell's parent, so a `powershell.exe` planted in the user's Shell `cwd` is
      never found. PowerShell starts in `cwd` (pinned on real Windows with spaces, diacritics
      and a trailing backslash).
    - **The script reaches PowerShell unchanged**, the argv byte-identical to dev28's:
      `Popen` quotes it with `list2cmdline`, `python.exe` parses it back, and the trampoline's
      `subprocess.call` quotes it the same way. So error records, `$?` / `exit` /
      `$LASTEXITCODE`, `param()` / `using`, the user's line numbers, the global scope and
      Constrained Language Mode are all plain PowerShell. Nothing of ours runs inside
      PowerShell.
    - **History.** Round 1 put a UTF-8 prefix (PowerShell code) in front of the script, and
      5.1 printed it in every `Write-Error` record's header (live). A `[ScriptBlock]::Create`
      bootstrap moved it to the record's position line and lost the `$?` exit code (pwsh probes
      on #48). That is why there is no PowerShell code at all. Never add any back.
    - **`python.exe` next to `sys.executable`, never `pythonw.exe`.** The server may itself run
      as `pythonw.exe`, but a GUI-subsystem program gets no console from `CREATE_NO_WINDOW`, so
      it would have none to switch. A missing `python.exe` raises; there is no silent fallback.
      `-I -S`: isolated and no `site` import, so it starts in tens of ms.
    - **Accepted cost: `$OutputEncoding` is back to Windows PowerShell's default, ASCII.** That
      is what PowerShell pipes INTO a native program's stdin, so `"čaká" | findstr "."` sends
      `?ak?`, as before #48. (Round 1's prefix had set it to UTF-8.) Native OUTPUT is exact.
    - Native programs started from PowerShell inherit the 65001 console, so one that writes
      through the console's output page writes UTF-8 (`cmd /c echo Žltý kôň`, pinned in
      `tests_windows`).
    - **A native program that writes the OEM page regardless:**
      - written straight into our pipe, it is read line by line by the OEM fallback;
      - CAPTURED by PowerShell (a pipeline, an assignment, `| Out-String`, `2>&1`), PowerShell
        decodes it with `[Console]::OutputEncoding` = UTF-8, and its non-ASCII text becomes
        U+FFFD before `decode_output` sees it. Nothing is logged;
      - a program that writes the ANSI page (e.g. a Python child) is not exact either way.
      - Which fleet tools do this is verified live only. The live list is on #48.
    - The 65001 page also governs grandchildren: cmd.exe reads a `.bat` file in the console
      page, so a cp852 batch file with a diacritic is read as UTF-8. Verified live only.
    - Never hand-build a PowerShell argv. `test_every_powershell_child_is_started_by_the_one_builder`
      AST-scans for an argv list/tuple starting with powershell/pwsh(.exe), or a
      `"powershell -…"` string, outside `command.py`.
  - **Every child runs in its OWN hidden console** (`own_console()` →
    `creationflags=CREATE_NO_WINDOW`, 0x08000000). This is why switching the child's console
    to UTF-8 is safe. When every child shared the server's console (the hidden cmd console of
    `start-remoteos.bat`), one `chcp 65001` or `[Console]::OutputEncoding = …UTF8` in a Shell
    call garbled every later reply for every client (live, #32 round 5: `A─Ź┼í┼żB`). Never
    switch the encoding in a child that shares the server's console.
  - **`run_child(cmd, timeout, windows, cwd, env)`** is the one runner of Shell, `run_command` and
    Windows OCR: `Popen` in binary mode, its own console (Windows) or session (POSIX), then
    `decode_output` and `ChildOutput.reply()` (stdout, `[STDERR] …`, `[Exit code: N]`).
    - `env` is the child's WHOLE environment, `None` = the server's. The Linux Shell passes
      `shell_env()`, the server user's session variables (#52, `.claude/rules/server-startup.md`);
      the Windows toast passes `toast_environment(title, message)` through `run_command(env=)`
      (#39). Free text never goes INTO a PowerShell script: a caller value in the script is
      `platform/win/psquote.ps_literal(value)` (a single-quoted literal, the typographic quotes
      doubled too), and multi-line text travels in an environment variable read as `$env:NAME`.
    - **A timeout kills the whole tree** (`kill_process_tree`) and re-raises
      `subprocess.TimeoutExpired`. It names the PowerShell argv, never the trampoline: the
      client's error text is what it was before round 2. Any other exception while waiting kills
      the tree too, as `subprocess.run` did.
      - Windows: `taskkill /F /T`, in its own hidden console, so no window flashes up under
        `pythonw.exe`. Its limit: `/T` follows parent pids, so a process whose parent already
        exited (PowerShell returned, something it started still holds the pipe) is not killed;
        the bounded reap still returns the call. dev28 had the same gap (review round 5).
      - POSIX: the group whose id is the child's pid. The caller's group id is never that pid,
        so the signals need no check of the child's own group first.
        - The group's members are snapshotted (`_group`: `os.getpgid` over `psutil.pids()`;
          a process that left the group is neither signalled nor waited for), the group gets
          SIGTERM, the members get up to `KILL_GRACE_S` (2 s) to exit, then the group gets
          SIGKILL IN ANY CASE (review round 2), from a `finally`. Whatever ignored SIGTERM dies,
          whether or not it holds our pipes. A failing scan or wait only cuts the grace short
          (review rounds 4 and 5).
        - Each member is a FRESH `psutil.Process`, never `process_iter()`'s cached one: a cached
          object for a pid reused by a member since has an old start time, and `is_running()`
          would cut that member's grace (review round 4).
        - Members, not just descendants (review round 3): in the usual timeout the shell has
          exited and a process it left behind (`x &`, `nohup x &`) holds the pipe. Reparented to
          init, it is no longer a descendant, but it is in the group and needs its grace.
        - "Exited" = gone, a zombie, or a reused pid (`is_running()` compares the start time;
          `status()` alone does not).
        - Nothing is reaped before that SIGKILL: the child, by then a zombie, keeps its group id
          from being reused. Waiting uses psutil status, because CPython has no `os.waitid` on
          macOS and `Popen.poll()` would reap.
      - Kill-path log lines carry exception TYPES only. `TimeoutExpired`'s text is the whole
        argv, the user's command included (#31).
    - **Every child's stdin is NUL** (`stdin=DEVNULL`, review round 1). Under
      `--transport stdio` the server's stdin is the MCP JSON-RPC stream, and a child that read it
      would eat protocol frames.
    - Why: under the trampoline the server's child is not the process doing the work. Killing
      only it, as `subprocess.run` does, leaves PowerShell holding our pipes, and on Windows
      `run()` then waits in `communicate()` with no timeout (CPython gh-81605).
    - Fakes of a child answer at `subprocess.Popen` (`popen_answered_by`). The real
      `subprocess.run` goes through `Popen` too.
  - **`decode_output(raw, windows, stream)`**:
    - Windows: a leading UTF-8 BOM is dropped (never seen live; defensive). Then the stream is
      tried as strict UTF-8. If it is not UTF-8, each LINE (ended by `\r\n`, `\n` or a lone
      `\r`, review round 2) is read on its own: UTF-8, or else the OEM page (`"oem"`,
      `errors="replace"`).
      - It goes line by line (review round 1): a per-stream fallback turned PowerShell's valid
        UTF-8 lines into mojibake as soon as one native line in the same pipe was not UTF-8.
      - 0x0A and 0x0D are never inside a UTF-8 or double-byte OEM character (DBCS trail
        bytes are >= 0x40), so the split is safe. Two encodings on the SAME line are read
        together in the OEM page.
      - Non-UTF-8 lines come from a native program started directly (`tscon` via
        `run_command`), a native program started from PowerShell that writes its own page, or
        a console that refused the switch. PowerShell's own parse error is UTF-8 now: the
        console is 65001 before PowerShell starts.
      - One log line per stream (`"<stream>: N line(s) not UTF-8 …; M undecodable …"`) gives
        the fallback lines and the replaced characters, never the content: INFO when nothing
        was lost, WARNING when something was. The tests lock the stream label for Shell and
        `run_child`.
      - Known limit: a short OEM line that happens to be valid UTF-8 (cp852 `Ďá` = D2 A0) is
        read as UTF-8.
      - PowerShell's error stream is UTF-8 as well (live on resolume-pp, cp437: `Write-Error
        "chyba ťava"` came back with an exact `ť`).
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
    console call. `tscon` gets no trampoline either: it is read through `run_command`.
  - Windows OCR's script starts with `$ErrorActionPreference = 'Stop'`. A failing step can
    still exit 0 with empty stdout, and since #36 it always answers one JSON object, so an empty
    stdout is a failure. It waits through `AsTask`, the #36 replacement of a compiled C# helper.
  - **Tests, Linux** (`tests/test_output_encoding.py`, `tests/test_child_timeout.py`) fake only
    the OS boundary, with one exception: the stale-identity test seeds psutil's private
    `process_iter` cache (`psutil._pmap`, `Process._ident`) and asserts the seed really differs,
    so a psutil change fails it loudly instead of silently:
    - `fake_console_child.FakeChild` (the `windows_child` fixture) replaces `subprocess.Popen`
      with a REAL Python child that writes the given bytes, so subprocess's own text and binary
      handling runs. It starts the interpreter captured at import, because `windows_python`
      points `sys.executable` at a fake install (`python.exe` + `pythonw.exe` in a temp folder);
    - `assert_runs_powershell(argv, python, script)` checks the trampoline argv. The program is
      exec'd against a fake kernel32 and a fake `subprocess.call` (its own boundary);
    - `oem_code_page(page)` registers Python's Windows-only `oem` codec as cp437 / cp852 /
      cp932. Set `child.oem` before the first decode: the codec registry caches a lookup, and
      `codecs.unregister` clears it;
    - `popen_answered_by(run)` turns a run-style fake into `Popen`; `run_result(kwargs, …)`
      gives bytes for a binary call and text for a text-mode one, as the real one does.
  - **Tests, real Windows** (`tests_windows/`, the `windows-powershell` CI job on
    windows-latest: PowerShell 5.1, en-US, OEM cp437, no fakes):
    - exact Slovak output, `cmd /c echo` exact, `Write-Error` with only the user's text and
      exit 1, `exit 3` / `exit -1`, a failing native program, `param()`, a parse error with
      exact text, and a timeout after which the PowerShell process itself (its `$PID`, written
      first) is gone;
    - the first test asserts OEM 437 and PowerShell 5, so an image change cannot turn the
      checks vacuous;
    - the Linux job never collects the directory (`testpaths = tests`), and its conftest
      refuses to run off Windows. No skips anywhere.
    - The job runs in `shell: cmd`. Under GitHub's default pwsh 7, `PSModulePath` starts with
      pwsh's own module folders, and a powershell.exe 5.1 started below python inherits them
      and fails to load `Microsoft.PowerShell.Utility`. The fleet starts from cmd too.
  - **Probing PowerShell semantics without a Windows box** (what decided round 2): the official
    pwsh linux-x64 tarball in the scratchpad (check its sha256 against the release's
    `hashes.sha256`), driven from a small Python harness that sets the child-only env, with
    `$ErrorView = "NormalView"` to get 5.1's error records. The engine shows the same
    `$MyInvocation` / `Write-Error` / `$?` behaviour. Gotchas:
    - `-EncodedCommand` with a redirected stderr writes the error stream as CLIXML
      (`#< CLIXML`): never start a PowerShell child that way;
    - `Write-Error` takes its caller's `$MyInvocation`, so ANY in-process wrapper around the
      user's script (a prefix, `& $sb`, `. $sb`, `Invoke-Expression`) puts our code in the
      record's header or position. Only a console that is UTF-8 before PowerShell starts
      avoids that.
- **The Snapshot "System Language"** comes from kernel32 `GetUserDefaultLocaleName` (an
  85-wchar buffer), then `locale.getlocale()[0]`, then `en_US`. A successful call with an
  empty name also falls back. The `locale` module's default-locale getter is removed in
  Python 3.15 (#48). `test_no_getdefaultlocale_call_is_left` greps for its name, so do not name
  it in a source file, not even in a comment.
