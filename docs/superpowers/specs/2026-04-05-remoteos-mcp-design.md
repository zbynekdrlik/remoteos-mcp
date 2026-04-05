# remoteos-mcp: Cross-Platform Remote OS MCP Server

**Date:** 2026-04-05
**Status:** Approved
**Scope:** Refactor winremote-mcp into a cross-platform (Windows + macOS) remote OS control MCP server

## Summary

Rename and refactor the existing `winremote-mcp` package to `remoteos-mcp`, adding macOS support via platform-specific backend modules. The tool API (39 MCP tools) stays identical — backends differ per platform. The GitHub repo renames from `winremote-setup` to `remoteos-mcp`.

## Motivation

The user manages multiple Windows machines via winremote-mcp and now has a macOS machine (ProPresenter) that needs the same remote control capabilities. Rather than maintaining a separate project, the existing codebase is refactored to support both platforms.

Usage analysis of devbridge sessions (3,427 MCP calls) shows the priority:
- Shell: 85% of all calls
- File operations: ~5%
- Process management: ~5%
- Desktop GUI (screenshots/clicks): ~3%
- Network tools: ~1.5%
- Everything else: <1%

## Target Machines

### Existing Windows machines (unchanged)
- win-iem-snv (host.example.local / 192.0.2.30)
- win-stream-snv (192.0.2.30)
- win-mbc-snv (host.example.local)
- win-print-client (host.example.local)
- win-print-server (host.example.local)
- win-host-server (192.0.2.20)
- win-host-a (192.0.2.10)
- win-host-b (192.0.2.10)
- win-host-c (192.0.2.20)

### New macOS machine
- mac-host-a (192.0.2.30), macOS 15.6 Sequoia, Apple Silicon (arm64), Python 3.9.6

## Architecture

### Approach: Platform backend modules

Each Windows-specific module gets a macOS counterpart behind a platform dispatcher. The main tool definitions in `__main__.py` remain platform-agnostic — they call into the platform layer, which routes to the correct backend.

### Package Structure

```
src/remoteos/
├── __main__.py              # 39 MCP tool definitions (platform-agnostic)
├── config.py                # Config loading (unchanged)
├── auth.py                  # Auth middleware (unchanged)
├── security.py              # IP allowlist middleware (unchanged)
├── tiers.py                 # Security tiers (unchanged)
├── taskmanager.py           # Concurrency control (unchanged)
├── network.py               # Ping/port/connections (minor: -n vs -c flag)
├── process_mgr.py           # psutil-based (cross-platform, unchanged)
├── recording.py             # PIL-based screen recording (cross-platform)
├── ocr.py                   # Platform dispatcher for OCR
├── platform/
│   ├── __init__.py          # Auto-detect, export get_desktop(), get_services(), get_system()
│   ├── win/
│   │   ├── desktop.py       # win32gui, pywin32 — screenshots, windows, clipboard
│   │   ├── services.py      # PowerShell Get-Service, schtasks
│   │   └── registry.py      # winreg
│   └── mac/
│       ├── desktop.py       # Quartz screenshots, pyautogui input, osascript windows, pbcopy/pbpaste
│       ├── services.py      # launchctl, launchd plists
│       └── system.py        # defaults command (plist read/write, replaces registry)
```

### Installer Files

```
install.ps1      # Windows installer (updated for new package name)
install.sh       # macOS installer (new)
uninstall.ps1    # Windows uninstaller (updated)
uninstall.sh     # macOS uninstaller (new)
```

## Platform Backends

### macOS Desktop (`platform/mac/desktop.py`)

| Capability | Implementation |
|-----------|---------------|
| Screenshots | `Quartz.CGWindowListCreateImage()` via pyobjc |
| Mouse/keyboard | `pyautogui` (already cross-platform) |
| Window list | `Quartz.CGWindowListCopyWindowInfo()` |
| Window focus/resize | `osascript` (AppleScript) |
| Clipboard | `pbcopy` / `pbpaste` subprocess |
| Lock screen | `osascript -e 'tell application "System Events" to sleep'` or `pmset displaysleepnow` |
| Notifications | `osascript -e 'display notification ...'` |
| MinimizeAll | `osascript` — hide all apps |

### macOS Services (`platform/mac/services.py`)

