"""Installer preflight, rollback and full pip error output (#12, Ubuntu 26.04).

Live on strih-pp (Ubuntu 26.04, Python 3.14, Debian pip 25.1.1), the canonical
install-linux.sh upgrade failed. The pinned set has to replace apt-owned
python3-* packages (cryptography, PyJWT, psutil, ...), and Debian's pip refuses
to uninstall them. Three defects sat on that path:

1. There was no --ignore-installed, so pip tried to uninstall the apt copies.
2. `pip install ... | tail -3 || true` hid the real error.
3. The pre-uninstall of remoteos-mcp/fastmcp/fastmcp-slim ran BEFORE anything
   proved the new install could succeed. A failed install left the box with
   NO remoteos package, and the service survived only from memory.

These tests run the real installers against tests/fake_pip.py, a stateful
Debian-pip stand-in with a fake site-packages. They check end to end the
preflight, the rollback, the restored bytes, the import check, an
interrupted run and one pinned commit of main.
Hermetic: no root, no network, and the work dir lands in tmp_path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from installer_harness import (
    GIT_URL,
    INSTALLER_LINUX,
    INSTALLER_MACOS,
    INSTALLER_WINDOWS,
    NEW_VERSION,
    OLD_FASTMCP_SLIM,
    OLD_VERSION,
    PINNED_CONSTRAINTS,
    FakePipBox,
    _create_linux_shims,
    _create_macos_shims,
    _find_first_code_line,
    _make_shim,
    _prepare_linux_installer,
    _prepare_macos_installer,
    _run_installer,
    health_shim,
    install_dep,
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


def _uninstalled_names(box: FakePipBox) -> set[str]:
    return {a for call in box.calls("uninstall") for a in call["argv"][1:] if not a.startswith("-")}


def _shared_block(path: Path) -> str:
    text = path.read_text()
    start = text.index("# >>> shared install block")
    return text[start : text.index("# <<< shared install block", start)]


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
        assert installs[0]["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS
        # The explicit pre-uninstall stays, between the preflight and the install.
        assert box.verbs() == ["dry-run", "uninstall", "install"], box.entries()
        assert OVERLAPPING <= _uninstalled_names(box)
        assert not {"cryptography", "PyJWT", "pyjwt"} & _uninstalled_names(box)
        assert box.work_dirs() == [], "the installer left its work dir behind"


# ---------------------------------------------------------------------------
# Linux: one commit of main for preflight, install and constraints
# ---------------------------------------------------------------------------


class TestLinuxPinnedCommit:
    def test_preflight_install_and_constraints_use_the_same_commit(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _linux_box(tmp_path)

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode == 0, _output(result)
        for call in box.calls("dry-run") + box.calls("install"):
            assert GIT_URL in call["argv"], call
            assert call["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS, call

    def test_unresolvable_main_aborts_before_anything_changes(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _linux_box(tmp_path)
        _make_shim(shim_dir / "git", 'echo "fatal: unable to access github.com" >&2\nexit 128')
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "unable to access github.com" in out, out
        assert box.calls("dry-run") == [] and box.calls("uninstall") == [], box.entries()
        assert box.files() == before


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
        assert dry_runs[0]["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS
        assert box.calls("uninstall") == [], "pip uninstall ran although the preflight failed"
        assert box.calls("install") == [], "the real install ran although the preflight failed"
        assert box.files() == before, "the box changed although the preflight failed"
        for i in range(1, 13):
            assert f"dry-run-error-line-{i:02d}" in out, f"line {i} of the pip error is missing:\n{out}"
        assert "systemctl" not in _invocations(invocations)

    def test_unreadable_installed_file_aborts_before_any_uninstall(self, tmp_path: Path) -> None:
        """No rollback copy = no upgrade: the pre-uninstall must not run."""
        assert os.geteuid() != 0, "this test needs an unprivileged user (root reads mode-000 files)"
        script, shim_dir, box, _ = _linux_box(tmp_path)
        locked = box.site / "remoteos" / "server.py"
        locked.chmod(0o000)
        try:
            result = _run_installer(script, shim_dir, tmp_path)
        finally:
            locked.chmod(0o644)
        out = _output(result)

        assert result.returncode != 0, out
        assert "rollback copy" in out, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.versions("remoteos-mcp") == [OLD_VERSION]


# ---------------------------------------------------------------------------
# Linux: pip-owned dependencies whose pin changes
# ---------------------------------------------------------------------------


class TestLinuxChangedDependencies:
    def test_changed_pip_owned_dependency_is_replaced_without_a_stale_dist_info(self, tmp_path: Path) -> None:
        """--ignore-installed writes over a pip-owned dep but keeps its old
        dist-info + RECORD.  A pip-owned dep whose pin changes must be
        pre-uninstalled; an apt-owned one never is."""
        script, shim_dir, box, _ = _linux_box(
            tmp_path, resolved=[["psutil", "7.2.2"], ["cryptography", "46.0.1"], ["click", "8.5.0"]]
        )
        install_dep(box, "psutil", "7.1.0")
        install_dep(box, "click", "8.5.0")  # already at the pin: untouched
        install_dep(box, "cryptography", "41.0.7", installer="debian")

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode == 0, out
        assert box.versions("psutil") == ["7.2.2"], out
        uninstalled = _uninstalled_names(box)
        assert "psutil" in uninstalled, box.entries()
        assert "click" not in uninstalled and "cryptography" not in uninstalled, box.entries()
        assert "41.0.7" in box.versions("cryptography"), "the apt-owned copy must stay"

    def test_failed_install_restores_a_pre_uninstalled_dependency(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _linux_box(tmp_path, resolved=[["psutil", "7.2.2"]], install_fail=True)
        install_dep(box, "psutil", "7.1.0")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.versions("psutil") == ["7.1.0"]
        assert box.files() == before

    def test_installed_version_without_record_aborts_before_anything_changes(self, tmp_path: Path) -> None:
        """pip can neither replace nor roll back a RECORD-less remoteos-mcp."""
        script, shim_dir, box, _ = _linux_box(tmp_path, upgrade=False)
        install_dep(box, "remoteos-mcp", OLD_VERSION, installer="debian")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "RECORD" in out, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.files() == before


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
        assert f"Rolled back: v{OLD_VERSION}" in out, out
        assert "systemctl restart" not in _invocations(invocations)
        assert box.work_dirs() == [], "a successful rollback must not leave the work dir behind"

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
        assert box.verbs()[:3] == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == []
        for i in range(1, 13):
            assert f"install-error-line-{i:02d}" in out, out
        assert "NOT installed" in out, out
        assert "systemctl restart" not in _invocations(invocations)

    def test_install_that_does_not_import_is_rolled_back(self, tmp_path: Path) -> None:
        """pip exits 0, but `from fastmcp import FastMCP` fails (the #12
        dropped-__init__.py class).  The service must never restart into it."""
        script, shim_dir, box, invocations = _linux_box(tmp_path, install_broken=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "does not import" in out and "FastMCP" in out, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.files() == before
        assert "systemctl restart" not in _invocations(invocations)

    def test_install_reporting_success_without_a_package_is_rolled_back(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _linux_box(tmp_path, install_noop=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "no remoteos package is installed" in out, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION]
        assert box.files() == before
        assert "systemctl restart" not in _invocations(invocations)

    def test_failed_rollback_is_loud_keeps_the_snapshot_and_does_not_restart(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _linux_box(tmp_path, install_fail=True, block_restore=True)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "Rollback FAILED" in out, out
        assert "systemctl restart" not in _invocations(invocations)
        kept = box.work_dirs()
        assert len(kept) == 1 and (kept[0] / "snapshot" / "manifest").exists(), kept
        assert f"restore {kept[0]}/snapshot" in out, out

    @pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
    def test_interrupted_install_is_rolled_back(self, tmp_path: Path, signal_name: str) -> None:
        """An ssh drop (HUP), a timeout's TERM or Ctrl-C mid-install must not
        leave the box without its package."""
        script, shim_dir, box, invocations = _linux_box(tmp_path, signal_parent=signal_name)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "Interrupted" in out, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.files() == before, "the interrupted upgrade was not rolled back"
        assert "systemctl restart" not in _invocations(invocations)
        assert box.work_dirs() == []


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
        assert box.calls("dry-run")[0]["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS
        assert GIT_URL in box.calls("install")[0]["argv"]
        assert box.work_dirs() == []

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

    def test_install_that_does_not_import_is_rolled_back(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _macos_box(tmp_path, install_broken=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)

    def test_interrupted_install_is_rolled_back(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _macos_box(tmp_path, signal_parent="HUP")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# Linux + macOS: the install logic is one shared block, not two drifting copies
# ---------------------------------------------------------------------------


class TestSharedInstallBlock:
    def test_linux_and_macos_installers_carry_the_identical_install_block(self) -> None:
        linux, macos = _shared_block(INSTALLER_LINUX), _shared_block(INSTALLER_MACOS)
        assert "rollback_install" in linux and "pip_install_remoteos" in linux
        assert linux == macos, "the shared install block differs between install-linux.sh and install.sh"


# ---------------------------------------------------------------------------
# Windows: parity of the error output only
# ---------------------------------------------------------------------------


class TestWindowsPipErrorOutput:
    def _main_install(self) -> tuple[str, str]:
        content = INSTALLER_WINDOWS.read_text()
        idx = _find_first_code_line(content, "pip", "install", "main.zip", exclude="uninstall")
        assert idx is not None, "install.ps1 must have the main pip install line"
        return content, content.split("\n")[idx].strip()

    def test_main_pip_install_output_is_kept_and_printed_on_failure(self) -> None:
        """install.ps1 piped the main pip install into Out-Null, so a failed
        install printed nothing but '[X] pip install failed'."""
        content, line = self._main_install()
        assert "Out-Null" not in line, f"the main pip install output is discarded: {line!r}"
        match = re.match(r"\$(\w+)\s*=\s*&\s*\$python\s+-m\s+pip\s+install\b", line)
        assert match, f"the main pip install output must be captured into a variable: {line!r}"
        failure = content.index("[X] pip install failed")
        branch = content[failure : content.index("return", failure)]
        assert f"${match.group(1)}" in branch, "the failure branch must print the captured pip output"

    def test_main_pip_install_exit_code_decides_failure(self) -> None:
        """A failed pip that leaves an old remoteos dist behind must still be a
        failure (and print its output) — not 'Installed v<old>'."""
        content, line = self._main_install()
        after = content[content.index(line) + len(line) :]
        match = re.match(r"\s*\$(\w+)\s*=\s*\$LASTEXITCODE\b", after)
        assert match, "the pip exit code must be captured right after the main pip install"
        condition = after[after.index("if ") : after.index("{", after.index("if "))]
        assert f"${match.group(1)}" in condition, f"the success test must use the pip exit code: {condition!r}"
