---
paths:
  - "install-linux.sh"
  - "install.sh"
  - "install.ps1"
  - "uninstall-linux.sh"
  - "tests/test_installer_*.py"
  - "tests/installer_harness.py"
  - "tests/linux_installer_harness.py"
  - "tests/fake_linux_tools.py"
  - "tests/fake_pip.py"
  - "tests_windows/test_install_ps1_key.py"
---

# One-liner installers (install-linux.sh / install.sh / install.ps1) — gotchas

The live-deploy procedure lives in the `/remoteos-install` skill. This file covers the code.

## The key, on every OS (#12, since 0.7.0.dev32)

- **The key is never in a unit, a plist's ProgramArguments, a command line, an argv or the
  installer's output.** The server reads `REMOTEOS_AUTH_KEY` (`__main__.py`, the click
  `envvar`), and an env value counts as explicit, so it also beats a `remoteos.toml` `auth_key`.
  - Linux: 0600 `/etc/remoteos-mcp/config.json` + the 0600 EnvironmentFile
    `/etc/remoteos-mcp/remoteos-mcp.env` (`REMOTEOS_AUTH_KEY=<key>`).
  - macOS: the plist's `EnvironmentVariables`; the plist and `config.json` are 0600.
  - Windows: `start-remoteos.bat` does `set "REMOTEOS_AUTH_KEY=<key>"` before the server line.
    The `.bat` and `config.json` get a protected ACL: the desktop user, SYSTEM, Administrators.
