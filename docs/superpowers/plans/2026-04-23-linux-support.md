# Linux Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add headless Linux support to remoteos-mcp — systemd services, bash shell, journalctl logs, systemd timers for scheduled tasks, and a Linux installer.

**Architecture:** Add `src/remoteos/platform/linux/` module with the same interface as `mac/` and `win/`. Extend `platform/__init__.py` to dispatch to Linux. Exclude desktop/GUI tools on Linux at startup. Add `install-linux.sh` for systemd service setup.

**Tech Stack:** Python 3.10+, psutil, systemd (systemctl, journalctl, systemd-run), bash, ufw

---

### Task 1: Platform detection — add Linux to `platform/__init__.py`

**Files:**
- Modify: `src/remoteos/platform/__init__.py`

- [ ] **Step 1: Add `is_linux()` function and update dispatchers**

Edit `src/remoteos/platform/__init__.py` to become:

```python
"""Platform auto-detection and backend dispatch."""

from __future__ import annotations

import sys
from types import ModuleType

_PLATFORM = sys.platform


def get_desktop() -> ModuleType:
    if _PLATFORM == "darwin":
        from remoteos.platform.mac import desktop
        return desktop
    if _PLATFORM == "linux":
        from remoteos.platform.linux import desktop
        return desktop
    from remoteos.platform.win import desktop
    return desktop


def get_services() -> ModuleType:
    if _PLATFORM == "darwin":
        from remoteos.platform.mac import services
        return services
    if _PLATFORM == "linux":
        from remoteos.platform.linux import services
        return services
    from remoteos.platform.win import services
    return services


def get_system() -> ModuleType:
    if _PLATFORM == "darwin":
        from remoteos.platform.mac import system
        return system
    if _PLATFORM == "linux":
        from remoteos.platform.linux import system
        return system
    from remoteos.platform.win import registry
    return registry


def is_windows() -> bool:
    return _PLATFORM == "win32"


def is_macos() -> bool:
    return _PLATFORM == "darwin"


def is_linux() -> bool:
    return _PLATFORM == "linux"
```

- [ ] **Step 2: Commit**

```bash
git add src/remoteos/platform/__init__.py
git commit -m "Add Linux platform detection to dispatcher"
```

---

### Task 2: Linux desktop stub — `platform/linux/desktop.py`

**Files:**
- Create: `src/remoteos/platform/linux/__init__.py`
- Create: `src/remoteos/platform/linux/desktop.py`

- [ ] **Step 1: Create `__init__.py`**

```python
# src/remoteos/platform/linux/__init__.py
```

Empty file — just marks the directory as a package.

- [ ] **Step 2: Create `desktop.py` with stubs**

All functions that `__main__.py` calls on `get_desktop()` must exist but raise an error. Check what the mac desktop module exports and mirror that interface:

```python
"""Linux desktop stub — headless servers have no display."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

HAS_WIN32 = False  # compatibility flag

_NO_DISPLAY = "Not available on headless Linux (no display)"


@dataclass
class WindowInfo:
    handle: int
    title: str
    rect: tuple[int, int, int, int]
    visible: bool
    pid: int = 0

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def enumerate_windows() -> list[WindowInfo]:
    return []


def get_interactive_elements() -> list[dict]:
    return []


def take_screenshot(quality: int = 65, max_width: int = 1280, monitor: int = 0) -> str:
    raise RuntimeError(_NO_DISPLAY)


def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> str:
    return _NO_DISPLAY


def minimize_all() -> str:
    return _NO_DISPLAY


def launch_app(name: str, args: str = "") -> str:
    return _NO_DISPLAY


def resize_window(handle: int, width: int, height: int) -> str:
    return _NO_DISPLAY


def get_clipboard() -> str:
    return _NO_DISPLAY


def set_clipboard(text: str) -> str:
    return _NO_DISPLAY


def lock_screen() -> str:
    return _NO_DISPLAY


def show_notification(title: str, message: str) -> str:
    return _NO_DISPLAY
```

- [ ] **Step 3: Commit**

```bash
git add src/remoteos/platform/linux/__init__.py src/remoteos/platform/linux/desktop.py
git commit -m "Add Linux desktop stub for headless servers"
```

---

### Task 3: Linux services — `platform/linux/services.py`

**Files:**
- Create: `src/remoteos/platform/linux/services.py`

