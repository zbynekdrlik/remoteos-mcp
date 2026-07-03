# remoteos-mcp — Project Instructions

## Project overview

Python MCP server for remote OS control across Windows, macOS, and Linux machines.
Package: `remoteos-mcp` (forked from dddabtc/winremote-mcp, MIT). GitHub: zbynekdrlik/remoteos-mcp.

## Playbook router

| Topic | Where to look |
|---|---|
| Install / upgrade / redeploy on any machine | `/remoteos-install` skill |
| `.mcp.json` setup, restore after clone, add new machine | `/remoteos-mcp-config` skill |
| Which MCP server belongs to which project | memory: `reference_mcp_project_mapping.md` |
| SSH credentials for managed machines | memory: `reference_ssh_credentials.md` |

## Always-apply rules

- **Never run ad-hoc `pip install` to upgrade.** Always use the OS-appropriate one-liner installer.
- **Never commit `.mcp.json`** — it contains bearer tokens. Symlink from `~/.claude/mcp-configs/`.
- **Deploy policy:** `python3 airuleset.py push` is not used here; after source changes push to git, then run the installer on each target machine via SSH.