- **One key precedence:** explicit `REMOTEOS_MCP_AUTH_KEY` (validated: 1-128 `[A-Za-z0-9]`,
  else refused BEFORE anything changes), else the key the RUNNING service gets (Linux: the
  unit's legacy `--auth-key`, else its EnvironmentFile; macOS: the plist's legacy
  `--auth-key`, else `EnvironmentVariables`; Windows: the start script's `set` line or legacy
  `--auth-key`), else `config.json`, (Windows: else `remoteos.toml` `[server] auth_key`, else
  the old winremote config), else a new key. A re-run never changes the key (#51): the FIRST
  key the box has decides, and one that is not 1-128 letters/digits is refused, never skipped
  for a new key (that silently cuts the clients off).
- **The server drops `REMOTEOS_AUTH_KEY` (and `REMOTEOS_OAUTH_CLIENT_SECRET`) from
  `os.environ` at the start of `cli()`**, after click read it: otherwise every Shell/App child
  inherits the key (`Shell("printenv REMOTEOS_AUTH_KEY")` printed it, review round 1).
- **Never `sudo VAR=<secret> bash`:** the secret is then in sudo's argv. Linux docs say
  `read -rs REMOTEOS_MCP_AUTH_KEY; export REMOTEOS_MCP_AUTH_KEY` + `sudo --preserve-env=REMOTEOS_MCP_AUTH_KEY bash`.
- **Never put the key into an argv on the way either.** Python reads a key file from stdin or
  by path and prints the key to a `$(…)`; files are written by `printf` / a here-string into a
  0600 temp file, then renamed; the health check sends `Authorization: Bearer` with
  `curl -H @-`; `GH_TOKEN` goes the same way. `unset` the secret env vars after reading them, so
  no child inherits them.
- Windows keys come from `RandomNumberGenerator` (rejection sampling, no modulo bias), never
  `Get-Random` (no CSPRNG; `-Count 32` drew 32 DISTINCT characters).
- PowerShell: `-cmatch '^[A-Za-z0-9]+\z'`, never `$` (it matches before a trailing newline);
  compare keys with `-cne` / `-ceq`, never the case-insensitive `-ne`.
- Do not spell out a 62-character `[A-Za-z0-9]` set: `block-sensitive-staging.sh` reads it as
  a secret. Bash: `[!A-Za-z0-9]` under `shopt -s globasciiranges` (Linux) or inside a
  `LC_ALL=C` subshell (macOS bash 3.2 has no globasciiranges). PowerShell: build the alphabet
  from code points.

## Linux: the venv layout (camera-box's, #12)

- **Layout:** `/opt/remoteos-mcp-venv` is what the unit runs. It is a SYMLINK to the current
  build `/opt/remoteos-mcp-venvs/<source-id>.<random>`. A real venv DIRECTORY there (camera-box's
  `scripts/lib/remoteos-mcp.sh`) is adopted as it is.
- **The unit is camera-box's shape** (its verify checks the exact `ExecStart=` and
  `EnvironmentFile=` lines). desktop: `User=`, `DISPLAY=<display>`,
  `XDG_RUNTIME_DIR=/run/user/<uid>`, `After=… graphical-session.target`. headless: the cams.
  - The existing unit's `Description=` is KEPT (else ours): a cosmetic difference alone
    restarted a live camera-box box on every run and flip-flopped with camera-box's lib.
  - The mode: `REMOTEOS_MCP_MODE`, else the existing unit (`Environment=DISPLAY=` or
    `After=…graphical-session.target` → desktop), else the session check (loginctl x11/wayland).
    `User=`: `REMOTEOS_MCP_USER`, else the existing unit, else `SUDO_USER`, else root. A re-run
    never changes the box's shape or account.
  - `DISPLAY`: the existing unit's, else the X11 session's (`loginctl show-session -p Display`,
    GDM's Xorg is often `:1`); a Wayland session, or a desktop unit of ours with no `DISPLAY` (the
    saved Wayland choice, on a re-run with nobody logged in), gets NO `DISPLAY`; else `:0`
    (camera-box's). No `DISPLAY` on Wayland because, for
    a non-root `User=` the server trusts its environment first, and a forced `:0` would make it
    treat Wayland as X11.
  - `REMOTEOS_MCP_POLICY=enable-only` (camera-box's cams: `remoteos_mcp_install root headless
    enable-only`): install, write, enable, never start or prove. The old builds ARE pruned (a
    venv run through the link has `sys.prefix` = the link path, so a running process already
    imports from the new build; an old build protects nothing). The old system-python copy
    stays (a running legacy process imports from it). Default `restart`.
- **A pending switch** (`/etc/remoteos-mcp/.switch-pending`): created when a switch starts,
  removed after a passed proof (or a rollback's restart). A run that finds it restarts instead
  of taking the files as unchanged: after enable-only or a run killed with no trap (SIGKILL), the
  running process is not what the files say, and `systemctl start` on an active unit loads
  nothing new. An enable-only run that changed nothing removes the mark it wrote ONLY when
  `check_running_install` finds the running install proven (round 5, narrowed in round 6): a
  cam whose venv camera-box's lib upgraded in place without a restart runs another version
  than its files, so its no-op run keeps the mark. A mark an earlier run left sets `CHANGED`.
- **PROVEN (`check_running_install`, review round 6)** = the unit existed, no pending switch,
  the service is active, `/health` reports the version the unit's own ExecStart python has
  (`python_version`, the interpreter is the ExecStart's first word, `python*` only), and an
  unauthenticated `POST /mcp` answers 401. Read-only, the key is never sent. A restart run
  reads it BEFORE the switch (what a failure may go back to); a no-op enable-only run reads it
  at the end. "A unit and no pending mark" alone is NOT proven: it restored the state that had
  just failed its proof, or files the running process never ran.
- **Order (keep it):**
  1. Validate the inputs (`REMOTEOS_MCP_REF` default `main`, empty = `main` as camera-box reads
     it; MODE / USER / POLICY), take `flock -n` on `/run/remoteos-mcp-install.lock` (a
     second run's prune could delete this run's build; NOT the world-writable `/run/lock`, where
     any local user could hold or pre-create the file and block every install). The lock is
     0600 (`umask 077`, and `chmod 600` for an older run's 0644 one): `flock(2)` works on a
     read-only fd, so a readable lock lets any account block every install (review round 5).
     Read the existing unit; resolve the key.
  2. `fetch_source`: ONE GitHub API tarball of the ref (`api.github.com/repos/…/tarball/<ref>`).
     Its top directory (`zbynekdrlik-remoteos-mcp-<sha7>`) is the source id. Code and
     `constraints.txt` come from the same tarball.
  3. Desktop mode: `install_desktop_tools` FIRST (it brings `python3-gi`, which the venv links).
  4. `venv_is_current` (the marker = the id, the installed version = the source's pyproject
     version, and the import check) → no pip at all. The marker is ours, else camera-box's; BOTH
     in one venv means camera-box's lib upgraded our build in place (it writes into whatever the
     link points at): rebuild.
  5. Else `build_venv` in the store, side by side: `python3 -m venv` (apt `python3-venv` when
     ensurepip is missing), `pip install --no-cache-dir <src>` with `PIP_CONSTRAINT=<src>/constraints.txt`
     under `env -i` + an allowlist (PATH, HOME, LANG, TMPDIR, proxies, CA/index vars), then the
     version + `import remoteos.__main__, fastmcp; from fastmcp import FastMCP`. Desktop mode
     links ONLY the system `gi` package: `<build>/system-gi/gi` → the system gi, named by a
     `.pth` (not the whole dist-packages). An unchanged desktop build of ours without gi gets it
     linked (`ensure_system_gi`, only when the system python has a gi: no gi = nothing switched)
     and a restart; camera-box's adopted directory is never written.
     **A failed download or build changes NOTHING** (it removes its build and an empty store).
  6. Save the previous unit / EnvironmentFile / config.json. ARM (every box, a fresh one too).
     Swap the link (`ln -sfn` + `mv -Tf`: one rename(2)); a real directory is moved into the
     store first. Write the files (`write_if_changed`: content + mode + owner, through a 0600
     temp file kept in `WRITE_TMP`, which `on_exit` removes: a signal never leaves a copy of a
     key file, round 7). daemon-reload,
     enable, and `is-enabled` must be literally `enabled` (camera-box's verify) or roll back;
     `restart` on a change, else `start` — but an unchanged box whose running install is not
     PROVEN (another version, e.g. camera-box's enable-only in-place upgrade; no answer; a
     stopped service; authentication off) is restarted too. `MODIFIED` marks what the run changed
     (a key file or the unit written, the venv swapped, the gi link added), each set BEFORE its
     change so a signal in between still counts it.
  7. `prove_health` (writes NOTHING): active + `/health` = the built version, an authenticated
     `initialize` 200, an unauthenticated `POST /mcp` 401. **The key goes only to the unit's own
     process** (`port_is_ours`, review round 6): `systemctl show -p MainPID --value` must be the
     ONE pid `ss -Hltnp 'sport = :8092'` lists, else the proof fails with the key unsent. A local
     account holding the port in the restart window (a loopback-only squatter gets no client
     traffic) could otherwise take the key and fake a passing proof. Checked live on dev1: the
     real server under systemd is its own listener. A restart run needs `ss` (iproute2), checked
     before anything changes. Clients still send the key to whoever holds the port; closing that
     is the transport's job (systemd socket activation), a follow-up candidate.
     Failure → `health_failed`, which
     keeps SIGPIPE/HUP/INT/TERM ignored and `set +e` until `undo_switch` is done, then exits 1.
     **`undo_switch` (also the armed EXIT trap) leaves up only what proves itself AFTER the
     undo**: PROVEN and `MODIFIED` → `restore_previous` (files, link or directory, the gi link
     taken out, daemon-reload, restart); PROVEN with nothing switched → nothing restored. Then
     `await_proven` (`check_running_install` up to HEALTH_TRIES times, read-only, no key): proven
     → "Rolled back … proved itself" / "Nothing was switched" (the pending mark removed); not
     proven → `stop_unproven` (round 7: the restored FILES may not be what the process ran, e.g.
     an EnvironmentFile changed under it to an empty key = authentication off). Not PROVEN before
     the run (a fresh box, a pending switch, files never run, auth off) → `stop_unproven`, the
     files + the pending marker kept for a re-run. An
     unproven state may be an agent with authentication off (a full shell on 0.0.0.0): it is
     never "rolled back" into running (review round 3). **enable-only never starts or stops
     anything**: its failure puts the previous files back with no restart (a fresh box: the unit is
     only disabled) and leaves the running process alone (review round 4). `restore_previous`
     puts `.switch-pending` back to its pre-run state (removed only when this run created it).
     The pending mark is written AFTER `arm`, and `undo_switch` removes a build nothing points at
     yet (a signal between `arm` and the swap).
  8. DISARM, then print the proof. Only then `remove_legacy_system_copy` (the pre-venv
     system-python `remoteos-mcp fastmcp fastmcp-slim`, `--break-system-packages`) and
     `prune_builds`.
  - While ARMED, any exit (a failed step, HUP/INT/TERM, a lost console: SIGPIPE is ignored, so
    the write fails and set -e exits) goes through the EXIT trap → `undo_switch`. Never arm
    on a list of signals alone: a no-tty `ssh … | sudo bash` drop sends no signal.
  - **The proof decides, not the console.** Before it, a lost console rolls back; during it,
    its result decides (a failure rolls back with no console); after it, the proven switch stays
    even if the next write kills the run (the legacy copy then waits for a re-run).
  - Never DISARM before a restore: `disarm` clears ARMED and puts SIGPIPE back to default, so
    the next echo to a dead console ends the run with ARMED already cleared: the EXIT trap runs
    but restores nothing (review round 1). Signals before ARM and after DISARM still go through
    `on_signal` → the EXIT trap, which removes a build nothing points at yet.
  - **Every step of the swap must be undoable**: `swap_venv` sets `SWAPPED` before the first
    rename, and `restore_venv` reads the state each step leaves (the directory moved aside, the
    `.link` temp, the new link). Tests inject `kill -HUP $$` after each step
    (`LinuxBox.inject_after`).
  - **One file, ~1000 lines (decided in review round 6).** `curl … | sudo bash` delivers ONE
    file, and camera-box installs pinned older refs, so the installer cannot source a second file
    from the ref's tarball or from `main` (raw caches `main` for minutes: version skew). At the
    budget, trim comments into this rule; if it must grow, split the SOURCE into parts that a
    generator concatenates into the committed `install-linux.sh`, with a test that they match.
  - Everything runs inside `main()`, called on the last line (a truncated download never
    half-runs).
- **Unset every name that ever holds the key before using it** (`AUTH_KEY`, `cfg_key`,
  `line`, `OLD_UNIT`, …): bash keeps the export flag of a name the caller exported, for globals
  AND locals, and the key would reach every child's environment. The shims log `export -p`
  (`box.child_environments()`).
- **The script is not sourceable** (the design said "sourced and tested like camera-box's lib"):
  `main "$@"` on the last line keeps a truncated `curl | bash` from half-running, and a source
  guard would break the piped one-liner. The tests run the whole script on the fake box instead.
- **Checks never write the venv:** `venv_python` sets `PYTHONDONTWRITEBYTECODE=1`, so an adopted
  venv stays byte-identical.
- **Never rename a venv directory in place to "swap" it**: console-script shebangs and
  `bin/activate` keep the build path. Build at the final path, swap a link.
- `uninstall-linux.sh` removes the link (or directory), the store, the unit, `/etc/remoteos-mcp`
  and a system-python copy (`--break-system-packages`).

## macOS: the system-python install block

- `install.sh` keeps the dev24 `# >>> shared install block` (the name stayed; Linux carried a
  twin until dev32). Order: pin ONE commit of main (`git ls-remote`) → record the installed
  version → `pip install --dry-run --report` preflight → plan + snapshot → ARM → pre-uninstall →
  install → verify version AND import → DISARM. While armed, any exit rolls the snapshot back
  (`rollback_install`). Details that still hold:
  - The rollback helper reads dists through `importlib.metadata` (RECORD + INSTALLER), never
    `pip show` text. "pip-owned" = INSTALLER `pip` + a RECORD.
  - Armed pip steps run through `shield` (a subshell ignoring HUP/INT/TERM): pip writes a
    wheel's RECORD last.
  - plan refuses a dep owned by another installer whose pin changes; the rollback fails loudly
    on `leftovers`.
  - Never let pip write to the console; never `--force-reinstall`; never drop the pre-uninstall.
- **macOS runs under bash 3.2** (`curl | bash` uses `/bin/bash`): no heredoc inside `$(…)` (the
  helper is written with a plain heredoc), `${arr[@]+"${arr[@]}"}` for a maybe-empty array,
  no `mapfile`, no `shopt -s globasciiranges`. Check with the `bash:3.2` container
  (`RealMetadata` needs a pip-installed pytest; apk's has no INSTALLER). From a worktree agent,
  run it through a small python driver (the worktree guard refuses `sh -c` text):

  ```
  docker run --rm -v <worktree>:/w:ro bash:3.2 sh -c 'ln -sf /usr/local/bin/bash /bin/bash; apk add -q python3 py3-pytest; adduser -D -u 1000 t; su t -c "cd /w && INSTALLER_TEST_BASH=/bin/bash python3 -m pytest -q -p no:cacheprovider --noconftest -k \"not RealMetadata\" tests/test_installer_macos_key.py tests/test_installer_preflight_rollback.py"'
  ```

## Windows

- Never truncate or discard pip output (`| Out-Null`): capture it, check `$LASTEXITCODE`, print
  the whole output on failure.
- The key is resolved BEFORE step 2: step 2 deletes the old winremote config, and an invalid
  explicit key must be refused first.

## Tests

- **Linux:** `tests/linux_installer_harness.py` (`linux_box`) + `tests/fake_linux_tools.py`.
  - The venvs are REAL (`python -m venv --without-pip`) with a stub `pip` package that runs
    `tests/fake_pip.py` against the venv's own site-packages, so the installer's version and
    import checks run against real files, under its `env -i`.
  - The fake systemd is stateful: `daemon-reload` loads the unit; `start`/`restart` "run" it (the
    version its ExecStart python has, the key from `--auth-key` or the EnvironmentFile);
    `start` on an active unit loads nothing new. The fake curl answers the tarball, `/health`
    and `/mcp` (Bearer from `-H @-`) from that state.
  - Every shim and a logging shim for each common command record their argv (`box.argvs()`):
    that is what the "never in an argv" checks read. `box.snapshot()` (inode + mtime + bytes)
    proves "nothing changed".
  - The same harness runs the pre-dev32 installer (system pip via fake_pip, git ls-remote), so a
    RED test fails for the right reason.
  - Fixture keys look like secrets: append `# airuleset:secret-ok <reason>` to the `git add`
    and the `git commit`.
- **macOS:** `tests/test_installer_macos_key.py` and the block's
  `tests/test_installer_preflight_rollback.py` (fake_pip, two site dirs, signals, a lost
  console). `installer_harness.log_every_command` records argv there.
- **Windows:** `tests_windows/test_install_ps1_key.py` loads install.ps1's own functions (AST,
  `FunctionDefinitionAst`, dot-sourced in a `foreach` statement, not `ForEach-Object`, so they
  land in the script scope) into powershell.exe 5.1, plus one real cmd.exe run of the generated
  `.bat`. Probe them locally with pwsh 7 (`.claude/rules/child-output.md` describes the
  tarball) through a python driver; ACL and cmd stay Windows-only.
- **Static checks find the first code line** (`_find_first_code_line`). It skips `#` and
  `Write-Host` lines, but NOT bash `echo` lines. Keep `pip install … remoteos-mcp` and
  `pip uninstall … remoteos-mcp` out of `echo` text.