This file implements the same interface as `platform/mac/services.py`: `service_list`, `service_start`, `service_stop`, `task_list`, `task_create`, `task_delete`, `event_log`.

- [ ] **Step 1: Create `services.py`**

```python
"""Linux service and scheduled task management via systemd."""

from __future__ import annotations

import os
import subprocess


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Run a command and return stdout+stderr."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout
    if result.stderr:
        output += f"\n[STDERR] {result.stderr}"
    if result.returncode != 0:
        output += f"\n[Exit code: {result.returncode}]"
    return output.strip() or "(no output)"


# ---------------------------------------------------------------------------
# Services (systemctl)
# ---------------------------------------------------------------------------


def service_list(filter_str: str = "") -> str:
    """List systemd services."""
    try:
        raw = _run(["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"])
        if filter_str:
            lines = [line for line in raw.splitlines() if filter_str.lower() in line.lower()]
            return "\n".join(lines) if lines else f"No services matching '{filter_str}'"
        return raw
    except Exception as e:
        return f"ServiceList error: {e}"


def service_start(name: str) -> str:
    """Start a systemd service."""
    try:
        return _run(["systemctl", "start", name])
    except Exception as e:
        return f"ServiceStart error: {e}"


def service_stop(name: str) -> str:
    """Stop a systemd service."""
    try:
        return _run(["systemctl", "stop", name])
    except Exception as e:
        return f"ServiceStop error: {e}"


# ---------------------------------------------------------------------------
# Scheduled Tasks (systemd timers)
# ---------------------------------------------------------------------------

_UNIT_DIR = "/etc/systemd/system"


def task_list(filter_str: str = "") -> str:
    """List systemd timers."""
    try:
        raw = _run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
        if filter_str:
            lines = [line for line in raw.splitlines() if filter_str.lower() in line.lower()]
            return "\n".join(lines) if lines else f"No timers matching '{filter_str}'"
        return raw
    except Exception as e:
        return f"TaskList error: {e}"


def task_create(name: str, command: str, schedule: str) -> str:
    """Create a systemd timer + service unit pair.

    Args:
        name: Task name (becomes remoteos-<name>.service/.timer).
        command: Shell command to run.
        schedule: Schedule string — 'ONSTART' for boot, 'DAILY' for daily,
                  a number for interval in seconds, or a systemd calendar
                  expression (e.g. '*-*-* 03:00:00').
    """
    try:
        unit_name = f"remoteos-{name}"
        service_path = os.path.join(_UNIT_DIR, f"{unit_name}.service")
        timer_path = os.path.join(_UNIT_DIR, f"{unit_name}.timer")

        # Write service unit
        service_content = f"""[Unit]
Description=RemoteOS task: {name}

[Service]
Type=oneshot
ExecStart=/bin/bash -c {_shell_quote(command)}
"""
        with open(service_path, "w") as f:
            f.write(service_content)

        # Build timer unit
        schedule_upper = schedule.upper()
        if schedule_upper in ("ONSTART", "ONBOOT"):
            timer_spec = "OnBootSec=10"
        elif schedule_upper == "DAILY":
            timer_spec = "OnCalendar=daily"
        else:
            try:
                seconds = int(schedule)
                timer_spec = f"OnUnitActiveSec={seconds}s\nOnBootSec={seconds}s"
            except ValueError:
                # Treat as systemd calendar expression
                timer_spec = f"OnCalendar={schedule}"

        timer_content = f"""[Unit]
Description=Timer for remoteos task: {name}

[Timer]
{timer_spec}
Persistent=true

[Install]
WantedBy=timers.target
"""
        with open(timer_path, "w") as f:
            f.write(timer_content)

        _run(["systemctl", "daemon-reload"])
        result = _run(["systemctl", "enable", "--now", f"{unit_name}.timer"])
        return f"Created and started {unit_name}.timer\n{result}"
    except Exception as e:
        return f"TaskCreate error: {e}"


def task_delete(name: str) -> str:
    """Stop, disable, and remove a systemd timer + service."""
    try:
        unit_name = f"remoteos-{name}"
        service_path = os.path.join(_UNIT_DIR, f"{unit_name}.service")
        timer_path = os.path.join(_UNIT_DIR, f"{unit_name}.timer")

        _run(["systemctl", "disable", "--now", f"{unit_name}.timer"])

        removed = []
        for path in (service_path, timer_path):
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)

        _run(["systemctl", "daemon-reload"])

        if removed:
            return f"Removed: {', '.join(removed)}"
        return f"No unit files found for {unit_name}"
    except Exception as e:
        return f"TaskDelete error: {e}"


# ---------------------------------------------------------------------------
# Event Log (journalctl)
# ---------------------------------------------------------------------------


def event_log(log_name: str = "system", count: int = 20, level: str = "") -> str:
    """Read systemd journal entries.

    Args:
        log_name: Unit name to filter (or 'system' for everything).
        count: Number of entries (default 20).
        level: Filter by priority: emerg, alert, crit, err, warning, notice, info, debug.
    """
    try:
        cmd = ["journalctl", "--no-pager", "-n", str(count), "-o", "short-iso"]
        if log_name.lower() != "system":
            cmd += ["-u", log_name]
        if level:
            priority_map = {
                "critical": "2", "crit": "2",
                "error": "3", "err": "3",
                "warning": "4", "warn": "4",
                "notice": "5",
                "information": "6", "info": "6",
                "debug": "7",
                "verbose": "7",
            }
            p = priority_map.get(level.lower(), level)
            cmd += ["-p", p]
        return _run(cmd, timeout=30)
    except Exception as e:
        return f"EventLog error: {e}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shell_quote(s: str) -> str:
    """Quote a string for safe use in shell commands."""
    return "'" + s.replace("'", "'\\''") + "'"
```

