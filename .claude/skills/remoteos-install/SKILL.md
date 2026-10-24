---
name: remoteos-install
description: >
  How to install, upgrade, or redeploy remoteos-mcp on any managed machine
  (Windows, macOS, Linux). Use when: installing on a new machine, upgrading
  after a source push, diagnosing a service that won't start, or fixing a
  Windows firewall/network issue.
triggers:
  - install remoteos
  - upgrade remoteos
  - deploy to machine
  - redeploy
  - service not starting
  - firewall
  - network profile
---

# RemoteOS Install / Upgrade Procedures

## The golden rule — always use the one-liner installer

NEVER run ad-hoc `pip install` or other manual commands. The installer is the single source of truth.
After pushing source changes, deploy by running the appropriate one-liner on each target via SSH. On Linux never through the agent's own MCP Shell: that run is inside `remoteos-mcp.service`, whose restart would kill it half-way, so the installer (and the uninstaller) refuses with "nothing was changed".

## Windows

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 | iex
```

Handles: pip install from repo, preserves existing auth key on reinstall, VBS hidden launcher (no CMD window), scheduled task with auto-restart, firewall rule.

**One commit per run (#12, since 0.7.0.dev35):** `REMOTEOS_MCP_REF` (default `main`) is resolved through `api.github.com/repos/zbynekdrlik/remoteos-mcp/commits/<ref>` to ONE sha before anything changes; `constraints.txt` (raw) and the pip archive both use that sha (raw caches `/main/` for minutes, so two `main` requests could pair new code with old pins). The sha is printed (`Source: main = commit <sha>`, and `Commit:` in the summary) and written to `installed-ref.txt` next to `config.json`. If GitHub's API cannot answer (rate limit: 60 unauthenticated requests/hour per IP), the run stops with "nothing was changed".

**Run it in an admin PowerShell (#57, since 0.7.0.dev35).** The installer checks elevation before it changes anything:

- Elevated (an admin PowerShell, or SSH as an administrator account: OpenSSH gives it the full token) → it installs.
- Not elevated, at a desktop (console input, no SSH, session != 0), no `REMOTEOS_MCP_AUTH_KEY` → it relaunches itself in an elevated window (one UAC click), waits, and exits with that run's exit code. A file run runs the same file; a piped run fetches the installer of the RESOLVED commit (`REMOTEOS_MCP_REF`, default `main`), not the URL it was piped from: to test a branch's installer, set `REMOTEOS_MCP_REF=<branch>`. A cancelled UAC prompt → exit 1, "nothing was changed". A relaunched run that is still not elevated (UAC off, standard user) fails instead of relaunching again.
- Not elevated over SSH, in session 0, or with no console input (e.g. run from the agent's own Shell tool) → `[X] ... needs an elevated (Administrator) PowerShell -- nothing was changed on this box`, exit 1. Run it as an administrator account.
- A piped run in a person's console waits for Enter before it exits on a failure, so the message stays readable (`exit` in `irm | iex` closes the console).
- Not elevated with an explicit `REMOTEOS_MCP_AUTH_KEY` → the same exit 1: the key never crosses the UAC elevation (only an argv or a file could carry it). Open PowerShell as Administrator, set it there, rerun.
- After the gate, a failing scheduled task or firewall rule fails the run (exit 1) instead of warning.
- **Over SSH (session 0) the installer does not start the server itself**: a server started there dies with the SSH session. It runs `schtasks /End` + `schtasks /Run /TN RemoteOSMCP` and proves health from the task's process (the :8092 listener must run in a session other than 0). The desktop user must be logged in. An interactive run starts the server directly, as before.

**The key (since 0.7.0.dev32, #12):** `start-remoteos.bat` sets `REMOTEOS_AUTH_KEY` in its own environment; the server's command line has no `--auth-key`. The `.bat` and `config.json` (in `C:\Users\<desktop user>\.remoteos-mcp\`) get an owner-only ACL (the user, SYSTEM, Administrators). A reinstall keeps the key: the running `.bat`'s, else `config.json`'s, else `remoteos.toml`'s `[server] auth_key` (`%USERPROFILE%\.config\remoteos\remoteos.toml`), else the old winremote one. It never mints a new key while any of them exists. To install a given key, hand it over in `$env:REMOTEOS_MCP_AUTH_KEY` without putting it on any command line (`powershell -Command "$env:…='K'; …"` puts it in powershell's), e.g. on stdin over SSH: `printf '%s\n' "$k" | ssh user@box 'powershell -NoProfile -Command "$env:REMOTEOS_MCP_AUTH_KEY=[Console]::In.ReadLine(); irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 | iex"'`. The installer drops it from its environment before starting any program, and the `.bat` sets it only for the server itself. The installer never prints the key: read it from `config.json` on the box.

Verify after a (re)install: the key is unchanged (the client config still gets 200), and the server's command line has no key: `Get-CimInstance Win32_Process | ? CommandLine -match '-m remoteos' | % CommandLine`.

### Windows network profile fix (if firewall rule doesn't work)

The installer's firewall rule allows TCP 8092 on **all** profiles (`-Profile Any`), so the profile alone does not block it. If clients still cannot connect, check for another rule or a third-party firewall that blocks the port on a Public network, and set the profile to Private:

```powershell
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
```

## macOS

```bash
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.sh | bash
```

Service runs as a LaunchAgent (`com.remoteos-mcp`). Since 0.7.0.dev32 (#12) the plist hands the server the key in `EnvironmentVariables` (`REMOTEOS_AUTH_KEY`), never in `ProgramArguments`; the plist and `~/.remoteos-mcp/config.json` are mode 600. The key precedence is the Linux one (explicit `REMOTEOS_MCP_AUTH_KEY`, else the running plist's, else `config.json`, else new). The installer never prints the key.

## Linux

```bash
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | sudo bash
# pin a ref (commit sha, tag or branch; default main):
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | sudo REMOTEOS_MCP_REF=<sha> bash
# install a given key: never `sudo REMOTEOS_MCP_AUTH_KEY=… bash` (the key would be in sudo's argv)
read -rs k   # not exported: only sudo's environment gets it, never curl or a later command
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | REMOTEOS_MCP_AUTH_KEY="$k" sudo --preserve-env=REMOTEOS_MCP_AUTH_KEY bash
```

Other inputs: `REMOTEOS_MCP_MODE=desktop|headless` and `REMOTEOS_MCP_USER=<account>` (default: what the existing unit has, else the session check / `SUDO_USER`), `REMOTEOS_MCP_POLICY=enable-only` (install + enable, never start: camera-box's cams). camera-box's `remoteos_mcp_install USER MODE POLICY` maps onto them.

Requires `sudo`. Uses systemd for service management. Supports Ubuntu 24.04+ and other systemd-based distributions.

**Layout (since 0.7.0.dev32, #12): ONE installer, on camera-box's venv layout.** camera-box's `scripts/lib/remoteos-mcp.sh` and this installer produce the same box, so they no longer fight over the unit.

- `/opt/remoteos-mcp-venv` is what the unit runs: a symlink to the current build in `/opt/remoteos-mcp-venvs/`. A real venv directory there (camera-box's) is adopted.
- The key lives ONLY in 0600 `/etc/remoteos-mcp/config.json` and the 0600 EnvironmentFile `/etc/remoteos-mcp/remoteos-mcp.env` (`REMOTEOS_AUTH_KEY=`). Never in the unit, an argv or the installer output: read it on the box with `sudo cat /etc/remoteos-mcp/config.json`.
- The key is kept on a re-run: explicit `REMOTEOS_MCP_AUTH_KEY`, else the running unit's (a legacy `--auth-key`, else its EnvironmentFile), else config.json, else new.
- An unchanged source skips pip and restarts nothing. A new one is built side by side, then swapped in; a failed build changes nothing.
- The install must prove itself (`/health` = the new version, an authenticated `initialize` 200, an unauthenticated request 401), or it rolls back to the previous PROVEN unit, key files and venv. The key is sent only when the unit's own process is the only listener on :8092 (`ss`, from iproute2, which a normal run requires).
- PROVEN = what ran before the run: a unit, no pending switch, the service answering `/health` with the version its own ExecStart python has, and refusing a request without the key. A failed run on a proven box with nothing switched leaves it running as it was (`Nothing was switched`).
- With nothing proven to go back to (a fresh box, a pending switch, files the running process never ran, an agent with authentication off), a failed proof stops and disables the service; its files and the pending marker stay for a re-run. An unproven agent never stays up.
- After `REMOTEOS_MCP_POLICY=enable-only` the switch is pending (`/etc/remoteos-mcp/.switch-pending`): the next normal run restarts and proves it. An enable-only run that changed nothing on a proven box leaves it proven.
- A desktop unit gets the X11 session's `DISPLAY` (or keeps the existing unit's); a Wayland session gets none.

**Migration from the old system-python layout:** the first run builds the venv, switches the unit and proves it; only then does it remove the old copy from the system python (`pip uninstall --break-system-packages remoteos-mcp fastmcp fastmcp-slim`). If the proof fails, the old unit and the old copy stay and the box runs as before. The box keeps its key (#51), which the old unit showed to every local account (`systemctl cat`, `ps`) and the old installers printed in their summaries (so it may sit in setup logs and ssh transcripts): on a box with other local accounts, or whose earlier install output was logged, rotate it with `REMOTEOS_MCP_AUTH_KEY` and update the clients. The same holds for a migrated mac (the old plist's ProgramArguments) and Windows box (the old `.bat`'s `--auth-key`). On an enable-only cam the old copy goes at the first proven no-op run after it rebooted into the venv.

Verify after a run (from dev1 and on the box):

- `/health` reports the new version; the client config still gets 200 (the key did not change);
- `systemctl cat remoteos-mcp` shows no key and no `--auth-key`;
- `pgrep -af remoteos` shows no `--auth-key`;
- `readlink /opt/remoteos-mcp-venv` points into `/opt/remoteos-mcp-venvs/`.

**Recovery:**

- `[X] … nothing was changed on this box`: nothing changed. Fix the cause (network, the ref) and rerun.
- `[X] The new install did not prove itself … Rolled back: the previous install runs again and proved itself`: the previous install runs again. Read `journalctl -u remoteos-mcp -n 50` from the failed start.
- `[X] The previous install does not prove itself again … stopped and disabled`: the previous unit, key files and venv are back, but that install does not come up proven either (not running, another version, or no authentication, e.g. an EnvironmentFile with an empty key). Nothing runs. Read the journal, fix the cause, rerun.
- `Nothing was switched: the proven install that was running is left as it ran`: the run changed nothing and the box runs as before; the proof's failure line says what failed.
- `[X] Nothing proven to go back to: remoteos-mcp is stopped and disabled`: a fresh box, a pending switch the unit never started, or a running install that did not prove itself before the run (another version, no answer, no authentication). The agent is DISABLED: it does not come back at boot. Fix the cause and rerun.
- enable-only failures never touch the running service: `Rolled back to the previous unit, key files and venv (enable-only: …)`, or on a fresh box `Nothing to go back to: remoteos-mcp is disabled`.
- `An earlier switch has run since (the unit started again)`: an enable-only switch that a reboot (or a restart) has started since; the run checks it as it runs.
- The key-out-of-the-environment guarantee needs a source at or after the #12 merge: an older `REMOTEOS_MCP_REF` (e.g. camera-box's pin before its bump) runs a server that keeps `REMOTEOS_AUTH_KEY` in its environment, so Shell children inherit it.
- `another process holds :8092, not the unit's`: something else listens on the port (`sudo ss -ltnp 'sport = :8092'`). The key was not sent. Stop that process, then rerun.
- `this run is inside remoteos-mcp.service (the agent's own Shell)`: nothing was changed; run the one-liner over SSH.
- Every restart run on an unchanged box ends `an authenticated initialize answered 401` + `Nothing was switched`: the running process holds another key than the key files (they were changed under it). `sudo systemctl restart remoteos-mcp` once, then rerun to prove it.

