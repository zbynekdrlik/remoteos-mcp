# remoteos-mcp — Project Instructions

## Project overview

Python MCP server for remote OS control across Windows, macOS, and Linux machines.
Package: `remoteos-mcp` (forked from dddabtc/winremote-mcp, MIT). GitHub: zbynekdrlik/remoteos-mcp.

## Playbook router

| Topic | Where to look |
|---|---|
| Install / upgrade / redeploy on any machine | `/remoteos-install` skill |
| Installer code (preflight, rollback, `--ignore-installed` on Debian, fake-pip tests) | `.claude/rules/installers.md` (auto-loads on its `paths:`) |
| `.mcp.json` setup, restore after clone, add new machine | `/remoteos-mcp-config` skill |
| Linux X11 desktop provider (session detection, xdotool/scrot backends, deploy/test) | `.claude/skills/linux-desktop` |
| MCP gateway on dev1 (`remoteos-gateway`, per-consumer tokens, streaming proxy, rollout) | `.claude/rules/gateway.md` (auto-loads on its `paths:`) + README "MCP gateway (dev1)" |
| Screen coordinates: Snapshot frame, scale/origin, image→screen input mapping, Windows DPI order, Retina | `.claude/rules/coordinates.md` (auto-loads on its `paths:`) + README "Screen coordinates" |
| Mouse wheel / Scroll: clicks + direction, Windows SendInput, Quartz wheel2 sign, xdotool 4-7 | `.claude/rules/scroll.md` (auto-loads on its `paths:`) + README "Scroll units" |
| Which MCP server belongs to which project | memory: `reference_mcp_project_mapping.md` |
| SSH credentials for managed machines | memory: `reference_ssh_credentials.md` |

## Always-apply rules

- **Never run ad-hoc `pip install` to upgrade.** Always use the OS-appropriate one-liner installer.
- **Never commit `.mcp.json`** — it contains bearer tokens. Symlink from `~/.claude/mcp-configs/`.
- **Deploy policy:** `python3 airuleset.py push` is not used here; after source changes push to git, then run the installer on each target machine via SSH.
