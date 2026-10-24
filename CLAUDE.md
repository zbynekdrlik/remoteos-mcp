# remoteos-mcp — Project Instructions

## Project overview

Python MCP server for remote OS control across Windows, macOS, and Linux machines.
Package: `remoteos-mcp` (forked from dddabtc/winremote-mcp, MIT). GitHub: zbynekdrlik/remoteos-mcp.

## Playbook router

| Topic | Where to look |
|---|---|
| Install / upgrade / redeploy on any machine | `/remoteos-install` skill |
| Installer code (Linux venv layout + swap/rollback, the key never in a unit/argv/output, key precedence, macOS preflight/rollback, the fake-box tests) | `.claude/rules/installers.md` (auto-loads on its `paths:`) |
| `.mcp.json` setup, restore after clone, add new machine | `/remoteos-mcp-config` skill |
| Linux X11 desktop provider (session detection, xdotool/scrot backends, deploy/test) | `.claude/skills/linux-desktop` |
| MCP gateway on dev1 (`remoteos-gateway`, per-consumer tokens, streaming proxy, rollout; #51 key store, `projects.toml`, `sync-configs`, `check` watchdog) | `.claude/rules/gateway.md` (auto-loads on its `paths:`) + README "MCP gateway (dev1)" |
| Screen coordinates: Snapshot frame, scale/origin, image→screen input mapping, Windows DPI order, Retina | `.claude/rules/coordinates.md` (auto-loads on its `paths:`) + README "Screen coordinates" |
| Mouse wheel / Scroll: clicks + direction, Windows SendInput, Quartz wheel2 sign, xdotool 4-7 | `.claude/rules/scroll.md` (auto-loads on its `paths:`) + README "Scroll units" |
| Typing text / Type: Unicode keystrokes (SendInput `KEYEVENTF_UNICODE`, Quartz Unicode string), delivered count, `method="paste"` | `.claude/rules/typing.md` (auto-loads on its `paths:`) + README "Typing text (Unicode)" |
| Server start flags (`uvicorn_config`, payload-free `--debug`, stdio stdout), the announced machine identity (`identity.py`: `initialize` instructions + `/health` os/hostname), Shell `cwd`, the Linux Shell session env (`shell_env`, `systemctl --user`), FileUpload, task states | `.claude/rules/server-startup.md` (auto-loads on its `paths:`) |
| Tool failures as MCP errors (`ToolError`, `@reports_errors`, `[task:<id>]`), desktop `diagnose()` preflight (locked / secure desktop / session 0 / macOS permissions) | `.claude/rules/tool-errors.md` (auto-loads on its `paths:`) + README "Tool errors and desktop diagnoses" |
| Starting a child and decoding its output: the PowerShell console trampoline (`powershell_argv`), `run_child` + the timeout tree kill, `decode_output` (per-line OEM fallback), its own console, Ping's OEM read, the Windows system language, the real-PowerShell `tests_windows` job | `.claude/rules/child-output.md` (auto-loads on its `paths:`) + README "Console output on Windows" |
| Which MCP server belongs to which project | memory: `reference_mcp_project_mapping.md` |
| SSH credentials for managed machines | memory: `reference_ssh_credentials.md` |

## Always-apply rules

- **Never run ad-hoc `pip install` to upgrade.** Always use the OS-appropriate one-liner installer.
- **Never commit `.mcp.json`** — it contains bearer tokens. Symlink from `~/.claude/mcp-configs/`.
- **Deploy policy:** `python3 airuleset.py push` is not used here; after source changes push to git, then run the installer on each target machine via SSH.
- **Local tests run in a dedicated venv**, never the user site-packages (on dev1 its fastmcp is broken and `.venv-test` is stale): `python3 -m venv <scratch>/venv && <scratch>/venv/bin/pip install -c constraints.txt -e '.[test,dev]'`, then `<scratch>/venv/bin/pytest` and `ruff check src/ tests/`.
