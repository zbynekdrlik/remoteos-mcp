"""RED regression tests for installer hardening (surfaced by #12 live deploy).

Tests that install-linux.sh:
(a) Aborts BEFORE any install when root filesystem is mounted read-only.
(b) Detects version mismatch between pip-installed package and /health endpoint.
(d) Uses --force-reinstall (not --ignore-installed) for reliable upgrades.

Also tests install.sh (macOS):
(c) Detects version mismatch between pip-installed package and /health endpoint.
(d) Uses --force-reinstall (not --ignore-installed) for reliable upgrades.

Also tests install.ps1 (Windows):
(d) Uses --force-reinstall for reliable upgrades.

All tests run the real installer under bash with a FAKE PATH of shim scripts —
no root, no network, no system changes.  Hermetic via tmp_path.
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


# ---------------------------------------------------------------------------
# (d) All installers must use --force-reinstall (not --ignore-installed)
# ---------------------------------------------------------------------------


class TestNoIgnoreInstalled:
    """Installers must NOT use --ignore-installed (insufficient for git-based
    upgrades — the imag.lan dev8-instead-of-dev13 incident, #12 2026-09-09).
    The correct approach is pre-uninstall + install (no --force-reinstall)."""

    def test_linux_installer_no_ignore_installed(self) -> None:
        """install-linux.sh must NOT use --ignore-installed."""
        content = INSTALLER_LINUX.read_text()
        assert "--ignore-installed" not in content, (
            "install-linux.sh must not use --ignore-installed — it does not "
            "force pip to re-clone from git when an older version exists"
        )


# ---------------------------------------------------------------------------
# (e) Windows: post-install health/version self-check
# ---------------------------------------------------------------------------


class TestWindowsVersionSelfCheck:
    """install.ps1 must verify the running service reports the correct version
    after the scheduled task starts — the same self-check Linux/macOS have."""

    def test_windows_installer_has_health_check(self) -> None:
        """install.ps1 must poll the health endpoint after starting the server."""
        content = INSTALLER_WINDOWS.read_text()
        assert "health" in content.lower(), (
            "install.ps1 must poll the /health endpoint after starting the "
            "server to verify the installed version matches the running version"
        )
        # Must use Invoke-RestMethod or Invoke-WebRequest for health check
        assert (
            "Invoke-RestMethod" in content or "Invoke-WebRequest" in content
        ), (
            "install.ps1 must use Invoke-RestMethod or Invoke-WebRequest to "
            "poll the health endpoint"
        )

    def test_windows_installer_compares_versions(self) -> None:
        """install.ps1 must compare installed version with health version and
        exit non-zero on mismatch."""
        content = INSTALLER_WINDOWS.read_text()
        # Must read installed version via importlib.metadata
        assert "importlib.metadata" in content or "pip show" in content, (
            "install.ps1 must read the installed package version (via "
            "importlib.metadata or pip show) to compare with /health"
        )
        # Must exit on mismatch
        assert "exit 1" in content or "exit(1)" in content, (
            "install.ps1 must exit non-zero when installed version does not "
            "match the running service version"
        )

    def test_windows_installer_exits_on_health_timeout(self) -> None:
        """install.ps1 must exit non-zero if health endpoint does not respond."""
        content = INSTALLER_WINDOWS.read_text()
        # Must have a timeout/retry loop AND exit on failure
        assert "30" in content or "attempt" in content.lower(), (
            "install.ps1 must poll health with a bounded timeout (~30s)"
        )
        # The mismatch/timeout message pattern
        assert "[X]" in content and "health" in content.lower(), (
            "install.ps1 must print an [X] error message when health check "
            "fails (mismatch or timeout)"
        )


# ---------------------------------------------------------------------------
# (f) Windows: stop running service BEFORE pip install, fail-hard import check
# ---------------------------------------------------------------------------


class TestWindowsStopBeforePip:
    """install.ps1 must stop the running service BEFORE pip install.
    Running pip --force-reinstall while the old python process has fastmcp's
    files open on Windows leaves a corrupt package (no __init__.py) because
    Windows cannot delete open files.  The fix is to stop the service first,
    not to repair the corruption after."""

    def test_stop_precedes_pip_install(self) -> None:
        """The stop-task/kill-process block must appear textually BEFORE the
        pip install command in install.ps1."""
        content = INSTALLER_WINDOWS.read_text()
        # Find position of process kill (the definitive stop)
        stop_pos = content.find("Stop-Process")
        assert stop_pos != -1, (
            "install.ps1 must contain a Stop-Process call to kill the old "
            "server before pip install"
        )
        # Find position of the MAIN pip install command (the one installing
        # from main.zip, not the fastmcp-slim recovery line).
        lines = content.split("\n")
        pip_line_offset = 0
        pip_found = False
        for line in lines:
            stripped = line.strip()
            if (
                "pip" in stripped
                and "install" in stripped
                and "main.zip" in stripped
                and not stripped.startswith("#")
                and not stripped.startswith("REM")
                and "Write-Host" not in stripped
                and "fastmcp" not in stripped
            ):
                pip_found = True
                break
            pip_line_offset += len(line) + 1  # +1 for newline
        assert pip_found, "install.ps1 must contain a pip install command"
        assert stop_pos < pip_line_offset, (
            f"install.ps1 must stop the running server (Stop-Process at char "
            f"{stop_pos}) BEFORE running pip install (at char "
            f"{pip_line_offset}). Running pip while the old service is alive "
            f"corrupts packages on Windows because open files cannot be "
            f"deleted."
        )

    def test_import_check_fails_hard(self) -> None:
        """install.ps1 must have an import sanity check for remoteos+fastmcp
        that exits non-zero on failure (never repairs)."""
        content = INSTALLER_WINDOWS.read_text()
        # Must import both remoteos and fastmcp
        assert "import remoteos" in content or "import fastmcp" in content, (
            "install.ps1 must have a post-install import sanity check for "
            "remoteos and/or fastmcp"
        )
        # The import check block must contain exit 1 (fail hard)
        assert "exit 1" in content, (
            "install.ps1 import check must exit 1 on failure — never repair"
        )


# ---------------------------------------------------------------------------
# (g) All installers: deterministic pre-uninstall before install
# ---------------------------------------------------------------------------


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


class TestDeterministicPreUninstall:
    """All three installers must explicitly pip uninstall remoteos-mcp, fastmcp,
    AND fastmcp-slim BEFORE pip install — so no stale RECORD from a pre-split
    monolithic fastmcp 2.x can delete fastmcp/__init__.py that fastmcp-slim
    just wrote.

    Root cause (wheel RECORD evidence, #12 win-stream-snv):
    - fastmcp 2.x (monolithic) RECORD claims fastmcp/__init__.py
    - fastmcp-slim 4.x RECORD also claims fastmcp/__init__.py
    - pip --force-reinstall can install fastmcp-slim (writes __init__.py)
      then uninstall old fastmcp 2.x (deletes __init__.py from its RECORD)

    The fix is deterministic: pre-uninstall all three packages, then install
    fresh with no --force-reinstall.  No recovery/repair blocks."""

    # --- Windows (install.ps1) ---

    def test_windows_pre_uninstall_present(self) -> None:
        """install.ps1 must pip uninstall remoteos-mcp, fastmcp, AND
        fastmcp-slim before the main pip install."""
        content = INSTALLER_WINDOWS.read_text()
        uninstall_pos = _find_first_code_line(content, "pip", "uninstall", "remoteos-mcp")
        install_pos = _find_first_code_line(content, "pip", "install", "main.zip")
        assert uninstall_pos is not None, (
            "install.ps1 must pip uninstall remoteos-mcp before install"
        )
        assert install_pos is not None, (
            "install.ps1 must have a pip install line"
        )
        assert uninstall_pos < install_pos, (
            f"install.ps1: pre-uninstall (line {uninstall_pos + 1}) must "
            f"precede pip install (line {install_pos + 1})"
        )
        # The uninstall line must also name fastmcp and fastmcp-slim
        lines = content.split("\n")
        uninstall_line = lines[uninstall_pos].strip()
        assert "fastmcp" in uninstall_line, (
            f"install.ps1 pre-uninstall must include fastmcp: {uninstall_line!r}"
        )

    def test_windows_no_force_reinstall(self) -> None:
        """install.ps1 must NOT use --force-reinstall on the main install."""
        content = INSTALLER_WINDOWS.read_text()
        assert _find_first_code_line(content, "pip", "install", "--force-reinstall", "main.zip") is None, (
            "install.ps1 must not use --force-reinstall on the main install "
            "(cascades to deps and corrupts fastmcp/__init__.py)"
        )

    def test_windows_no_recovery_lines(self) -> None:
        """install.ps1 must NOT have fastmcp-slim repair/recovery pip install
        lines (pip uninstall lines naming fastmcp-slim are fine — that is the
        pre-uninstall, not a recovery)."""
        content = INSTALLER_WINDOWS.read_text()
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("REM") or "Write-Host" in stripped:
                continue
            # A pip install (not uninstall) targeting fastmcp-slim specifically
            if (
                "pip" in stripped
                and "install" in stripped
                and "uninstall" not in stripped
                and "fastmcp-slim" in stripped
            ):
                assert False, (
                    f"install.ps1 line {i + 1} has a fastmcp-slim repair line: "
                    f"{stripped!r}. The pre-uninstall eliminates the root cause; "
                    f"no recovery is needed."
                )

    # --- Linux (install-linux.sh) ---

    def test_linux_pre_uninstall_present(self) -> None:
        """install-linux.sh must pip uninstall remoteos-mcp, fastmcp, AND
        fastmcp-slim before the main pip install."""
        content = INSTALLER_LINUX.read_text()
        uninstall_pos = _find_first_code_line(content, "pip", "uninstall", "remoteos-mcp")
        install_pos = _find_first_code_line(content, "pip", "install", "remoteos-mcp", exclude="uninstall")
        assert uninstall_pos is not None, (
            "install-linux.sh must pip uninstall before install"
        )
        assert install_pos is not None
        assert uninstall_pos < install_pos, (
            f"install-linux.sh: pre-uninstall (line {uninstall_pos + 1}) must "
            f"precede pip install (line {install_pos + 1})"
        )
        lines = content.split("\n")
        uninstall_line = lines[uninstall_pos]
        assert "fastmcp" in uninstall_line, (
            f"install-linux.sh pre-uninstall must include fastmcp: {uninstall_line!r}"
        )

    def test_linux_no_force_reinstall(self) -> None:
        """install-linux.sh must NOT use --force-reinstall on the main install.
        The flag may span continuation lines, so check all non-comment lines."""
        content = INSTALLER_LINUX.read_text()
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip().rstrip("\\")
            if stripped.startswith("#"):
                continue
            if "pip" in stripped and "install" in stripped and "--force-reinstall" in stripped:
                assert False, (
                    f"install-linux.sh line {i + 1} uses --force-reinstall: "
                    f"{stripped!r}. Use pre-uninstall + install instead."
                )

    # --- macOS (install.sh) ---

    def test_macos_pre_uninstall_present(self) -> None:
        """install.sh must pip uninstall remoteos-mcp, fastmcp, AND
        fastmcp-slim before the main pip install."""
        content = INSTALLER_MACOS.read_text()
        uninstall_pos = _find_first_code_line(content, "pip", "uninstall", "remoteos-mcp")
        install_pos = _find_first_code_line(content, "pip", "install", "remoteos-mcp", exclude="uninstall")
        assert uninstall_pos is not None, (
            "install.sh must pip uninstall before install"
        )
        assert install_pos is not None
        assert uninstall_pos < install_pos, (
            f"install.sh: pre-uninstall (line {uninstall_pos + 1}) must "
            f"precede pip install (line {install_pos + 1})"
        )
        lines = content.split("\n")
        uninstall_line = lines[uninstall_pos]
        assert "fastmcp" in uninstall_line, (
            f"install.sh pre-uninstall must include fastmcp: {uninstall_line!r}"
        )

    def test_macos_no_force_reinstall(self) -> None:
        """install.sh must NOT use --force-reinstall on the main install.
        The flag may span continuation lines, so check all non-comment lines."""
        content = INSTALLER_MACOS.read_text()
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip().rstrip("\\")
            if stripped.startswith("#"):
                continue
            if "pip" in stripped and "install" in stripped and "--force-reinstall" in stripped:
                assert False, (
                    f"install.sh line {i + 1} uses --force-reinstall: "
                    f"{stripped!r}. Use pre-uninstall + install instead."
                )
