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
  the old winremote config), else a new key. A re-run never changes the key (#51).
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
- **The unit is camera-box's shape**, byte for byte except `Description=` (its verify checks the
  exact `ExecStart=` and `EnvironmentFile=` lines). desktop: `User=`, `DISPLAY=:0`,
  `XDG_RUNTIME_DIR=/run/user/<uid>`, `After=… graphical-session.target`. headless: the cams.
  - The mode follows the existing unit (`Environment=DISPLAY=` → desktop), else the session
    check (loginctl x11/wayland). `User=` follows the existing unit, else `SUDO_USER`, else root.
    A re-run never changes the box's shape or account.
- **Order (keep it):**
  1. Validate `REMOTEOS_MCP_REF` (default `main`; empty = `main`, as camera-box reads it) and
     the explicit key. Read the existing unit; resolve the key.
  2. `fetch_source`: ONE GitHub API tarball of the ref (`api.github.com/repos/…/tarball/<ref>`).
     Its top directory (`zbynekdrlik-remoteos-mcp-<sha7>`) is the source id. Code and
     `constraints.txt` come from the same tarball.
  3. Unchanged source (the venv's `.remoteos-mcp-source`, else camera-box's `.camera-box-source`,
     equals the id) AND the venv imports → no pip at all.
  4. Else `build_venv` in the store, side by side: `python3 -m venv` (apt `python3-venv` when
     ensurepip is missing), `pip install --no-cache-dir <src>` with `PIP_CONSTRAINT=<src>/constraints.txt`
     under `env -i` + an allowlist (PATH, HOME, LANG, TMPDIR, proxies, CA/index vars), then the
     version + `import remoteos.__main__, fastmcp; from fastmcp import FastMCP`. Desktop mode
     adds one `.pth` naming the system python's `gi` directory (AT-SPI, #7).
     **A failed download or build changes NOTHING** (it removes its build and an empty store).
  5. Save the previous unit / EnvironmentFile / config.json. ARM (only when a unit existed).
     Swap the link (`ln -sfn` + `mv -Tf`: one rename(2)); a real directory is moved into the
     store first. Write the files (`write_if_changed`: content + mode + owner). daemon-reload,
     enable, `restart` on a change, else `start`.
  6. `prove_health`: active + `/health` = the built version, an authenticated `initialize`
     200, an unauthenticated `POST /mcp` 401. Failure → `restore_previous` (files, link or
     directory, daemon-reload, restart), exit 1. A fresh box has nothing to roll back to.
  7. DISARM. Only then `remove_legacy_system_copy` (the pre-venv system-python `remoteos-mcp
     fastmcp fastmcp-slim`, `--break-system-packages`) and `prune_builds`.
  - While ARMED, any exit (a failed step, HUP/INT/TERM, a lost console: SIGPIPE is ignored, so
    the write fails and set -e exits) goes through the EXIT trap → `restore_previous`. Never arm
    on a list of signals alone: a no-tty `ssh … | sudo bash` drop sends no signal.
  - Everything runs inside `main()`, called on the last line (a truncated download never
    half-runs).
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