- [ ] **Step 2: Verify the interface matches mac/services.py**

The mac module exports: `service_list`, `service_start`, `service_stop`, `task_list`, `task_create`, `task_delete`, `event_log`. The Linux module above exports the same functions with the same signatures. Confirmed.

- [ ] **Step 3: Commit**

```bash
git add src/remoteos/platform/linux/services.py
git commit -m "Add Linux services module: systemctl, systemd timers, journalctl"
```

---

### Task 4: Linux system info — `platform/linux/system.py`

**Files:**
- Create: `src/remoteos/platform/linux/system.py`

This mirrors `platform/mac/system.py` which provides `reg_read` and `reg_write` (the macOS version uses `defaults`). On Linux, registry doesn't apply — return clear error messages.

- [ ] **Step 1: Create `system.py`**

```python
"""Linux system module — registry operations are not applicable."""

from __future__ import annotations


def reg_read(key: str, value_name: str) -> str:
    """Registry is a Windows concept. Not available on Linux."""
    return "RegRead is not available on Linux. Use 'Shell' to read config files instead."


def reg_write(key: str, value_name: str, data: str, reg_type: str = "string") -> str:
    """Registry is a Windows concept. Not available on Linux."""
    return "RegWrite is not available on Linux. Use 'Shell' to edit config files instead."
```

- [ ] **Step 2: Commit**

```bash
git add src/remoteos/platform/linux/system.py
git commit -m "Add Linux system stub for registry operations"
```

---

### Task 5: Tool exclusions — exclude GUI tools on Linux

**Files:**
- Modify: `src/remoteos/tiers.py`
- Modify: `src/remoteos/__main__.py`

- [ ] **Step 1: Add `LINUX_EXCLUDED_TOOLS` to `tiers.py`**

Add after the `ALL_TOOLS` line (after line 58 in `src/remoteos/tiers.py`):

```python
# Tools that require a display or are platform-specific (Windows/macOS only)
LINUX_EXCLUDED_TOOLS = {
    "Snapshot",
    "AnnotatedSnapshot",
    "OCR",
    "ScreenRecord",
    "Click",
    "Type",
    "Move",
    "Scroll",
    "Shortcut",
    "FocusWindow",
    "MinimizeAll",
    "App",
    "Scrape",
    "GetClipboard",
    "SetClipboard",
    "RegRead",
    "RegWrite",
    "ReconnectSession",
    "LockScreen",
    "Notification",
}
```

- [ ] **Step 2: Update `resolve_enabled_tools` in `tiers.py` to accept a `platform_excludes` parameter**

Change the function signature and add exclusion logic at the end, before the return:

