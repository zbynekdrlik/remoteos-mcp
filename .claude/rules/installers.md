---
paths:
  - "install-linux.sh"
  - "install.sh"
  - "install.ps1"
  - "installers/**"
  - "scripts/build_installers.py"
  - "src/remoteos/selfupgrade.py"
  - "tests/test_selfupgrade.py"
  - "src/remoteos/updater.py"
  - "tests/test_updater.py"
  - "tests_windows/test_install_ps1_updater.py"
  - "uninstall-linux.sh"
  - "tests/test_installer_*.py"
  - "tests/installer_harness.py"
  - "tests/linux_installer_harness.py"
  - "tests/fake_linux_tools.py"
  - "tests/fake_pip.py"
  - "tests_windows/test_install_ps1_key.py"
  - "tests_windows/test_install_ps1_wheelhouse.py"
  - "tests_windows/test_install_ps1_upgrade_task.py"
---

# One-liner installers (install-linux.sh / install.sh / install.ps1) — gotchas

The live-deploy procedure lives in the `/remoteos-install` skill. This file covers the code.

## Generated parts (#71)

- **`install-linux.sh` and `install.ps1` are generated: edit `installers/install-linux/*.sh` and
  `installers/install-ps1/*.ps1`, run `python3 scripts/build_installers.py`, commit both.** The
  parts are concatenated byte for byte in file-name order; `tests/test_installer_generated.py`
  fails when a committed file differs. Line numbers in old tickets refer to the file before the
  split. The one-file budget applies per part now. `install.sh` is not split.
- A part holds whole functions, or the flow (`90-*`): the last part's `main "$@"` keeps a truncated
  download from half-running. shellcheck and the static tests read the generated files.

## Admin: the sudoers drop-in (#71, owner decision on #67)

- `/etc/sudoers.d/90-remoteos-mcp` = exactly `<account> ALL=(ALL) NOPASSWD:ALL\n`, one line, so
  the hand-made files of strih-pp, stream-pp, strih-snv and stream-snv are adopted untouched.
- **Order:** `prepare_sudoers` right after the EXIT trap, before the download: render into the work
  dir, `visudo -cf`; a refusal or no visudo = "nothing was changed". `install_sudoers` after the proof and
  the prune: a failed proof leaves no admin change, so the rollback has nothing to undo. It restarts
  nothing (`CHANGED` untouched): sudo reads the file per call.
- The temp name is `<file>.new`: sudo skips an includedir name with a dot, so a half-written file is
  never read. Linux keeps it in `WRITE_TMP` for `on_exit`.
- A root unit (`SERVICE_UID` 0, the cams) gets none, and an existing file is left alone.
- macOS: `install.sh` runs as the user. `prepare_sudoers` the same (inside the block, it needs
  `WORK_DIR`; a real `visudo -cf` needs no root), then after the health check `sudo -n cmp`, else
  `admin` = `sudo -n`, else sudo's own prompt when `/dev/tty` opens. **No sudo = a `[!]` warning, the
  install stands:** only a refused line fails the run. `uninstall.sh` likewise.

## SelfUpgrade (#71): `src/remoteos/selfupgrade.py`

- Pure `upgrade_plan` (ref check + one argv per OS) and `start` (the I/O). Never in-process, and
  never a child of the server: the switch stops the server, and the proof + rollback live in the installers.
- **The ref check is the installers' (`ref_ok`, `Test-RemoteOSRefName`) plus 100 characters:** a ref
  they refuse must fail in the tool, not in a detached run nobody reads. install.ps1's task checks
  it again: the file is the user's.
- **Linux:** a `systemd-run` unit's cgroup is `/system.slice/remoteos-mcp-upgrade-<ts>.service`, which
  the in-service guard (`/remoteos-mcp\.service`) does not match. The command line holds no `$` or `%`:
  systemd expands both.
- **macOS: `launchctl submit` keeps the job alive** (launchd reruns it when it exits): the
  command's last step is `launchctl remove <its label>`, which never returns (it kills the job).