| Capability | Implementation |
|-----------|---------------|
| ServiceList | `launchctl list` |
| ServiceStart | `launchctl start <label>` |
| ServiceStop | `launchctl stop <label>` |
| TaskCreate | Write plist to `~/Library/LaunchAgents/` + `launchctl load` |
| TaskList | List plists in LaunchAgents directories |
| TaskDelete | `launchctl unload` + remove plist |
| EventLog | `log show --predicate '...' --last Nh` |

### macOS System (`platform/mac/system.py`)

Replaces Windows registry for reading/writing system and app preferences:

| Capability | Implementation |
|-----------|---------------|
| RegRead → defaults read | `defaults read <domain> <key>` |
| RegWrite → defaults write | `defaults write <domain> <key> <type> <value>` |

Not a 1:1 equivalent, but covers the same use case: reading and writing app/system preferences.

### Shell Tool

The Shell tool in `__main__.py` already runs subprocesses. The only change: detect platform and use `zsh` on macOS instead of `powershell`.

### Network Module

One change: ping flag `-n` (Windows) → `-c` (macOS/Linux) for count parameter.

## Tool Availability by Platform

38 of 39 tools work on both platforms. Only `ReconnectSession` is Windows-only (RDP session management) — it is not registered on macOS.

All other tools: Shell, Snapshot, Click, Type, Move, Scroll, Shortcut, App, FocusWindow, MinimizeAll, AnnotatedSnapshot, FileRead, FileWrite, FileList, FileSearch, FileUpload, FileDownload, ListProcesses, KillProcess, GetSystemInfo, Ping, PortCheck, NetConnections, GetClipboard, SetClipboard, ScreenRecord, OCR, Scrape, Wait, ServiceList, ServiceStart, ServiceStop, TaskCreate, TaskList, TaskDelete, EventLog, RegRead, RegWrite, LockScreen, Notification — all supported on macOS.

## macOS Installer (`install.sh`)

One-liner: `curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/install.sh | bash`

Steps:
1. **Python check** — needs 3.10+. macOS ships 3.9.6. If too old: install via `brew install python@3.12` or exit with instructions.
2. **pip install** — `pip install git+https://github.com/zbynekdrlik/remoteos-mcp.git`
3. **Auth key** — generate 32-char random key, or reuse from `~/.remoteos-mcp/config.json`
4. **Config** — write `~/.remoteos-mcp/config.json`
5. **Auto-start** — create `~/Library/LaunchAgents/com.remoteos-mcp.plist` (launchd agent: run at login, auto-restart on crash)
6. **Firewall** — macOS doesn't need explicit port opening on LAN. Note: if macOS firewall is enabled, the user may need to allow the Python binary.
7. **Start** — launch server immediately
8. **Output** — print `claude mcp add` command

## macOS Uninstaller (`uninstall.sh`)

1. Kill running server process
2. `launchctl unload` and remove plist
3. Remove `~/.remoteos-mcp/`
4. Optional `pip uninstall remoteos-mcp`

## Rename & Migration

### Renames
| Old | New |
|-----|-----|
| GitHub repo: `winremote-setup` | `remoteos-mcp` |
| Python package: `winremote` | `remoteos` |
| CLI entry point: `winremote-mcp` | `remoteos-mcp` |
| Config dir: `~/.winremote-mcp/` | `~/.remoteos-mcp/` |
| Source: `src/winremote/` | `src/remoteos/` |

### MCP config naming convention
- Windows servers: `win-*` prefix (e.g., `win-host-a`)
- macOS servers: `mac-*` prefix (e.g., `mac-host-a`)

### Backward compatibility
None. Clean break. The installer handles the transition — uninstalls old `winremote-mcp`, installs `remoteos-mcp`.

## Dependencies

### All platforms
- fastmcp
- pyautogui
- psutil
- Pillow
- click
- python-dotenv
- thefuzz
- markdownify

### Windows only
- pywin32

### macOS only
- pyobjc-framework-Quartz
- pyobjc-framework-ApplicationServices

## Rollout Order

1. Refactor codebase (rename + add platform backends)
2. Test on Mac ProPresenter (192.0.2.30) — install.sh, verify tools
3. Re-run install.ps1 on one Windows machine to confirm nothing broke
4. Roll out to remaining Windows machines
5. Update all `.mcp.json` files across projects (package name in URLs changes)

## Test Target

First macOS deployment: mac-host-a at 192.0.2.30 (SSH: newlevel@192.0.2.30)