```python
def resolve_enabled_tools(
    *,
    enable_tier3: bool = False,
    disable_tier2: bool = False,
    enable_all: bool = False,
    explicit_tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
    platform_excludes: set[str] | None = None,
) -> set[str]:
    """Resolve active tools.

    Precedence: explicit tools > tier toggles.
    Platform excludes are applied last (cannot be overridden).
    """
    explicit_tools = explicit_tools or []
    exclude_tools = exclude_tools or []

    if explicit_tools:
        enabled = set(normalize_tool_names(explicit_tools))
    elif enable_all:
        enabled = set(ALL_TOOLS)
    else:
        enabled = set(TOOL_TIERS["tier1"])
        if not disable_tier2:
            enabled |= TOOL_TIERS["tier2"]
        if enable_tier3:
            enabled |= TOOL_TIERS["tier3"]

    if exclude_tools:
        enabled -= set(normalize_tool_names(exclude_tools))

    if platform_excludes:
        enabled -= platform_excludes

    return enabled
```

- [ ] **Step 3: Update `__main__.py` to pass platform excludes**

In the `cli()` function (around line 1699), change the `resolve_enabled_tools` call to:

```python
from remoteos.platform import is_linux
from remoteos.tiers import LINUX_EXCLUDED_TOOLS

# ... inside cli() ...

platform_excludes = LINUX_EXCLUDED_TOOLS if is_linux() else None

enabled_tools = resolve_enabled_tools(
    enable_tier3=enable_tier3,
    disable_tier2=disable_tier2,
    enable_all=enable_all,
    explicit_tools=selected_tools,
    exclude_tools=excluded_tools,
    platform_excludes=platform_excludes,
)
```

Note: `is_linux` is already imported on line 30. `LINUX_EXCLUDED_TOOLS` needs to be added to the import from `remoteos.tiers` on line 33.

- [ ] **Step 4: Commit**

```bash
git add src/remoteos/tiers.py src/remoteos/__main__.py
git commit -m "Exclude GUI/desktop tools on Linux at startup"
```

---

### Task 6: Shell and pyautogui — Linux-specific changes in `__main__.py`

**Files:**
- Modify: `src/remoteos/__main__.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update Shell tool to use bash on Linux**

In `src/remoteos/__main__.py`, change the Shell function (around line 513-521). Replace:

```python
        if is_windows():
            if cwd:
                command = f"cd {cwd}; {command}"
            shell_cmd = ["powershell", "-NoProfile", "-Command", command]
        else:
            if cwd:
                command = f"cd {cwd} && {command}"
            shell_cmd = ["/bin/zsh", "-c", command]
```

With:

```python
        if is_windows():
            if cwd:
                command = f"cd {cwd}; {command}"
            shell_cmd = ["powershell", "-NoProfile", "-Command", command]
        elif is_macos():
            if cwd:
                command = f"cd {cwd} && {command}"
            shell_cmd = ["/bin/zsh", "-c", command]
        else:
            if cwd:
                command = f"cd {cwd} && {command}"
            shell_cmd = ["/bin/bash", "-c", command]
```

- [ ] **Step 2: Guard pyautogui import for Linux**

At the top of `__main__.py` (lines 14, 37-38), change the pyautogui import to be conditional:

Replace:

```python
import pyautogui
```

With:

```python
from remoteos.platform import is_linux as _is_linux_check

if not _is_linux_check():
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
```

And remove the standalone lines 37-38:

```python
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05
```

**Important:** Since `is_linux` from `platform/__init__.py` is a function call checking `sys.platform`, this can be evaluated at import time safely. We use a different name (`_is_linux_check`) to avoid confusion with the later import of `is_linux` on line 30.

Actually, simpler approach — just use `sys.platform` directly at module level:

Replace lines 14, 37-38:

```python
import pyautogui
...
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05
```

With:

```python
import sys as _sys

