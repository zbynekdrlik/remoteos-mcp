"""Regression tests for installer hardening (surfaced by #12 live deploys).

install-linux.sh:
(a) Aborts BEFORE any install when the root filesystem is mounted read-only.
    (Its /health version check is in tests/test_installer_linux_venv.py, since
    the #12 venv layout.)

install.sh (macOS):
(c) Detects a version mismatch between the pip-installed package and /health.

install.ps1 (Windows):
(e) Post-install /health version self-check.
(f) Stops the running service before pip install; fail-hard import check.

All installers:
(g) Deterministic pre-uninstall of remoteos-mcp, fastmcp, fastmcp-slim (macOS,
    Windows: the system/user python), and no --force-reinstall on the main install.
(h) Every pip uninstall from a system python passes --break-system-packages
    (macOS: the pre-uninstall; Linux: the removal of the old system-python copy).

The behavioural tests run the real installer under bash with a FAKE PATH of
shim scripts — no root, no network, no system changes.  Hermetic via tmp_path.
"""


from __future__ import annotations

from pathlib import Path

from installer_harness import (
    INSTALLER_LINUX,
    INSTALLER_MACOS,
    INSTALLER_WINDOWS,
    _create_macos_shims,
    _find_first_code_line,
    _make_shim,
    _prepare_macos_installer,
    _run_installer,
)
from linux_installer_harness import linux_box

# ---------------------------------------------------------------------------
# (a) Read-only rootfs must abort BEFORE any install command
# ---------------------------------------------------------------------------


class TestRoRootfsAbort:
    """install-linux.sh must abort when / is mounted read-only."""

    def test_ro_rootfs_aborts_with_message_and_nonzero(self, tmp_path: Path) -> None:
        """findmnt reports 'ro' as a mount option -> installer exits non-zero
        with '[X] Root filesystem is read-only' BEFORE calling apt-get / pip /
        systemctl."""
        box = linux_box(tmp_path)
        box.shim("findmnt", 'echo "ro,relatime,errors=remount-ro"\n')

        result = box.run()

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

        # -- Must NOT have started any install / download / service command --
        started = {argv[0] for argv in box.argvs()}
        assert not started & {"apt-get", "curl", "systemctl", "git", "python3", "tar"}, box.argvs()

    def test_the_read_only_message_never_advises_editing_fstab(self, tmp_path: Path) -> None:
        """Review round 12: camera-box's cams are read-only by design (its ro-root.sh) and run
        this installer inside camera-box's own rw window; telling the operator to turn fstab's
        `ro` into `defaults` would undo the appliance."""
        box = linux_box(tmp_path)
        box.shim("findmnt", 'echo "ro,relatime"\n')

        result = box.run()

        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        assert "fstab" not in output, output
        assert "camera-box" in output and "nothing was changed" in output, output


