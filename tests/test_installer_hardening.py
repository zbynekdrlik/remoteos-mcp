"""RED regression tests for installer hardening (surfaced by #12 live deploy).

Tests that install-linux.sh:
(a) Aborts BEFORE any install when root filesystem is mounted read-only.
(b) Detects version mismatch between pip-installed package and /health endpoint.

Also tests install.sh (macOS):
(c) Detects version mismatch between pip-installed package and /health endpoint.

All tests run the real installer under bash with a FAKE PATH of shim scripts —
no root, no network, no system changes.  Hermetic via tmp_path.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER_LINUX = ROOT / "install-linux.sh"
INSTALLER_MACOS = ROOT / "install.sh"


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
        'SERVICE_FILE="/etc/systemd/system/${{SERVICE_NAME}}.service"',
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
        'PLIST_PATH="$HOME/Library/LaunchAgents/${{PLIST_LABEL}}.plist"',
        f'PLIST_PATH="{launch_dir}/com.remoteos-mcp.plist"',
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


# ---------------------------------------------------------------------------
# (a) Read-only rootfs must abort BEFORE any install command
# ---------------------------------------------------------------------------


class TestRoRootfsAbort:
    """install-linux.sh must abort when / is mounted read-only."""

    def test_ro_rootfs_aborts_with_message_and_nonzero(self, tmp_path: Path) -> None:
        """findmnt reports 'ro' as a mount option -> installer exits non-zero
        with '[X] Root filesystem is read-only' BEFORE calling apt-get / pip /
        systemctl."""
        script = _prepare_linux_installer(tmp_path)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        invocations = tmp_path / "invocations.log"

        # findmnt reports read-only root
        _make_shim(shim_dir / "findmnt", 'echo "ro,relatime,errors=remount-ro"')

        _create_linux_shims(shim_dir, invocations)

        result = _run_installer(script, shim_dir, tmp_path)

        # -- Must exit non-zero --
        assert result.returncode != 0, (
            "Installer should abort on ro rootfs but exited 0.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # -- Must print the specific ro message --
        output = result.stdout + result.stderr
        assert "read-only" in output.lower(), (
            "Expected 'read-only' in output when rootfs is ro.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # -- Must NOT have called any install commands --
        if invocations.exists():
            calls = invocations.read_text()
            assert "apt-get" not in calls, (
                f"apt-get was called despite ro rootfs:\n{calls}"
            )
            assert "pip install" not in calls.lower(), (
                f"pip install was called despite ro rootfs:\n{calls}"
            )
            assert "systemctl" not in calls, (
                f"systemctl was called despite ro rootfs:\n{calls}"
            )


# ---------------------------------------------------------------------------
# (b) Linux: version mismatch from /health must cause failure
# ---------------------------------------------------------------------------


class TestLinuxVersionSelfCheck:
    """install-linux.sh must detect version mismatch after service restart."""

    def test_version_mismatch_exits_nonzero(self, tmp_path: Path) -> None:
        """pip installed 0.7.0.dev13 but health reports 0.7.0.dev8 -> installer
        must exit non-zero with '[X] Installed ... but service reports ...'."""
        script = _prepare_linux_installer(tmp_path)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        invocations = tmp_path / "invocations.log"

        installed_ver = "0.7.0.dev13"
        health_ver = "0.7.0.dev8"

        # findmnt reports rw (happy path for ro check)
        _make_shim(shim_dir / "findmnt", 'echo "rw,relatime,errors=remount-ro"')

        _create_linux_shims(shim_dir, invocations, installed_version=installed_ver)

        # curl — returns stale version on health check
        _make_shim(
            shim_dir / "curl",
            f"""\
echo "curl $*" >> "{invocations}"
if [[ "$*" == *"health"* ]]; then
    echo '{{"status":"ok","version":"{health_ver}"}}'
    exit 0
fi
exit 0
""",
        )

        result = _run_installer(script, shim_dir, tmp_path)

        # -- Must exit non-zero due to version mismatch --
        assert result.returncode != 0, (
            f"Installer should fail on version mismatch "
            f"(installed {installed_ver}, health {health_ver}) "
            f"but exited 0.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # -- Must print both versions in the mismatch message --
        output = result.stdout + result.stderr
        assert installed_ver in output and health_ver in output, (
            f"Expected both '{installed_ver}' and '{health_ver}' in "
            f"mismatch output.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# (c) macOS: version mismatch from /health must cause failure
# ---------------------------------------------------------------------------


class TestMacosVersionSelfCheck:
    """install.sh (macOS) must detect version mismatch after service start."""

    def test_version_mismatch_exits_nonzero(self, tmp_path: Path) -> None:
        """pip installed 0.7.0.dev13 but health reports 0.7.0.dev8 -> installer
        must exit non-zero with version mismatch message."""
        script = _prepare_macos_installer(tmp_path)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        invocations = tmp_path / "invocations.log"

        installed_ver = "0.7.0.dev13"
        health_ver = "0.7.0.dev8"

        _create_macos_shims(shim_dir, invocations, installed_version=installed_ver)

        # curl — returns stale version on health check
        _make_shim(
            shim_dir / "curl",
            f"""\
echo "curl $*" >> "{invocations}"
if [[ "$*" == *"health"* ]]; then
    echo '{{"status":"ok","version":"{health_ver}"}}'
    exit 0
fi
# For the "is it running" check (Invoke-WebRequest equiv on macOS not used,
# but the macOS installer uses a basic GET check)
exit 0
""",
        )

        result = _run_installer(script, shim_dir, tmp_path)

        # -- Must exit non-zero due to version mismatch --
        assert result.returncode != 0, (
            f"macOS installer should fail on version mismatch "
            f"(installed {installed_ver}, health {health_ver}) "
            f"but exited 0.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # -- Must print both versions in the mismatch message --
        output = result.stdout + result.stderr
        assert installed_ver in output and health_ver in output, (
            f"Expected both '{installed_ver}' and '{health_ver}' in "
            f"mismatch output.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
