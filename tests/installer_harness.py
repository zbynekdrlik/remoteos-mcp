"""Shared harness for the installer tests (install-linux.sh, install.sh, install.ps1).

The installers are copied to tmp_path with root-only paths redirected, then run
under bash with a FAKE PATH of shim scripts — no root, no network, no system
changes.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER_LINUX = ROOT / "install-linux.sh"
INSTALLER_MACOS = ROOT / "install.sh"
INSTALLER_WINDOWS = ROOT / "install.ps1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shim(path: Path, content: str) -> None:
    """Write an executable bash shim."""
    path.write_text(f"#!/bin/bash\n{content}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _real_python() -> str:
    """Absolute path to the real Python interpreter running pytest."""
    return sys.executable


def _prepare_linux_installer(tmp_path: Path) -> Path:
    """Copy install-linux.sh to tmp with test patches (no-root, tmp dirs)."""
    content = INSTALLER_LINUX.read_text()

    etc_dir = tmp_path / "etc-remoteos-mcp"
    etc_dir.mkdir(parents=True, exist_ok=True)

    # Disable EUID root check — tests run as unprivileged user
    content = content.replace(
        'if [[ "$EUID" -ne 0 ]]; then',
        "if false; then",
    )
    content = content.replace(
        'ACTUAL_USER="${SUDO_USER:-root}"',
        'ACTUAL_USER="testuser"',
    )
    # Redirect writable paths to tmp
    content = content.replace(
        'CONFIG_DIR="/etc/remoteos-mcp"',
        f'CONFIG_DIR="{etc_dir}"',
    )
    content = content.replace(
        'SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"',
        f'SERVICE_FILE="{tmp_path}/remoteos-mcp.service"',
    )

    script = tmp_path / "install-linux.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


def _prepare_macos_installer(tmp_path: Path) -> Path:
    """Copy install.sh (macOS) to tmp with test patches (tmp dirs)."""
    content = INSTALLER_MACOS.read_text()

    config_dir = tmp_path / "dot-remoteos-mcp"
    config_dir.mkdir(parents=True, exist_ok=True)
    launch_dir = tmp_path / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)

    # Redirect writable paths
    content = content.replace(
        'CONFIG_DIR="$HOME/.remoteos-mcp"',
        f'CONFIG_DIR="{config_dir}"',
    )
    content = content.replace(
        'PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"',
        f'PLIST_PATH="{launch_dir}/com.remoteos-mcp.plist"',
    )
    # The macOS auth-key generation uses `tr -dc ... | head -c 32` which
    # causes SIGPIPE+pipefail on Linux (works on macOS).  Replace with the
    # Python approach the Linux installer already uses.
    content = content.replace(
        "AUTH_KEY=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)",
        'AUTH_KEY=$("$PYTHON" -c "import secrets,string; '
        "print(''.join(secrets.choice(string.ascii_letters+string.digits) "
        'for _ in range(32)))")',
    )
    # mkdir for LaunchAgents — ensure directory exists under tmp HOME
    content = content.replace(
        'mkdir -p "$HOME/Library/LaunchAgents"',
        f'mkdir -p "{launch_dir}"',
    )

    script = tmp_path / "install-macos.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


def _create_linux_shims(
    shim_dir: Path,
    invocations: Path,
    *,
    installed_version: str = "0.7.0.dev13",
) -> None:
    """Create the full set of shims for install-linux.sh testing."""
    real_py = _real_python()

    # python3 — intercept pip calls, delegate -c to real python
    _make_shim(
        shim_dir / "python3",
        f"""\
case "$1" in
    --version)
        echo "Python 3.12.0"
        exit 0
        ;;
    -m)
        if [[ "$2" == "pip" ]]; then
            shift 2
            echo "python3 -m pip $*" >> "{invocations}"
            case "$1" in
                --version)
                    echo "pip 24.0 from /test (python 3.12)"
                    exit 0
                    ;;
                install)
                    echo "Successfully installed remoteos-mcp-{installed_version}"
                    exit 0
                    ;;
                show)
                    echo "Name: remoteos-mcp"
                    echo "Version: {installed_version}"
                    exit 0
                    ;;
                *)
                    exit 0
                    ;;
            esac
        elif [[ "$2" == "ensurepip" ]]; then
            exit 0
        else
            exec "{real_py}" "$@"
        fi
        ;;
    -c)
        exec "{real_py}" "$@"
        ;;
    *)
        exec "{real_py}" "$@"
        ;;