class TestLoopbackCurlsGoDirect:
    """Review rounds 11-12: every curl to the box's own 127.0.0.1 starts `curl -q --noproxy '*'`.
    --noproxy keeps a proxy from the environment (kept for pip) or a .curlrc off the request; -q
    keeps every other .curlrc option (a trace would write the Authorization header) off it."""

    def test_every_loopback_curl_starts_with_q_and_noproxy(self) -> None:
        for installer in (INSTALLER_LINUX, INSTALLER_MACOS):
            lines = installer.read_text().splitlines()
            calls = [(n, line) for n, line in enumerate(lines, 1)
                     if "curl " in line and not line.lstrip().startswith(("#", "echo", '"'))
                     and "-o \"$tgz\"" not in line]  # the source download goes through any proxy
            assert calls, installer
            bad = [(n, line.strip()) for n, line in calls if "curl -q --noproxy '*' " not in line]
            assert bad == [], f"{installer.name}: a loopback curl may use a proxy or a .curlrc: {bad}"


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
        # Must fail on mismatch: since #64 the proof is Test-RemoteOSServerHealth (the rollback
        # proves the old version with it too); its failure goes through Exit-RemoteOSAfterFailure
        mismatch = next(line.strip() for line in content.split("\n") if "but service reports" in line)
        assert mismatch.startswith("return [pscustomobject]@{ Ok = $false;"), (
            "install.ps1 must fail when installed version does not match the running service version"
        )
        failure = content.index("if (-not $health.Ok) {")
        assert "Exit-RemoteOSAfterFailure -Reason $health.Message" in content[failure : content.index("\n}", failure)]

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
        # Since #64 the stop and the offline install are functions; the flow calls them in order
        assert "Stop-Process" in content[content.index("function Stop-RemoteOSServer("):], (
            "install.ps1 must contain a Stop-Process call to kill the old server before pip install"
        )
        stop_pos = content.find("\nStop-RemoteOSServer -Port $Port")
        pip_pos = content.find("\n$install = Install-RemoteOSWheelhouse -Python $python")
        assert stop_pos != -1 and pip_pos != -1, "install.ps1 must stop the server and install in its flow"
        assert stop_pos < pip_pos, (
            "install.ps1 must stop the running server BEFORE running pip install. Running pip while "
            "the old service is alive corrupts packages on Windows because open files cannot be deleted."
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
        # The import check fails the install (Install-RemoteOSWheelhouse returns Ok = $false), and
        # the flow then rolls back and exits 1 (#64) — never a repair
        failure = next(line.strip() for line in content.split("\n") if "Import check failed" in line)
        assert failure.startswith("return [pscustomobject]@{ Ok = $false;"), failure
        block = content[content.index("if (-not $install.Ok) {"):]
        assert block.split("\n")[1].strip().startswith("Exit-RemoteOSAfterFailure -Reason $install.Error"), (
            "install.ps1 import check must exit 1 on failure — never repair"
        )


# ---------------------------------------------------------------------------
# (g) All installers: deterministic pre-uninstall before install
# ---------------------------------------------------------------------------


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
        # Since #64: Install-RemoteOSWheelhouse, offline from the commit's wheelhouse
        uninstall_pos = _find_first_code_line(content, "Pip", "'uninstall'", "'remoteos-mcp'")
        install_pos = _find_first_code_line(content, "Pip", "'install'", "'--no-index'", exclude="'--dry-run'")
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
        assert _find_first_code_line(content, "pip", "install", "--force-reinstall", "archive/") is None, (
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
    # Since #12 (0.7.0.dev32) Linux installs into a FRESH venv built side by side, so there is
    # no pre-uninstall: no stale RECORD can exist in it (tests/test_installer_linux_venv.py).

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


# ---------------------------------------------------------------------------
# (h) Linux/macOS: pip uninstall must use --break-system-packages
# ---------------------------------------------------------------------------


class TestPreUninstallBreakSystemPackages:
    """On PEP 668 systems (Ubuntu 24.04+), pip uninstall without
    --break-system-packages fails with 'externally-managed-environment',
    causing the pre-uninstall to silently fail (swallowed by || true).
    Stale packages then interfere with the fresh install.

    Root cause (cam1.lan 2026-09-10): canonical installer ran on Ubuntu 24.04.
    The pre-uninstall `pip uninstall -y remoteos-mcp fastmcp fastmcp-slim`
    failed silently (PEP 668, no --break-system-packages). The subsequent
    pip install installed 0.7.0.dev12 instead of dev19 because stale packages
    from a prior half-failed install were still present."""

    def test_linux_pre_uninstall_has_break_system_packages(self) -> None:
        """install-linux.sh's pip uninstall from the SYSTEM python (the removal of
        the old system-python copy, since #12) must include --break-system-packages
        so it succeeds on PEP 668 systems (Ubuntu 24.04+)."""
        content = INSTALLER_LINUX.read_text()
        uninstall_pos = _find_first_code_line(content, "pip", "uninstall", "remoteos-mcp")
        assert uninstall_pos is not None, (
            "install-linux.sh must have a pip uninstall line"
        )
        lines = content.split("\n")
        uninstall_line = lines[uninstall_pos]
        assert "--break-system-packages" in uninstall_line, (
            f"install-linux.sh pip uninstall (line {uninstall_pos + 1}) must "
            f"include --break-system-packages for PEP 668 systems. "
            f"Without it, the uninstall silently fails on Ubuntu 24.04+ and "
            f"stale packages interfere with the fresh install. "
            f"Line: {uninstall_line.strip()!r}"
        )

    def test_macos_pre_uninstall_has_break_system_packages(self) -> None:
        """install.sh (macOS) pip uninstall must include --break-system-packages
        for consistency and future PEP 668 compliance."""
        content = INSTALLER_MACOS.read_text()
        uninstall_pos = _find_first_code_line(content, "pip", "uninstall", "remoteos-mcp")
        assert uninstall_pos is not None, (
            "install.sh must have a pip uninstall line"
        )
        lines = content.split("\n")
        uninstall_line = lines[uninstall_pos]
        assert "--break-system-packages" in uninstall_line, (
            f"install.sh pip uninstall (line {uninstall_pos + 1}) must "
            f"include --break-system-packages for PEP 668 systems. "
            f"Line: {uninstall_line.strip()!r}"
        )