- **Windows:** `Get-RemoteOSUpgradeCommand` is fixed at install time and goes as `-EncodedCommand`
  in the task's arguments: base64 cannot match `Stop-RemoteOSServer`'s command-line patterns, and
  there is no script file the desktop user could edit. It sets `REMOTEOS_MCP_RELAUNCHED=1`: a hidden
  run must not relaunch for UAC or wait for Enter. `Register-RemoteOSUpgradeTask` leaves an unchanged
  task alone, because a SelfUpgrade run is inside it. `Stop-RemoteOSServer` skips `$PID` for the same reason.
- A box needs one installer run at 0.7.0.dev39+ before SelfUpgrade works (the drop-in, the task).
- `install.sh` resolves no short sha, so `macos_plan` refuses one. raw caches `main` for minutes: an
  upgrade right after a merge may run the previous installer against the new source; pass a sha.
- The runners' `curl` has `--max-time 120`: a transient unit or a launchd job has no runtime limit.
- `systemd-run` answers 0 once the unit is queued, so a read-only `/var/log` or a held install lock
  still replies "started": `/health` decides, as the reply says.

## The updater (#65): `src/remoteos/updater.py`

- Pull model: scheduler -> `python -m remoteos.updater` -> `release_channel.choose` -> pure `decide`
  (forward only) -> `selfupgrade`. `selfupgrade` is imported at the call: it reaches fastmcp through
  `errors.py`, and the decisions must be logged even on a box with broken packages.
- The ring: `updater.json` next to `config.json` (Linux `/etc/remoteos-mcp`). `REMOTEOS_MCP_RING`
  on an installer run sets it, a run without it keeps it (SelfUpgrade and the updater's own runs pass
  none), refused before anything changes when it is neither `stable` nor `canary`. No BOM: `json.loads`.
- **Set up after the proof**: nothing there touches the server; a failure leaves the proven install.
  Linux: `install_updater` (`47-updater.sh`), `write_if_changed` through HERE-STRINGS (a pipe runs it
  in a subshell: CHANGED and WRITE_TMP are lost). macOS: at the end of `install.sh`. Windows is the
  exception: `Register-RemoteOSUpdaterTask` sits with the other tasks, under `@rollback`.