if _sys.platform != "linux":
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
```

- [ ] **Step 3: Make pyautogui conditional in `pyproject.toml`**

In `pyproject.toml`, change:

```toml
"pyautogui>=0.9.54",
```

To:

```toml
"pyautogui>=0.9.54; sys_platform != 'linux'",
```

Also:
- Change `"Pillow>=10.0.0",` to `"Pillow>=10.0.0; sys_platform != 'linux'",` (only needed for screenshots)

- [ ] **Step 4: Update project metadata in `pyproject.toml`**

Change description:

```toml
description = "Remote OS MCP Server - control Windows, macOS, and Linux machines via MCP protocol"
```

Add Linux classifier:

```toml
"Operating System :: POSIX :: Linux",
```

Add `"linux"` to keywords:

```toml
keywords = ["mcp", "remote", "automation", "desktop", "windows", "macos", "linux"]
```

- [ ] **Step 5: Update MCP instructions string**

In `__main__.py` (around line 42-46), change:

```python
    instructions=(
        "Remote OS MCP Server. Provides desktop control, window management, "
        "shell execution, file operations, network tools, registry, services, "
        "and system management tools for Windows and macOS machines."
    ),
```

To:

```python
    instructions=(
        "Remote OS MCP Server. Provides desktop control, window management, "
        "shell execution, file operations, network tools, registry, services, "
        "and system management tools for Windows, macOS, and Linux machines."
    ),
```

- [ ] **Step 6: Update Shell docstring**

Change:

```python
    """Execute a shell command (PowerShell on Windows, zsh on macOS).
```

To:

```python
    """Execute a shell command (PowerShell on Windows, zsh on macOS, bash on Linux).
```

- [ ] **Step 7: Commit**

```bash
git add src/remoteos/__main__.py pyproject.toml
git commit -m "Add Linux shell support, guard pyautogui import, update metadata"
```

---

### Task 7: Fix `get_system_info` disk path for Linux

**Files:**
- Modify: `src/remoteos/process_mgr.py`

- [ ] **Step 1: Update disk label in `get_system_info`**

In `src/remoteos/process_mgr.py` line 92-103, the disk usage line already handles non-Windows:

```python
disk = psutil.disk_usage("C:\\") if platform.system() == "Windows" else psutil.disk_usage("/")
```

But the label on line 103 says `Disk (C:)` even on Linux. Fix:

Replace:

```python
        f"**Disk (C:):** {disk.percent}% — {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB",
```

With:

```python
        f"**Disk ({disk_label}):** {disk.percent}% — {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB",
```

And add before the `lines` list:

```python
    disk_label = "C:" if platform.system() == "Windows" else "/"
```

- [ ] **Step 2: Commit**

```bash
git add src/remoteos/process_mgr.py
git commit -m "Fix disk label in GetSystemInfo for non-Windows platforms"
```

---

### Task 8: Linux installer — `install-linux.sh`

**Files:**
- Create: `install-linux.sh`

- [ ] **Step 1: Create the installer script**

```bash
#!/usr/bin/env bash
# RemoteOS MCP - One-line installer for Linux (systemd)
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/install-linux.sh | sudo bash
set -euo pipefail

PORT=8092
CONFIG_DIR="/etc/remoteos-mcp"
CONFIG_FILE="$CONFIG_DIR/config.json"
SERVICE_NAME="remoteos-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo ""
echo "  RemoteOS MCP Installer (Linux)"
echo "  Remote control for Claude Code"
echo ""

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    echo "  [X] This installer must be run as root (use sudo)."
    exit 1
fi

# Determine the actual user (when run via sudo)
ACTUAL_USER="${SUDO_USER:-$(whoami)}"
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")

echo "  Installing for user: $ACTUAL_USER"
echo ""

# --- [1/6] Check Python ---
echo "  [1/6] Checking Python..."
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 || true)
        if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && (( ${BASH_REMATCH[1]} >= 10 )); then
            PYTHON="$cmd"
            echo "        Found $ver ($cmd)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "        Python 3.10+ not found. Installing..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv
        PYTHON="python3"
    elif command -v dnf &>/dev/null; then
        dnf install -y -q python3 python3-pip
        PYTHON="python3"
    elif command -v yum &>/dev/null; then
        yum install -y -q python3 python3-pip
        PYTHON="python3"
    else
        echo "        [X] Cannot auto-install Python. Install Python 3.10+ manually."
        exit 1
    fi
    echo "        Installed $($PYTHON --version)"
fi

# Ensure pip is available
if ! "$PYTHON" -m pip --version &>/dev/null; then
    echo "        Installing pip..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y -qq python3-pip
    else
        "$PYTHON" -m ensurepip --upgrade 2>/dev/null || true
    fi
fi

