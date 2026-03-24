# WinRemote Setup

## Installing / Upgrading on Windows machines

The ONLY correct way to install or upgrade winremote-mcp on a Windows machine is via the one-liner:

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/winremote-setup/master/install.ps1 | iex
```

Run this via SSH or MCP Shell. It handles everything:

- Installs/upgrades pip package from this repo
- Preserves existing auth key on reinstall
- Sets up VBS hidden launcher (no CMD window)
- Registers scheduled task with auto-restart
- Configures firewall

DO NOT run ad-hoc `pip install` commands to upgrade. Always use the installer.

## Repository structure

This repo contains both the installer scripts and the winremote-mcp Python package source:

- `install.ps1` / `uninstall.ps1` — installer scripts
- `src/winremote/` — the Python package (forked from dddabtc/winremote-mcp, MIT license)
- `pyproject.toml` — package metadata

After pushing changes, run the installer on each target machine to deploy.

## Windows machines

Network profiles must be set to Private (not Public) for the firewall rule to work.
If a machine's network is Public, fix it:

```powershell
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
```
