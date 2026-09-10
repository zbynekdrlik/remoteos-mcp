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
After pushing source changes, deploy by running the appropriate one-liner on each target via SSH or MCP Shell.

## Windows

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 | iex
```

Handles: pip install from repo, preserves existing auth key on reinstall, VBS hidden launcher (no CMD window), scheduled task with auto-restart, firewall rule.

### Windows network profile fix (if firewall rule doesn't work)

Network must be **Private** (not Public) for the firewall rule to apply:

```powershell
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
```

## macOS

```bash
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.sh | bash
```

Service runs as a LaunchAgent (`com.remoteos-mcp`).

## Linux

```bash
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install-linux.sh | sudo bash
```

Requires `sudo`. Uses systemd for service management. Supports Ubuntu 24.04+ and other systemd-based distributions.

**Gotcha — cam boxes can boot with a read-only rootfs** (fstab `ro`). The installer now refuses to run until remounted rw + fstab fixed (`mount -o remount,rw /` then edit `/etc/fstab`). Both Linux and macOS installers also verify after restart that the `/health` endpoint reports the version just installed — a stale pre-existing install is caught and reported as failure.

**Gotcha — pip's `--ignore-installed` does not force a re-clone from a git URL** (discovered 2026-09-09 deploying dev13 to imag.lan/cam2.lan). When an older version of remoteos-mcp is already installed, pip with `--ignore-installed` may reuse the existing package instead of fetching the latest from the git URL. All three installers now use `--force-reinstall` instead, which uninstalls the old version and installs fresh from git. On some boxes (cam2), even `--force-reinstall git+https://...` can fail due to deeper pip git caching — the workaround is a fresh `git clone` + install from the local path. The `@main` branch is now pinned in the git URL for Linux/macOS installers.

**Gotcha — Windows `--force-reinstall` from archive URL corrupts fastmcp** (discovered 2026-09-10 deploying dev14 to resolume/iem). `pip install --force-reinstall https://...archive/main.zip` can delete fastmcp's `__init__.py`, leaving a namespace package that cannot import `FastMCP`. The Windows installer now verifies the fastmcp import after pip install and repairs it with a targeted `pip install --force-reinstall fastmcp==4.0.3` if broken. This does NOT affect the Linux/macOS installers (they use `git+https://` URLs).

**Gotcha — some Windows SSH servers reject piped PowerShell one-liners** (iem.lan, 2026-09-10). The `irm ... | iex` one-liner fails with `exec request failed on channel 0` when run via SSH exec channel on some boxes. Workaround: download the script first, then run it with `-File`:
```powershell
ssh user@box 'powershell -NoProfile -Command "Invoke-WebRequest -Uri https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 -OutFile C:\Users\user\install-remoteos.ps1"'
ssh user@box 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\user\install-remoteos.ps1'
```

**Gotcha — server won't start from SSH session 0** (Windows, all boxes). The VBS hidden launcher uses `WScript.Shell.Run` which needs a desktop session. After deploying over SSH, trigger the scheduled task: `schtasks /Run /TN RemoteOSMCP` (it runs in the desktop user's interactive session). All three installers now have a post-install health self-check (~30s poll) that verifies the server actually started with the correct version.

## Repository structure

- `install.ps1` / `uninstall.ps1` — Windows installer scripts
- `install.sh` / `uninstall.sh` — macOS installer scripts
- `install-linux.sh` / `uninstall-linux.sh` — Linux installer scripts
- `src/remoteos/` — the Python package (forked from dddabtc/winremote-mcp, MIT license)
- `pyproject.toml` — package metadata

If the installer doesn't handle something, **fix the installer** — don't work around it.
