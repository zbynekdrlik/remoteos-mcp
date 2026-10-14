"""Installer preflight, rollback and full pip error output (#12, Ubuntu 26.04).

Live on strih-pp (Ubuntu 26.04, Python 3.14, Debian pip 25.1.1) the canonical
install-linux.sh upgrade failed: the pinned set has to replace apt-owned
python3-* packages (cryptography, PyJWT, psutil, ...) and Debian's pip refuses
to uninstall them.  Three defects sat on that path:

1. no --ignore-installed, so pip tried to uninstall the apt copies;
2. `pip install ... | tail -3 || true` hid the real error;
3. the pre-uninstall of remoteos-mcp/fastmcp/fastmcp-slim ran BEFORE anything
   proved the new install could succeed, so a failed install left the box with
   NO remoteos package (the service survived only from memory).

These tests run the real installers against tests/fake_pip.py — a stateful
Debian-pip stand-in with a fake site-packages — so the preflight, the rollback
and the restored bytes are checked end to end.  Hermetic: no root, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from installer_harness import (
    GIT_URL,
    INSTALLER_WINDOWS,
    MAIN_CONSTRAINTS,
    NEW_VERSION,
    OLD_FASTMCP_SLIM,
    OLD_VERSION,
    FakePipBox,
    _create_linux_shims,
    _create_macos_shims,
    _find_first_code_line,
    _make_shim,
    _prepare_linux_installer,
    _prepare_macos_installer,
    _run_installer,
    health_shim,
    install_old_remoteos,
    setup_fake_pip,
)

OVERLAPPING = {"remoteos-mcp", "fastmcp", "fastmcp-slim"}


def _linux_box(tmp_path: Path, *, upgrade: bool = True, **config: object) -> tuple[Path, Path, FakePipBox, Path]:
    script = _prepare_linux_installer(tmp_path)
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    invocations = tmp_path / "invocations.log"
    _make_shim(shim_dir / "findmnt", 'echo "rw,relatime"')
    _create_linux_shims(shim_dir, invocations)
    box = setup_fake_pip(tmp_path, shim_dir, **config)
    health_shim(shim_dir, NEW_VERSION)
    if upgrade:
        install_old_remoteos(box)
    return script, shim_dir, box, invocations


def _macos_box(tmp_path: Path, **config: object) -> tuple[Path, Path, FakePipBox, Path]:
    script = _prepare_macos_installer(tmp_path)
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    invocations = tmp_path / "invocations.log"
    _create_macos_shims(shim_dir, invocations)
    box = setup_fake_pip(tmp_path, shim_dir, **config)
    health_shim(shim_dir, NEW_VERSION)
    install_old_remoteos(box)
    return script, shim_dir, box, invocations


def _output(result) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _invocations(path: Path) -> str:
    return path.read_text() if path.exists() else ""


# ---------------------------------------------------------------------------
# Linux: apt-owned dependencies are shadowed, never uninstalled
# ---------------------------------------------------------------------------


class TestLinuxDebianOwnedDependencies:
    def test_upgrade_over_debian_owned_dependencies_succeeds_with_ignore_installed(self, tmp_path: Path) -> None:
        """Debian's pip refuses to uninstall apt-owned cryptography/PyJWT.  The
        pinned install must overlay them (--ignore-installed) and succeed."""
        script, shim_dir, box, _ = _linux_box(tmp_path, debian_owned=["cryptography", "PyJWT"])

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode == 0, out
        assert box.versions("remoteos-mcp") == [NEW_VERSION], out
        installs = box.calls("install")
        assert len(installs) == 1, box.entries()
        argv = installs[0]["argv"]
        assert "--ignore-installed" in argv, argv
        assert "--no-cache-dir" in argv and "--break-system-packages" in argv, argv
        assert GIT_URL in argv, argv
        assert installs[0]["PIP_CONSTRAINT"] == MAIN_CONSTRAINTS
        # The explicit pre-uninstall stays, between the preflight and the install.
        assert box.verbs() == ["dry-run", "uninstall", "install"], box.entries()
        assert OVERLAPPING <= set(box.calls("uninstall")[0]["argv"])
        for call in box.calls("uninstall"):
            assert "cryptography" not in call["argv"] and "PyJWT" not in call["argv"]


# ---------------------------------------------------------------------------
# Linux: dry-run preflight before anything is touched
# ---------------------------------------------------------------------------


class TestLinuxPreflight:
    def test_failed_dry_run_aborts_before_any_uninstall_and_prints_the_full_error(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _linux_box(tmp_path, dry_run_fail=True, error_lines=12)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        dry_runs = box.calls("dry-run")
        assert len(dry_runs) == 1, box.entries()
        argv = dry_runs[0]["argv"]
        assert "--ignore-installed" in argv and "--break-system-packages" in argv and GIT_URL in argv, argv
        assert dry_runs[0]["PIP_CONSTRAINT"] == MAIN_CONSTRAINTS
        assert box.calls("uninstall") == [], "pip uninstall ran although the preflight failed"
        assert box.calls("install") == [], "the real install ran although the preflight failed"
        assert box.files() == before, "the box changed although the preflight failed"
        for i in range(1, 13):
            assert f"dry-run-error-line-{i:02d}" in out, f"line {i} of the pip error is missing:\n{out}"
        assert "systemctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# Linux: a failed real install rolls back to the recorded version
# ---------------------------------------------------------------------------


class TestLinuxRollback:
    def test_failed_install_after_pre_uninstall_restores_the_recorded_version(self, tmp_path: Path) -> None:
        """The install dies after the pre-uninstall (and after half-installing
        fastmcp-slim).  The box must end with exactly the old package files —
        never without a remoteos package — and the installer must exit non-zero
        without restarting the service."""
        script, shim_dir, box, invocations = _linux_box(tmp_path, install_fail=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.verbs()[:3] == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.versions("fastmcp-slim") == [OLD_FASTMCP_SLIM], out
        assert box.files() == before, "the rollback did not restore the exact previous files and modes"
        assert OLD_VERSION in out and "roll" in out.lower(), out
        assert "systemctl restart" not in _invocations(invocations)

    def test_failed_install_prints_the_full_pip_output(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _linux_box(tmp_path, install_fail=True, error_lines=20)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        for i in range(1, 21):
            assert f"install-error-line-{i:02d}" in out, f"line {i} of the pip error is missing:\n{out}"

    def test_failed_install_on_a_fresh_box_exits_nonzero_with_the_full_error(self, tmp_path: Path) -> None:
        """Nothing was installed before: nothing to roll back, but the failure
        must still be loud and complete."""
        script, shim_dir, box, invocations = _linux_box(tmp_path, upgrade=False, install_fail=True)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.verbs() == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == []
        for i in range(1, 13):
            assert f"install-error-line-{i:02d}" in out, out
        assert "systemctl restart" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# macOS: the same uninstall-before-install path gets the same protection
# ---------------------------------------------------------------------------


class TestMacosPreflightRollback:
    def test_upgrade_runs_preflight_then_pre_uninstall_then_install(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _macos_box(tmp_path)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode == 0, out
        assert box.verbs() == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == [NEW_VERSION]
        assert box.calls("dry-run")[0]["PIP_CONSTRAINT"] == MAIN_CONSTRAINTS

    def test_failed_dry_run_aborts_before_any_uninstall_and_prints_the_full_error(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _macos_box(tmp_path, dry_run_fail=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.files() == before
        for i in range(1, 13):
            assert f"dry-run-error-line-{i:02d}" in out, out
        assert "launchctl" not in _invocations(invocations)

    def test_failed_install_restores_the_recorded_version_and_prints_the_full_error(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _macos_box(tmp_path, install_fail=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.files() == before, "the rollback did not restore the exact previous files and modes"
        for i in range(1, 13):
            assert f"install-error-line-{i:02d}" in out, out
        assert "launchctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# Windows: parity of the error output only
# ---------------------------------------------------------------------------


class TestWindowsPipErrorOutput:
    def test_main_pip_install_output_is_kept_and_printed_on_failure(self) -> None:
        """install.ps1 piped the main pip install into Out-Null, so a failed
        install printed nothing but '[X] pip install failed'."""
        content = INSTALLER_WINDOWS.read_text()
        idx = _find_first_code_line(content, "pip", "install", "main.zip", exclude="uninstall")
        assert idx is not None, "install.ps1 must have the main pip install line"
        line = content.split("\n")[idx].strip()
        assert "Out-Null" not in line, f"the main pip install output is discarded: {line!r}"
        match = re.match(r"\$(\w+)\s*=\s*&\s*\$python\s+-m\s+pip\s+install\b", line)
        assert match, f"the main pip install output must be captured into a variable: {line!r}"
        failure = content.index("[X] pip install failed")
        branch = content[failure : content.index("return", failure)]
        assert f"${match.group(1)}" in branch, "the failure branch must print the captured pip output"