esac
""",
    )

    # git
    _make_shim(shim_dir / "git", 'echo "git version 2.43.0"')

    # apt-get — logs all invocations
    _make_shim(shim_dir / "apt-get", f'echo "apt-get $*" >> "{invocations}"\nexit 0')

    # systemctl — logs all invocations
    _make_shim(
        shim_dir / "systemctl",
        f'echo "systemctl $*" >> "{invocations}"\nexit 0',
    )

    # loginctl — no graphical sessions (headless)
    _make_shim(
        shim_dir / "loginctl",
        """\
if [[ "$1" == "list-sessions" ]]; then
    echo ""
fi
exit 0
""",
    )

    # hostname
    _make_shim(
        shim_dir / "hostname",
        """\
if [[ "$1" == "-I" ]]; then
    echo "192.168.1.100"
elif [[ "$1" == "-s" ]]; then
    echo "testhost"
else
    echo "testhost"
fi
""",
    )

    # ufw / firewall-cmd — not available
    _make_shim(shim_dir / "ufw", "exit 1")
    _make_shim(shim_dir / "firewall-cmd", "exit 1")


def _create_macos_shims(
    shim_dir: Path,
    invocations: Path,
    *,
    installed_version: str = "0.7.0.dev13",
) -> None:
    """Create the full set of shims for install.sh (macOS) testing."""
    real_py = _real_python()

    # python3 — same pattern as Linux
    _make_shim(
        shim_dir / "python3",
        f"""\
case "$1" in
    --version)
        echo "Python 3.12.0"
        exit 0
        ;;
    -m)
        if [[ "$2" == "pip" ]]; then
            shift 2
            echo "python3 -m pip $*" >> "{invocations}"
            case "$1" in
                --version)
                    echo "pip 24.0 from /test (python 3.12)"
                    exit 0
                    ;;
                install)
                    echo "Successfully installed remoteos-mcp-{installed_version}"
                    exit 0
                    ;;
                show)
                    echo "Name: remoteos-mcp"
                    echo "Version: {installed_version}"
                    exit 0
                    ;;
                *)
                    exit 0
                    ;;
            esac
        else
            exec "{real_py}" "$@"
        fi
        ;;
    -c)
        exec "{real_py}" "$@"
        ;;
    *)
        exec "{real_py}" "$@"
        ;;
esac
""",
    )

    # launchctl — no-op
    _make_shim(
        shim_dir / "launchctl",
        f'echo "launchctl $*" >> "{invocations}"\nexit 0',
    )

    # ipconfig (macOS) — return test IP
    _make_shim(shim_dir / "ipconfig", 'echo "192.168.1.100"')

    # hostname
    _make_shim(
        shim_dir / "hostname",
        """\
if [[ "$1" == "-s" ]]; then
    echo "testmac"
elif [[ "$1" == "-I" ]]; then
    echo "192.168.1.100"
else
    echo "testmac"
fi
""",
    )

    # brew — not available
    _make_shim(shim_dir / "brew", "exit 1")


def _run_installer(
    script: Path,
    shim_dir: Path,
    tmp_path: Path,
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run an installer script with controlled PATH."""
    env = {
        "PATH": f"{shim_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "TERM": "dumb",
        "LANG": "C",
    }

    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _find_first_code_line(content: str, *keywords: str, exclude: str | None = None) -> int | None:
    """Return the 0-based line index of the first non-comment code line that
    contains ALL of the given keywords (and does NOT contain exclude), or None."""
    for i, line in enumerate(content.split("\n")):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("REM") or "Write-Host" in stripped:
            continue
        if all(kw in stripped for kw in keywords):
            if exclude and exclude in stripped:
                continue
            return i
    return None