# --- [2/6] Install remoteos-mcp ---
echo "  [2/6] Installing remoteos-mcp..."
"$PYTHON" -m pip install --no-cache-dir --break-system-packages \
    "git+https://github.com/zbynekdrlik/remoteos-mcp.git" 2>&1 | tail -1 || true

# Verify installation
PKG_VER=$("$PYTHON" -m pip show remoteos-mcp 2>/dev/null | grep "^Version:" | awk '{print $2}' || true)
if [[ -n "$PKG_VER" ]]; then
    echo "        Installed v${PKG_VER}"
else
    echo "        [X] pip install failed"
    echo "        Try manually: $PYTHON -m pip install git+https://github.com/zbynekdrlik/remoteos-mcp.git"
    exit 1
fi

# --- [3/6] Configure auth key ---
echo "  [3/6] Configuring auth key..."
mkdir -p "$CONFIG_DIR"

AUTH_KEY=""
if [[ -f "$CONFIG_FILE" ]]; then
    EXISTING_KEY=$("$PYTHON" -c "import json; print(json.load(open('$CONFIG_FILE')).get('auth_key',''))" 2>/dev/null || true)
    if [[ -n "$EXISTING_KEY" ]]; then
        AUTH_KEY="$EXISTING_KEY"
        echo "        Reusing existing auth key"
    fi
fi

if [[ -z "$AUTH_KEY" ]]; then
    AUTH_KEY=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
    echo "        Generated new auth key"
fi

"$PYTHON" -c "
import json, pathlib
cfg = {'port': $PORT, 'auth_key': '$AUTH_KEY', 'host': '0.0.0.0'}
pathlib.Path('$CONFIG_FILE').write_text(json.dumps(cfg, indent=2))
"
echo "        Config: $CONFIG_FILE"

# --- [4/6] Create systemd service ---
echo "  [4/6] Setting up systemd service..."

PYTHON_PATH=$(command -v "$PYTHON")

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=RemoteOS MCP Server
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
ExecStart=$PYTHON_PATH -m remoteos --transport streamable-http --enable-all --host 0.0.0.0 --port $PORT --auth-key $AUTH_KEY
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
echo "        Service started: $SERVICE_NAME"

# --- [5/6] Firewall ---
echo "  [5/6] Configuring firewall..."
if command -v ufw &>/dev/null; then
    if ufw status | grep -q "Status: active"; then
        ufw allow "$PORT/tcp" comment "RemoteOS MCP" >/dev/null 2>&1 || true
        echo "        UFW rule added for port $PORT"
    else
        echo "        UFW inactive, skipping"
    fi
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    echo "        firewalld rule added for port $PORT"
else
    echo "        No firewall detected, skipping"
fi

# --- [6/6] Get network info ---
echo "  [6/6] Getting network info..."
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "LINUX_IP")
HOSTNAME_SHORT=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "linux")
echo "        $HOSTNAME_SHORT ($LOCAL_IP)"

# --- Summary ---
echo ""
echo "  ============================================"
echo "  SETUP COMPLETE"
echo "  ============================================"
echo ""
echo "  Computer:  $HOSTNAME_SHORT ($LOCAL_IP)"
echo "  Port:      $PORT"
echo "  Auth Key:  $AUTH_KEY"
echo ""
echo "  Add to Claude Code:"
echo ""
echo "  claude mcp add --transport http linux-${HOSTNAME_SHORT} \\"
echo "    http://${LOCAL_IP}:${PORT}/mcp \\"
echo "    --header \"Authorization: Bearer ${AUTH_KEY}\""
echo ""
echo "  Service commands:"
echo "    sudo systemctl status $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  To uninstall later:"
echo "  curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/uninstall-linux.sh | sudo bash"
echo ""
```

- [ ] **Step 2: Make executable**

```bash
chmod +x install-linux.sh
```

- [ ] **Step 3: Commit**

```bash
git add install-linux.sh
git commit -m "Add Linux installer with systemd service setup"
```

---

### Task 9: Linux uninstaller — `uninstall-linux.sh`

**Files:**
- Create: `uninstall-linux.sh`

- [ ] **Step 1: Create the uninstaller**

```bash
#!/usr/bin/env bash
# RemoteOS MCP - Uninstaller for Linux
# Usage: curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/uninstall-linux.sh | sudo bash
set -euo pipefail

SERVICE_NAME="remoteos-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CONFIG_DIR="/etc/remoteos-mcp"

