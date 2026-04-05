# RemoteOS Setup

## Installing / Upgrading on Windows machines

The ONLY correct way to install or upgrade remoteos-mcp on a Windows machine is via the one-liner:

```powershell
irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-setup/master/install.ps1 | iex
```

Run this via SSH or MCP Shell. It handles everything:

- Installs/upgrades pip package from this repo
- Preserves existing auth key on reinstall
- Sets up VBS hidden launcher (no CMD window)
- Registers scheduled task with auto-restart
- Configures firewall

DO NOT run ad-hoc `pip install` commands to upgrade. Always use the installer.

## Installing / Upgrading on macOS machines

```bash
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-setup/master/install.sh | bash
```

## Repository structure

This repo contains both the installer scripts and the remoteos-mcp Python package source:

- `install.ps1` / `uninstall.ps1` — Windows installer scripts
- `src/remoteos/` — the Python package (forked from dddabtc/winremote-mcp, MIT license)
- `pyproject.toml` — package metadata

After pushing changes, run the installer on each target machine to deploy.

## Windows machines

Network profiles must be set to Private (not Public) for the firewall rule to work.
If a machine's network is Public, fix it:

```powershell
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
```
