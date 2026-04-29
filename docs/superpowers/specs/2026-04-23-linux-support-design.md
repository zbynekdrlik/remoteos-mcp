# Linux Support for remoteos-mcp

**Date:** 2026-04-23
**Status:** Approved

## Context

remoteos-mcp currently supports Windows and macOS. This design extends it to headless Linux servers (no GUI). Target: Ubuntu 24.04+ with systemd.

## Architecture

Add `platform/linux/` alongside existing `platform/win/` and `platform/mac/`. Extend `platform/__init__.py` to detect `sys.platform == "linux"` and dispatch accordingly. No changes to existing Windows/macOS code.

## Platform Module: `platform/linux/`

| File | Responsibility |
|------|---------------|
| `__init__.py` | Package init |
| `desktop.py` | Stub — all methods raise "not supported on headless Linux" |
| `services.py` | `systemctl` for services, systemd timers for scheduled tasks, `journalctl` for event logs |
| `system.py` | Linux-specific system info (distro, kernel, uptime via `/proc`) |

## Tool Availability

### Enabled on Linux (24 tools)

- **Shell** — `/bin/bash -c`
- **File operations** — FileRead, FileWrite, FileList, FileSearch, FileDownload, FileUpload
- **Process management** — ListProcesses, KillProcess
- **Services** — ServiceList, ServiceStart, ServiceStop (via `systemctl`)
- **Scheduled tasks** — TaskCreate, TaskDelete, TaskList, GetTaskStatus, GetRunningTasks, CancelTask (via systemd timers)
- **Logs** — EventLog (via `journalctl`)
- **System** — GetSystemInfo
- **Network** — Ping, PortCheck, NetConnections
- **Utility** — Wait

### Disabled on Linux (23 tools)

- **Desktop/UI** — Snapshot, AnnotatedSnapshot, OCR, ScreenRecord, Click, Type, Move, Scroll, Shortcut, FocusWindow, MinimizeAll, App, Scrape
- **Clipboard** — GetClipboard, SetClipboard
- **Registry** — RegRead, RegWrite
- **Windows-specific** — ReconnectSession, LockScreen, Notification

Disabled tools are filtered out at startup via the existing `filter_tools()` mechanism using a new `LINUX_EXCLUDED_TOOLS` set in `tiers.py`.

## Shell Implementation

```python
if is_windows():
    shell_cmd = ["powershell", "-NoProfile", "-Command", command]
elif is_macos():
    shell_cmd = ["/bin/zsh", "-c", command]
else:
    shell_cmd = ["/bin/bash", "-c", command]
```

## Services Implementation (`systemctl`)

| Tool | Linux command |
|------|--------------|
| ServiceList | `systemctl list-units --type=service --no-pager` |
| ServiceStart | `systemctl start <name>` |
| ServiceStop | `systemctl stop <name>` |

## Scheduled Tasks (systemd timers)

| Tool | Implementation |
|------|---------------|
| TaskCreate | Write `.service` + `.timer` unit pair to `/etc/systemd/system/`, then `systemctl enable --now` |
| TaskDelete | `systemctl disable --now`, remove unit files |
| TaskList | `systemctl list-timers --no-pager` |

## EventLog

`journalctl` with filters: `--unit`, `--since`, `--priority`, `--no-pager`, JSON output via `-o json`.

## Installer: `install-linux.sh`

Separate from `install.sh` (macOS). Requires `sudo`.

1. Check Python 3.10+ (install `python3-pip` via apt if missing)
2. `pip install` from GitHub
3. Generate 32-char auth key
4. Write config to `/etc/remoteos-mcp/config.toml`
5. Create systemd unit at `/etc/systemd/system/remoteos-mcp.service`
6. `systemctl daemon-reload && systemctl enable --now remoteos-mcp`
7. Add `ufw` firewall rule for port 8092 (if ufw is active)
8. Print LAN IP for MCP config

Service runs as the user who ran the installer (configurable).

## Dependencies

No new Python dependencies. `psutil` already handles Linux.

Conditional pyautogui to avoid pulling X11 deps on headless Linux:

```toml
pyautogui>=0.9.54; sys_platform != 'linux'
```

## Platform Detection

Add to `platform/__init__.py`:

```python
def is_linux() -> bool:
    return _PLATFORM == "linux"
```

Update `get_desktop()` and `get_services()` to handle `"linux"`.

## Files Changed

| Area | Files |
|------|-------|
| New | `src/remoteos/platform/linux/__init__.py`, `desktop.py`, `services.py`, `system.py` |
| New | `install-linux.sh` |
| Modified | `src/remoteos/platform/__init__.py` — add Linux detection |
| Modified | `src/remoteos/__main__.py` — Shell bash fallback, Linux tool exclusions at startup |
| Modified | `src/remoteos/tiers.py` — `LINUX_EXCLUDED_TOOLS` set |
| Modified | `pyproject.toml` — conditional pyautogui, version bump |

## MCP Description

Update FastMCP instructions string to include "Linux" alongside "Windows and macOS".