echo ""
echo "  RemoteOS MCP Uninstaller (Linux)"
echo ""

if [[ $EUID -ne 0 ]]; then
    echo "  [X] This uninstaller must be run as root (use sudo)."
    exit 1
fi

# Stop and disable service
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    echo "  [OK] Service stopped"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    echo "  [OK] Service disabled"
fi

# Remove service file
if [[ -f "$SERVICE_FILE" ]]; then
    rm "$SERVICE_FILE"
    systemctl daemon-reload
    echo "  [OK] Service file removed"
fi

# Remove config
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    echo "  [OK] Config removed ($CONFIG_DIR)"
fi

# Uninstall pip package
if python3 -m pip show remoteos-mcp &>/dev/null; then
    python3 -m pip uninstall -y remoteos-mcp 2>/dev/null || true
    echo "  [OK] Package uninstalled"
fi

echo ""
echo "  Uninstall complete."
echo ""
```

- [ ] **Step 2: Make executable**

```bash
chmod +x uninstall-linux.sh
```

- [ ] **Step 3: Commit**

```bash
git add uninstall-linux.sh
git commit -m "Add Linux uninstaller script"
```

---

### Task 10: Version bump

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Bump version**

In `pyproject.toml`, change:

```toml
version = "0.7.0.dev2"
```

To:

```toml
version = "0.7.0.dev3"
```

This should be done as the FIRST commit before any other changes per the version-bumping rule. However, since the plan is executed sequentially and all changes go into `dev`, this version bump should be the first thing committed.

**Important:** Move this task to be executed FIRST, before Task 1. The implementer should commit this version bump before any code changes.

- [ ] **Step 2: Commit**

```bash
git add pyproject.toml
git commit -m "Bump version to 0.7.0.dev3 for Linux support"
```

---

### Task 11: Update CLAUDE.md and README

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add Linux installer section. Change the existing macOS section header area to include Linux. After the macOS section, add:

```markdown
## Installing / Upgrading on Linux machines

```bash
curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/install-linux.sh | sudo bash
```

Requires `sudo`. Uses systemd for service management. Supports Ubuntu 24.04+ and other systemd-based distributions.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "Add Linux installer instructions to CLAUDE.md"
```

---

### Task 12: Deploy and verify on presenter.lan

**Files:** None (deployment task)

- [ ] **Step 1: Push dev branch**

```bash
git push origin dev
```

- [ ] **Step 2: Install on presenter.lan via SSH**

```bash
sshpass -p 'newlevel' ssh newlevel@presenter.lan "curl -fsSL https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/install-linux.sh | sudo -S bash <<< 'newlevel'"
```

Wait for installer output showing the auth key and IP.

- [ ] **Step 3: Verify service is running**

```bash
sshpass -p 'newlevel' ssh newlevel@presenter.lan "systemctl status remoteos-mcp"
```

Expected: Active (running).

- [ ] **Step 4: Verify MCP endpoint responds**

```bash
curl -s http://presenter.lan:8092/health
```

Expected: `{"status":"ok","version":"0.7.0.dev3"}`

- [ ] **Step 5: Test Shell tool via MCP**

Use the MCP tools to run a command on the Linux machine:

```
mcp__linux-presenter__Shell(command="uname -a")
```

Expected: Linux kernel info string.

- [ ] **Step 6: Test ServiceList tool via MCP**

```
mcp__linux-presenter__ServiceList(filter="ssh")
```

Expected: sshd service listed.

---

### Execution Order

**Critical:** Task 10 (version bump) must be executed FIRST, before all other tasks. The remaining tasks can follow in order 1→2→3→4→5→6→7→8→9→11→12.

Summary:
1. **Task 10** — Version bump (FIRST)
2. **Task 1** — Platform detection
3. **Task 2** — Linux desktop stub
4. **Task 3** — Linux services (systemctl, timers, journalctl)
5. **Task 4** — Linux system stub
6. **Task 5** — Tool exclusions on Linux
7. **Task 6** — Shell/pyautogui/metadata changes
8. **Task 7** — Fix disk label in GetSystemInfo
9. **Task 8** — Linux installer
10. **Task 9** — Linux uninstaller
11. **Task 11** — Update CLAUDE.md
12. **Task 12** — Deploy and verify on presenter.lan
