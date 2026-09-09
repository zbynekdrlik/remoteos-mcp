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

## Repository structure

- `install.ps1` / `uninstall.ps1` — Windows installer scripts
- `install.sh` / `uninstall.sh` — macOS installer scripts
- `install-linux.sh` / `uninstall-linux.sh` — Linux installer scripts
- `src/remoteos/` — the Python package (forked from dddabtc/winremote-mcp, MIT license)
- `pyproject.toml` — package metadata

If the installer doesn't handle something, **fix the installer** — don't work around it.