**Gotcha — camera-box cams run a read-only rootfs by design** (camera-box's `scripts/lib/ro-root.sh`; its provisioning opens an rw window and runs this installer inside it). The installer refuses a read-only root and changes nothing: update a cam through camera-box's provisioning, never by editing its `/etc/fstab`. On any other box, `mount -o remount,rw /` for the run. Both Linux and macOS installers also verify after restart that the `/health` endpoint reports the version just installed — a stale pre-existing install is caught and reported as failure.

The pip gotchas below are history of the system-python layout. They still apply to macOS (and Windows) and explain the old Linux boxes; a Linux install now runs pip only in a fresh venv.

**Gotcha — pip's `--ignore-installed` does not force a re-clone from a git URL** (discovered 2026-09-09 deploying dev13 to imag.lan/cam2.lan). When an older version of remoteos-mcp is already installed, pip with `--ignore-installed` may reuse the existing package instead of fetching the latest from the git URL. All three installers now use explicit pre-uninstall + clean install (no `--force-reinstall`). On some boxes (cam2), even `--force-reinstall git+https://...` can fail due to deeper pip git caching — the workaround is a fresh `git clone` + install from the local path. The `@main` branch is now pinned in the git URL for Linux/macOS installers.

**Gotcha — on Windows, pip must not run while the old server is alive** (discovered 2026-09-10 deploying dev14 to resolume/iem). `pip install --force-reinstall` uninstalls and reinstalls all dependencies; on Windows, if the old python process (running `python -m remoteos`) still has the package files open, pip cannot delete them — fastmcp's `__init__.py` gets silently dropped, leaving a namespace package that cannot import `FastMCP`. Linux does not have this problem (POSIX unlink works on open files). The fix: the installer now stops the scheduled task (`schtasks /End`) and kills all running remoteos python processes BEFORE pip install, and verifies the import (`import remoteos, fastmcp`) after install — a broken import fails the installer hard (`exit 1`), never repairs.

**Gotcha — pip uninstall needs `--break-system-packages` on PEP 668 systems** (discovered 2026-09-10 deploying dev19 to cam1.lan Ubuntu 24.04). PEP 668 enforces `externally-managed-environment` on BOTH `pip install` AND `pip uninstall`. The install command already had `--break-system-packages`, but the pre-uninstall step did not. On cam1 the uninstall failed silently (error swallowed by `|| true`), leaving stale packages that caused pip install to produce dev12 instead of dev19. The fix: add `--break-system-packages` to the `pip uninstall` command in install-linux.sh and install.sh (both).

**Gotcha — pip --force-reinstall with the fastmcp/fastmcp-slim split drops __init__.py** (discovered 2026-09-10 deploying dev18 to win-stream-snv, wheel RECORD evidence). The fastmcp PyPI package split at 3.0.0: `fastmcp` became a zero-file meta-package, `fastmcp-slim` owns all code including `fastmcp/__init__.py`. A box running pre-split monolithic fastmcp 2.x has a RECORD that claims `fastmcp/__init__.py`. During `pip install --force-reinstall`, pip can install fastmcp-slim (writes __init__.py) then uninstall old monolithic fastmcp (deletes __init__.py from its old RECORD). The fix (all three installers): explicitly `pip uninstall -y remoteos-mcp fastmcp fastmcp-slim` BEFORE pip install — removes all stale RECORDs, then a clean `pip install` (no `--force-reinstall`) writes everything fresh.

**Gotcha — some Windows SSH servers reject piped PowerShell one-liners** (iem.lan, 2026-09-10). The `irm ... | iex` one-liner fails with `exec request failed on channel 0` when run via SSH exec channel on some boxes. Workaround: download the script first, then run it with `-File`:
```powershell
ssh user@box 'powershell -NoProfile -Command "Invoke-WebRequest -Uri https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 -OutFile C:\Users\user\install-remoteos.ps1"'
ssh user@box 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\user\install-remoteos.ps1'
```

**Gotcha — server won't start from SSH session 0** (Windows, all boxes). A server started from an SSH session (session 0) dies when SSH disconnects. Since 0.7.0.dev35 (#57) the installer handles it: in session 0 it starts the server only through `schtasks /Run /TN RemoteOSMCP` (the desktop user's interactive session) and checks the listener's session. Before dev35, trigger the task by hand after an SSH deploy, in a NEW SSH call (a `/Run` in the same call as the old installer did not take, most likely because the installer's own session-0 server still held the port, so the task's start script exited as a duplicate). All three installers have a post-install health self-check (~30s poll) that verifies the server actually started with the correct version.

## Repository structure

- `install.ps1` / `uninstall.ps1` — Windows installer scripts
- `install.sh` / `uninstall.sh` — macOS installer scripts
- `install-linux.sh` / `uninstall-linux.sh` — Linux installer scripts
- `src/remoteos/` — the Python package (forked from dddabtc/winremote-mcp, MIT license)
- `pyproject.toml` — package metadata

If the installer doesn't handle something, **fix the installer** — don't work around it.