- **Not on enable-only boxes** (camera-box's cams, #60): a second updater would fight its pin; an
  enable-only run removes a timer a default run left. Its failure to enable is a `[!]`, like macOS.
- **Two tries per release** (`updater-state.json`, counted BEFORE the start, so a killed run counts): a
  box-specific failure stops the server and rolls back, and daily repeats are the restart loop the
  design rejected. A newer tag, or a success (the version moves), ends it. The drift report names the box.
- **One run at a time** everywhere: Linux `flock`, Windows `Enter-RemoteOSInstallLock` (a Global named
  mutex: it dies with its holder, an abandoned one is simply ours), macOS `<config>.install-lock` (a
  directory + pid, a dead run's is taken over).
- Windows: the task runs `pythonw.exe` (a daily console window would steal the focus on a live
  desktop; `sys.stderr` is None there, so `--log-file`) as the desktop user, like
  RemoteOSMCP-Upgrade, NOT as SYSTEM as the first ticket text said: `windows_plan` writes
  `upgrade-ref.txt` under `Path.home()`. Interactive logon: no one logged on = no run, but then the
  server's task has no session either; `StartWhenAvailable` catches up.
- `Stop-RemoteOSServer`'s `-m remoteos` pattern also matches `-m remoteos.updater`. Harmless: it
  has handed over to the Upgrade task by then.
- macOS: `RunAtLoad`: launchd runs a missed calendar date only after a sleep, not after a power-off.

## The key, on every OS (#12, since 0.7.0.dev32)

- **The key is never in a unit, a plist's ProgramArguments, a command line, an argv or the
  installer's output.** The server reads `REMOTEOS_AUTH_KEY` (`__main__.py`, the click
  `envvar`), and an env value counts as explicit, so it also beats a `remoteos.toml` `auth_key`.
  - Linux: 0600 `/etc/remoteos-mcp/config.json` + the 0600 EnvironmentFile
    `/etc/remoteos-mcp/remoteos-mcp.env` (`REMOTEOS_AUTH_KEY=<key>`).
  - macOS: the plist's `EnvironmentVariables`; the plist and `config.json` are 0600.
  - Windows: `start-remoteos.bat` does `set "REMOTEOS_AUTH_KEY=<key>"` right before the server
    line and `set "REMOTEOS_AUTH_KEY="` right after `set EXIT_CODE=%errorlevel%`: every program
    cmd.exe starts inherits its environment, so the loop's netstat, findstr and timeout never get
    the key (review round 10). `Get-RemoteOSKeyFromStartScript` matches a non-empty value only.
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
  `read -rs k` + `… | REMOTEOS_MCP_AUTH_KEY="$k" sudo --preserve-env=REMOTEOS_MCP_AUTH_KEY bash`:
  the prefix assignment reaches only sudo's environment, never an argv, and nothing is exported
  to curl or the rest of the shell (round 8; `export` leaked it to every later command).
- **The server side of "the key never in a child's environment" starts at the #12 merge.** A
  `REMOTEOS_MCP_REF` older than that (camera-box's pin before its bump) installs a server that
  keeps `REMOTEOS_AUTH_KEY` in `os.environ`, so Shell children inherit it. camera-box's pin must
  be at or after the merge commit when it delegates to this installer.
- **Never put the key into an argv on the way either.** Python reads a key file from stdin or
  by path and prints the key to a `$(…)`; files are written by `printf` / a here-string into a
  0600 temp file, then renamed (macOS `write_private` and Linux `write_if_changed` keep the
  temp path in `WRITE_TMP`, which `on_exit` removes); the health check sends `Authorization:
  Bearer` with `curl -H @-`; `GH_TOKEN` goes the same way. **Every loopback curl is `curl -q
  --noproxy '*'`** (review rounds 11-12): `--noproxy` keeps an `http_proxy`/`all_proxy` from the
  environment (kept for pip) or a `.curlrc` proxy off `http://127.0.0.1` — a remote proxy cannot
  reach the box (every check fails, a working agent is stopped), a local one would read the key;
  `-q` keeps every other `.curlrc` option off the request that carries the key (a `trace` would
  write the Authorization header to a file). A static test pins both; the source download is
  exempt (it may need the proxy). Read the secret env vars and `unset` them (Windows:
  `Remove-Item Env:`) BEFORE the installer's FIRST child — the Python check, `query user`, brew,
  winget — not after it: every child inherits them (review round 9; pinned on Linux and macOS by
  the child-environment tests, on Windows by a static order test).
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
    stays while a legacy process may still import from it: it goes after a restart run's
    proof, or at a no-op enable-only run whose running install is proven (it runs our unit's
    venv; review round 14 — a cam never gets a restart run). Default `restart`.
- **A pending switch** (`/etc/remoteos-mcp/.switch-pending`): written after `arm`, holding the
  unit's `systemctl show -p InvocationID --value` (systemd makes a new one at every start and
  keeps it while the unit is inactive). Removed after a passed proof, by "Nothing was switched",
  by a no-op enable-only run on a proven box; `restore_previous` puts it back to its pre-run
  state. A run that finds it with the SAME InvocationID restarts instead of taking the files as
  unchanged: after enable-only or a run killed with no trap (SIGKILL), the running process is
  not what the files say, and `systemctl start` on an active unit loads nothing new. Another
  InvocationID = the unit started since (a cam rebooted into its enable-only switch): the mark
  is stale and `check_running_install` judges what runs (round 8: a stale mark stopped and
  disabled a working cam on the next failing restart run). An enable-only run that changed nothing removes the mark it wrote ONLY when
  `check_running_install` finds the running install proven (round 5, narrowed in round 6): a
  cam whose venv camera-box's lib upgraded in place without a restart runs another version
  than its files, so its no-op run keeps the mark. A mark an earlier run left sets `CHANGED`.
- **Reading what runs polls like the proof (review round 10).** `check_running_install` sets
  `NO_ANSWER` for an active unit whose `/health` does not reply (it may be binding);
  `poll_running_install read` (before a restart run's switch, at a no-op enable-only run's end)
  polls only while that holds, so one missed poll never turns a working box into "nothing
  proven". A definite answer (inactive, another version, authentication off) returns at once.
- **PROVEN (`check_running_install`, review round 6)** = the unit existed, no pending switch,
  the service is active, `/health` reports the version the unit's own ExecStart python has
  (`python_version`, the interpreter is the ExecStart's first word, `python*` only), and an
  unauthenticated `POST /mcp` answers 401. Read-only, the key is never sent. A restart run
  reads it BEFORE the switch (what a failure may go back to); a no-op enable-only run reads it
  at the end. "A unit and no pending mark" alone is NOT proven: it restored the state that had
  just failed its proof, or files the running process never ran.
- **Order (keep it):**
  1. Validate the inputs (`REMOTEOS_MCP_REF` default `main`, empty = `main` as camera-box reads
     it; MODE / USER / POLICY), refuse a run inside `remoteos-mcp.service` (`/proc/self/cgroup`,
     `CGROUP_FILE`, redirected by the harness: from the agent's own Shell the switch's restart
     kills the run half-way and its rollback waits on that same stop until TimeoutStopSec; review
     round 13; the uninstaller has the same guard), take `flock -n` on
     `/run/remoteos-mcp-install.lock` (a
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
     `poll_running_install await` (`check_running_install` up to HEALTH_TRIES times, read-only,
     no key): proven
     → "Rolled back … proved itself" / "Nothing was switched" (the pending mark removed); not
     proven → `stop_unproven` (round 7: the restored FILES may not be what the process ran, e.g.
     an EnvironmentFile changed under it to an empty key = authentication off). Not PROVEN before
     the run (a fresh box, a pending switch, files never run, auth off) → `stop_unproven`, the
     files + the pending marker kept for a re-run; `stop_unproven` re-stamps a kept mark AFTER the
     disable (systemd unloads an inactive, disabled unit: its InvocationID reads empty until it
     starts again; the fake models that), so the re-run takes it as pending, not as "has run
     since" (rounds 9-10). An
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
  - **One delivered file (decided in review round 6).** `curl … | sudo bash` delivers ONE
    file, and camera-box installs pinned older refs, so the installer cannot source a second file
    from the ref's tarball or from `main` (raw caches `main` for minutes: version skew). The
    source is split into parts since #71 (top of this file).
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
  twin until dev32). Order: pin ONE commit of `REMOTEOS_MCP_REF` (#64: validated by `ref_ok`
  before the first child; a 40-hex sha as it is, else `git ls-remote` of `refs/tags/<r>^{}`,
  `refs/tags/<r>`, `refs/heads/<r>`, picked in that order) → record the installed
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
- **Python 3.11+ (#64):** one `python_ok` per bash installer (`tests/test_installer_python_gate.py`
  runs each exactly as written, under bash 3.2 too), `Test-RemoteOSPythonVersion` in install.ps1.
- **macOS runs under bash 3.2** (`curl | bash` uses `/bin/bash`): no heredoc inside `$(…)` (the
  helper is written with a plain heredoc), `${arr[@]+"${arr[@]}"}` for a maybe-empty array,
  no `mapfile`, no `shopt -s globasciiranges`. Check with the `bash:3.2` container
  (`RealMetadata` needs a pip-installed pytest; apk's has no INSTALLER). From a worktree agent,
  run it through a small python driver (the worktree guard refuses `sh -c` text):

  ```
  docker run --rm -v <worktree>:/w:ro bash:3.2 sh -c 'ln -sf /usr/local/bin/bash /bin/bash; apk add -q python3 py3-pytest; adduser -D -u 1000 t; su t -c "cd /w && INSTALLER_TEST_BASH=/bin/bash python3 -m pytest -q -p no:cacheprovider --noconftest -k \"not RealMetadata\" tests/test_installer_macos_key.py tests/test_installer_preflight_rollback.py tests/test_installer_macos_sudoers.py"'
  ```

## Windows

- **`install.ps1` stays ASCII-only (#12, PR #55).** Windows PowerShell 5.1 reads a BOM-less
  script file (`-File`, `Parser::ParseFile`) in the ANSI code page. A UTF-8 em dash
  (`E2 80 94`) then becomes cp1252 `â€”`, and its `0x94` byte is U+201D, which PowerShell takes
  as a CLOSING double quote: the string ends early and the whole file stops parsing ("Missing
  closing '}'"). `irm | iex` hides the bug (the web response is decoded as UTF-8), so only a
  file run shows it. Write `-` / `--` / `...`, never `—` / `…` / smart quotes, in comments too.
  Also no PowerShell 7-only syntax (`??`, `?.`, `&&`, `||`, ternary). Guarded by
  `tests/test_installer_windows_key.py` (`test_install_ps1_is_ascii_so_windows_powershell_5_1_parses_it`,
  `test_install_ps1_uses_no_powershell_7_only_syntax`).
- Never truncate or discard pip output (`| Out-Null`): capture it, check `$LASTEXITCODE`, print
  the whole output on failure.
- The key is resolved BEFORE step 2: step 2 deletes the old winremote config, and an invalid
  explicit key must be refused first.
- **One commit per run (#12):** `$Ref` (`REMOTEOS_MCP_REF`, default `main`, checked by
  `Test-RemoteOSRefName`) → `Invoke-RestMethod api.github.com/.../commits/$Ref` → `$sourceSha`
  (must be 40 hex chars), right after the gate's fail branch and before the relaunch and any
  change. The preflight gets `-ConstraintsUrl raw/.../$sourceSha/constraints.txt` and
  `-SourceUrl archive/$sourceSha.zip`, `$InstallerUrl` = `raw/.../$sourceSha/install.ps1`, and
  `installed-ref.txt` (next to `config.json`, no secret) holds the sha for #60's updater and the
  next run's rollback, written only after the health check passed. Never reintroduce a literal
  `/main/` or `main.zip` there (a static test).
- **Download first, install offline, roll back (#64).** Order: key resolved -> [2/7]
  `New-RemoteOSWheelhouse` (the preflight, before any change but a Python install: the commit's
  `constraints.txt` fetched through Python's urllib into the wheelhouse, `pip download --timeout
  60 --retries 10 -c <it>` of `archive/$sourceSha.zip`, then `pip wheel --no-deps` over EVERY
  non-wheel file it left: the archive and sdist-only deps such as pyautogui cannot be built
  offline; built in `<sha>.partial`, renamed when complete, so a wheelhouse is whole or absent;
  a complete one is reused) -> `Get-RemoteOSRollbackPoint` (installed-ref.txt's sha + its
  complete wheelhouse) -> `Stop-RemoteOSServer` -> `Install-RemoteOSWheelhouse` (pre-uninstall,
  `--no-index --find-links <wh> -c <wh>\constraints.txt remoteos-mcp==<ver>`, version + import
  check) -> config, task, firewall -> `Start-RemoteOSServer` + `Test-RemoteOSServerHealth` ->
  installed-ref.txt -> `Remove-RemoteOSStaleWheelhouses` (keep current + previous).
  - **Every failure after the stop is `Exit-RemoteOSAfterFailure -Reason ... @rollback`**, never
    `Exit-RemoteOSInstaller` (a static test): stop, reinstall the old wheelhouse, start, prove the
    OLD version, `[X] <reason> -- rolled back to <ver>`, exit 1. The `$rollback` splat holds
    scriptblocks resolved by dynamic scope (`$Port`, `$ConfigDir`, `$startDirect` of the script):
    never name a parameter of `Exit-RemoteOSAfterFailure` / `Invoke-RemoteOSRollback` like
    those, and never `.GetNewClosure()` them (a closure's module scope cannot see the script's
    functions).
  - No `$env:PIP_CONSTRAINT` URL: pip would fetch it even with `--no-index`. An inherited one
    (an older installer, same console) is removed at the top; `PIP_NO_INDEX`/`PIP_FIND_LINKS` (the
    hand restore) are honoured by the preflight.
  - **Both wheelhouses are proven installable before the stop** (review round 1):
    `Test-RemoteOSWheelhouseInstallable` = `pip install --dry-run --ignore-installed --no-index`.
    Wheels are per interpreter: a box whose Python changed since would fail only after the stop,
    and the rollback the same way. A kept wheelhouse that fails is rebuilt; a rollback point that
    fails is dropped (with a `[!]` line).
  - **The wheelhouse root feeds an elevated pip from the desktop user's profile** (review round
    1): `Protect-RemoteOSWheelhouseRoot` before the download: owner Administrators, protected
    ACL, SYSTEM + Administrators full, the desktop user read; a root that is not like that
    (`Test-RemoteOSWheelhouseRootTrusted`, by SID) is deleted with everything in it. **The root is
    `%ProgramData%\remoteos-mcp\wheelhouse` since #65**, and `%ProgramData%\remoteos-mcp` gets the
    same protection FIRST: any account may create a folder in ProgramData, and its owner could swap
    what is inside. The old `<config>\wheelhouse` is never trusted: the installed commit's wheelhouse
    is built again (`New-RemoteOSRollbackWheelhouse`), and the old root goes after the proof. **Every
    elevated delete goes through `Remove-RemoteOSTree`** (review round 2): 5.1's `Remove-Item
    -Recurse` enters a junction and deletes the TARGET's contents, and Get-Acl/Set-Acl follow one,
    so a link as the root is removed alone, a link as the config folder is refused, and a
    directory is walked entry by entry (a link inside removed alone, read-only files cleared):
    .NET Framework's recursive `Directory.Delete($p, $true)` throws "The parameter is incorrect"
    on a junction inside (it calls DeleteVolumeMountPoint; CI on 12e8902). The old
    `.winremote-mcp` and other-account config cleanups use it too (a static test: no
    `Remove-Item -Recurse` anywhere in install.ps1). `Test-RemoteOSLink` fails closed (a path
    that exists but cannot be read counts as a link). The prune does nothing when the root or its
    folder became a link since the check, and deletes only `<sha>` / `<sha>.partial` names: a
    root swapped for a junction to a system folder mid-run cannot make it delete that folder.
  - The installed version is read from a `REMOTEOS_VER=` line (a stderr warning on the merged
    output must not read as the version and roll back a good install).
  - The firewall rule: the new one is created BEFORE the old ones are removed, so a failure that
    rolls back keeps the LAN reachable.
  - The pre-uninstall's exit code is fatal now (it was ignored); the rollback runs the same
    uninstall. A re-run of the same commit keeps only that wheelhouse (`@($sha, $sha)`): the
    proven commit is the current one.
  - **The rollback trap (#65, `35-rollback-trap.ps1`).** PowerShell has no EXIT trap for a killed
    process, and an SSH drop kills the session's children, so the watcher is a scheduled task,
    `RemoteOSMCP-Rollback`. Before the stop (only with a rollback point):
    `rollback-pending.txt` (the marker, next to config.json) + `rollback-trap.ps1` in the
    Administrators-only data folder + `schtasks /Run`. The script writes
    `rollback-watching.txt` first, and the arm waits for it: `schtasks /Run` only requests a start. It
    polls: marker gone = exit; the installer's pid gone, or 30 minutes passed (Ctrl+C in a piped run
    keeps its powershell, a pid may be reused), with the marker there = take the install lock (held and
    the installer dead = a new run owns the box, exit; held by the installer still alive = roll back
    anyway), `Invoke-RemoteOSRollback`, `rollback.log`. The flow releases the lock at its very end: `irm | iex`
    leaves the console open, and a held mutex would refuse every later run. A new arm first
    `schtasks /End`s an old watcher. The
    installer's own functions go in as text (`(Get-Command).Definition`), so it is the same code and
    needs no download. Disarmed after the proof, and at the end of `Exit-RemoteOSAfterFailure`
    (`Disarm` in the splat), never before its rollback. A trap that cannot start is a `[!]`: the run's own
    rollback still stands. It runs as the desktop user, never SYSTEM: its pip runs a Python the user
    may own. Where that user is no administrator, pip may be refused: `rollback.log` says so.
  - **uninstall.ps1** has the elevation gate before its first change, the console user's profile
    (`Get-RemoteOSDesktopProfile`), and copies of `Test-RemoteOSLink` / `Remove-RemoteOSTree` (a test
    keeps them equal). It stops the server through Win32_Process: 5.1's Get-Process has no CommandLine.
  - **Size:** `irm | iex` delivers ONE file; its source is split into parts since #71 (top of
    this file).
  - A native child's output goes through `Invoke-RemoteOSNative` (`2>&1`, each record as text,
    under `Continue`). In argument mode a bare `[pscustomobject]@{...}` is a STRING: wrap it in
    parentheses (bit the tests).
  - Probe locally: tests_windows' wheelhouse tests run under pwsh 7 on Linux with a `powershell`
    symlink to pwsh first in PATH and `--noconftest` (a venv's `bin/python`); the real-pip
    preflight against GitHub/PyPI works the same way.
- **The `shell` CI job** runs `shellcheck install-linux.sh install.sh uninstall*.sh` and the
  macOS installer tests under the `bash:3.2` container (the command above). Keep all bash
  installers shellcheck-clean (ubuntu-latest's shellcheck).
- **The elevation gate (#57) is the first thing after the banner, before any change**:
  `Get-RemoteOSElevationAction -IsAdmin -Interactive -ExplicitKey -Relaunched` (pure) → proceed |
  relaunch | fail; `Test-RemoteOSInteractiveSession` (no `SSH_CONNECTION`/`SSH_CLIENT`, session
  != 0, and `[Console]::IsInputRedirected` false: the agent's own Shell tool runs in the desktop
  session with stdin NUL, and nobody would click its UAC prompt) feeds it. Static tests pin the
  call, and the exit at the end of the fail and relaunch branches, before the first
  state-changing line of the script's flow (winget / curl.exe / the Python installer count).
  - relaunch = `Invoke-RemoteOSElevatedRelaunch` (the command itself is the pure
    `Get-RemoteOSRelaunchCommand`, run for real in tests_windows): `Start-Process powershell
    -Verb RunAs -PassThru` + `WaitForExit()`, never `-Wait` (5.1's `-Wait` also waits for every
    descendant, and the elevated run leaves the server running). Only `Start-Process` is in the
    try (a refused UAC, or no process object → `$null` → "nothing was changed"). The
    elevated window runs the installer as a CHILD powershell and then `Read-Host`s, because an
    `exit` inside the installer would close the window before the person reads it. The command
    goes over as `-EncodedCommand` (no quoting). An explicit key is never relaunched: the
    elevated process gets a fresh environment, and an argv or a file would expose the key.
  - Elevation does NOT pass the caller's environment: anything the elevated run needs (the
    resolved ref, `REMOTEOS_MCP_RELAUNCHED=1`) is set inside the encoded command. A relaunched
    run that is still not elevated (UAC off for a standard user: RunAs gives the same token, no
    prompt) fails instead of relaunching again; it also does not pause at exit.
  - The gate's inputs are `[Parameter(Mandatory)]` and static tests pin the call-site arguments
    (`-InputRedirected ([Console]::IsInputRedirected)`, `-Relaunched $relaunched`): a dropped
    `[bool]` argument would silently read as `$false` (review round 2 mutants).
  - **Every exit after the gate is `Exit-RemoteOSInstaller -Code <n> -Pause $pauseOnExit`**
    (never a bare `return`: it exits 0, and the relaunch forwarded that as success; never a bare
    `exit`; a static test). Its `Read-Host` is in a try: under `-NonInteractive` it throws, and the
    health loop's catch-all would swallow that and go on.
    `$pauseOnExit` = interactive, no `$PSCommandPath` (piped), not relaunched: `exit` inside
    `irm | iex` closes the person's console, so it waits for Enter first. Tests that bound a
    failure block read it up to its closing brace.
  - After the gate, the scheduled-task and firewall `catch` blocks exit 1: the run is
    elevated, so a failure is real.
  - `$startDirect` = interactive AND `$env:USERNAME -ieq $desktopUser`. Otherwise (SSH, session
    0, or elevated as ANOTHER account: a standard user typed an admin's password at UAC, so a
    direct start would run as that admin, pass the health check, and hide that the task's user
    may not reach this run's per-user Python; `Test-RemoteOSPythonReachable` refuses such a
    Python, or a missing one this run would install per-user, BEFORE any change): never
    `Start-Process wscript` (it dies with the SSH
    session); `schtasks /End` + `schtasks /Run /TN RemoteOSMCP` under `$ErrorActionPreference =
    "Continue"` (5.1 turns native stderr under `2>&1` into a terminating error with `Stop`), then
    the health loop also requires `Get-RemoteOSListenerSessionId -Port` != 0 (session 0 = exit 1,
    `$null` = keep polling; still `$null` at the end = "the session could not be read", not "did
    not respond").

## Enrollment (#26): `REMOTEOS_ENROLL`, `REMOTEOS_ENROLL_SSH_KEY`

- Unset = none of this runs: no sshd, no POST, no machine-wide Python. Tests pin it per OS.
- **Read and removed from the environment with the other secrets, before the first child.** The URL's
  token is its receiver's only auth, like a key. Judged before anything changes (`enroll_url_ok`,
  `ssh_key_ok`, the PowerShell twins): a public key line with options (`command=`) or a second line
  is refused, and a quote in the URL could rewrite curl's config.
- **After the proof, last.** A failure exits 1 and leaves the proven install up, no rollback.
  Windows: `Exit-RemoteOSEnrollmentFailure`, because `Exit-RemoteOSInstaller` after the stop is
  reserved for the rollback path (a static test). Linux: `enable-only` is refused, nothing would be proven.
- **The POST:** bash: the URL through `curl -K /dev/fd/5`, the body on stdin, so neither is in an argv;
  `-q`, `--proto =https`, a named agent. The box's proxy stays usable. PowerShell: `Invoke-RestMethod`
  in-process with bytes. Three tries.
- **The UAC relaunch never carries it:** `-ExplicitKey ([bool]$explicitKey -or [bool]$enrollUrl)`, the same
  reason as an explicit key: an argv or a file would expose the token. The command says "as Administrator".
- **Windows Python:** `Install-RemoteOSMachinePython` runs before `Test-RemoteOSPythonReachable`: an
  all-users Python is reachable by the desktop user. `Start-Process -Wait` (the installer leaves no
  descendant), exit 0 or 3010, and `python312.dll` must exist, else `[X]`. `$python` is the full path.
- **`administrators_authorized_keys` is ignored by sshd unless only Administrators and SYSTEM reach it:**
  `icacls /inheritance:r`. No BOM in either key file.
- macOS: no sudo = Remote Login stays off with a `[!]`, the box still enrolls.
- Not proven by CI: the OpenSSH capability, the real Python installers and a fresh Windows box.

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
- **Windows ACL tests (#64):** `FileSystemAccessRule` refuses a mask above FullControl (a
  GENERIC_WRITE ACE cannot be built through it, and PowerShell refuses the enum cast): build such
  an ACL through SDDL (`SetSecurityDescriptorSddlForm(... + '(A;OICI;GW;;;BU)', 'Access')`).
  Junctions: `New-Item -ItemType Junction` (5.1, no admin); the same tests use `SymbolicLink`
  under pwsh on Linux (`$IsLinux`), so the link cases also run in the local pwsh probe.
- **Static checks find the first code line** (`_find_first_code_line`). It skips `#` and
  `Write-Host` lines, but NOT bash `echo` lines. Keep `pip install … remoteos-mcp` and
  `pip uninstall … remoteos-mcp` out of `echo` text.

- **A Linux box too old for `SelfUpgrade` (before dev39) upgrades over MCP too, no SSH.** The installer refuses to run inside `remoteos-mcp.service`, but a transient unit is outside it: through `Shell`, `sudo -n systemd-run --unit=remoteos-upgrade --collect --setenv=REMOTEOS_MCP_REF=<sha> --setenv=HOME=/root bash -c 'curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/<sha>/install-linux.sh | bash'`, then poll `/health`. Needs the passwordless sudo of `/etc/sudoers.d/90-remoteos-mcp`. Done on strih-pp and stream-pp, dev20 to dev49.
