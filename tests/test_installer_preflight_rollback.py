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

The fix (0.7.0.dev24) was one install block shared by install-linux.sh and
install.sh. Since 0.7.0.dev32 (#12, design issuecomment-6293340521) Linux
installs into its own venv, built side by side and swapped in
(tests/test_installer_linux_venv.py), so the system-python block lives on in
install.sh (macOS) only, and these tests run it there.

These tests run the real installer against tests/fake_pip.py, a stateful
pip stand-in. Its fake box has two site dirs: pip's copy, and an apt copy
behind it (the Debian shape the block was built for; the block handles it on
any box). They check end to end:

- the preflight;
- the rollback: the restored bytes, what it removes and what it verifies;
- the import check;
- an interrupted or truncated run, and a run whose console goes away;
- one pinned commit of main.

Hermetic: no root, no network, and the work dir lands in tmp_path. The
helper is also checked on the test interpreter's own real dists, read-only.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from installer_harness import (
    GIT_URL,
    INSTALLER_MACOS,
    INSTALLER_WINDOWS,
    NEW_VERSION,
    OLD_FASTMCP_SLIM,
    OLD_VERSION,
    PINNED_CONSTRAINTS,
    FakePipBox,
    _create_macos_shims,
    _find_first_code_line,
    _make_shim,
    _prepare_macos_installer,
    _run_installer,
    dist_files,
    health_shim,
    install_dep,
    install_old_remoteos,
    rollback_helper,
    setup_fake_pip,
    start_installer,
    write_dist,
)

OVERLAPPING = {"remoteos-mcp", "fastmcp", "fastmcp-slim"}


def _box(tmp_path: Path, *, upgrade: bool = True, **config: object) -> tuple[Path, Path, FakePipBox, Path]:
    """install.sh on a fake box: `upgrade` = remoteos-mcp OLD_VERSION is installed."""
    script = _prepare_macos_installer(tmp_path)
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    invocations = tmp_path / "invocations.log"
    _create_macos_shims(shim_dir, invocations)
    box = setup_fake_pip(tmp_path, shim_dir, **config)
    health_shim(shim_dir, NEW_VERSION)
    if upgrade:
        install_old_remoteos(box)
    return script, shim_dir, box, invocations


def _output(result) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _invocations(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _uninstalled_names(box: FakePipBox) -> set[str]:
    return {a for call in box.calls("uninstall") for a in call["argv"][1:] if not a.startswith("-")}


# ---------------------------------------------------------------------------
# One commit of main for preflight, install and constraints
# ---------------------------------------------------------------------------


class TestBlockPinnedCommit:
    def test_preflight_install_and_constraints_use_the_same_commit(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _box(tmp_path)

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode == 0, _output(result)
        for call in box.calls("dry-run") + box.calls("install"):
            assert GIT_URL in call["argv"], call
            assert call["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS, call

    def test_unresolvable_main_aborts_before_anything_changes(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _box(tmp_path)
        _make_shim(shim_dir / "git", 'echo "fatal: unable to access github.com" >&2\nexit 128')
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "unable to access github.com" in out, out
        assert box.calls("dry-run") == [] and box.calls("uninstall") == [], box.entries()
        assert box.files() == before


# ---------------------------------------------------------------------------
# A dry-run preflight before anything is touched
# ---------------------------------------------------------------------------


class TestBlockPreflight:
    def test_failed_dry_run_aborts_before_any_uninstall_and_prints_the_full_error(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _box(tmp_path, dry_run_fail=True, error_lines=12)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        dry_runs = box.calls("dry-run")
        assert len(dry_runs) == 1, box.entries()
        argv = dry_runs[0]["argv"]
        assert "--break-system-packages" in argv and GIT_URL in argv, argv
        assert dry_runs[0]["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS
        assert box.calls("uninstall") == [], "pip uninstall ran although the preflight failed"
        assert box.calls("install") == [], "the real install ran although the preflight failed"
        assert box.files() == before, "the box changed although the preflight failed"
        for i in range(1, 13):
            assert f"dry-run-error-line-{i:02d}" in out, f"line {i} of the pip error is missing:\n{out}"
        assert "launchctl" not in _invocations(invocations)

    def test_unreadable_installed_file_aborts_before_any_uninstall(self, tmp_path: Path) -> None:
        """No rollback copy = no upgrade: the pre-uninstall must not run."""
        assert os.geteuid() != 0, "this test needs an unprivileged user (root reads mode-000 files)"
        script, shim_dir, box, _ = _box(tmp_path)
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
# Pip-owned dependencies whose pin changes
# ---------------------------------------------------------------------------


class TestBlockChangedDependencies:
    def test_changed_pip_owned_dependency_is_replaced_without_a_stale_dist_info(self, tmp_path: Path) -> None:
        """--ignore-installed writes over a pip-owned dep but keeps its old
        dist-info + RECORD.  A pip-owned dep whose pin changes must be
        pre-uninstalled; an apt-owned one never is."""
        script, shim_dir, box, _ = _box(
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
        script, shim_dir, box, _ = _box(tmp_path, resolved=[["psutil", "7.2.2"]], install_fail=True)
        install_dep(box, "psutil", "7.1.0")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.versions("psutil") == ["7.1.0"]
        assert box.files() == before

    def test_installed_version_without_record_aborts_before_anything_changes(self, tmp_path: Path) -> None:
        """pip can neither replace nor roll back a RECORD-less remoteos-mcp."""
        script, shim_dir, box, _ = _box(tmp_path, upgrade=False)
        install_dep(box, "remoteos-mcp", OLD_VERSION, installer="debian")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "RECORD" in out, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.files() == before


# ---------------------------------------------------------------------------
# A failed real install rolls back to the recorded version
# ---------------------------------------------------------------------------


class TestBlockRollback:
    def test_failed_install_after_pre_uninstall_restores_the_recorded_version(self, tmp_path: Path) -> None:
        """The install dies after the pre-uninstall (and after half-installing
        fastmcp-slim).  The box must end with exactly the old package files —
        never without a remoteos package — and the installer must exit non-zero
        without restarting the service."""
        script, shim_dir, box, invocations = _box(tmp_path, install_fail=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.verbs()[:3] == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.versions("fastmcp-slim") == [OLD_FASTMCP_SLIM], out
        assert box.files() == before, "the rollback did not restore the exact previous files and modes"
        assert f"Rolled back: v{OLD_VERSION}" in out, out
        assert "launchctl" not in _invocations(invocations)
        assert box.work_dirs() == [], "a successful rollback must not leave the work dir behind"

    def test_failed_install_prints_the_full_pip_output(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _box(tmp_path, install_fail=True, error_lines=20)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        for i in range(1, 21):
            assert f"install-error-line-{i:02d}" in out, f"line {i} of the pip error is missing:\n{out}"

    def test_failed_install_on_a_fresh_box_exits_nonzero_with_the_full_error(self, tmp_path: Path) -> None:
        """Nothing was installed before: nothing to roll back, but the failure
        must still be loud and complete."""
        script, shim_dir, box, invocations = _box(tmp_path, upgrade=False, install_fail=True)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.verbs()[:3] == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == []
        for i in range(1, 13):
            assert f"install-error-line-{i:02d}" in out, out
        assert "NOT installed" in out, out
        assert "launchctl" not in _invocations(invocations)

    def test_install_that_does_not_import_is_rolled_back(self, tmp_path: Path) -> None:
        """pip exits 0, but `from fastmcp import FastMCP` fails (the #12
        dropped-__init__.py class).  The service must never restart into it."""
        script, shim_dir, box, invocations = _box(tmp_path, install_broken=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "does not import" in out and "FastMCP" in out, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)

    def test_install_reporting_success_without_a_package_is_rolled_back(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _box(tmp_path, install_noop=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "no remoteos package is installed" in out, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION]
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)

    @pytest.mark.parametrize(
        "block",
        [
            "file",  # a plain file where the remoteos/ package dir must go
            "dir",  # a directory where remoteos/server.py must go (copy2 would copy INTO it)
            "partial",  # a RECORD-less half-written dist-info nothing can remove
        ],
    )
    def test_failed_rollback_is_loud_keeps_the_snapshot_and_does_not_restart(
        self, tmp_path: Path, block: str
    ) -> None:
        script, shim_dir, box, invocations = _box(
            tmp_path, install_fail=True, block_restore=block, resolved=[["newdep", "1.0"]]
        )

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "Rollback FAILED" in out, out
        assert "launchctl" not in _invocations(invocations)
        kept = box.work_dirs()
        assert len(kept) == 1 and (kept[0] / "snapshot" / "manifest").exists(), kept
        assert f"restore {kept[0]}/snapshot" in out, out

    @pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
    def test_interrupted_install_is_rolled_back(self, tmp_path: Path, signal_name: str) -> None:
        """An ssh drop (HUP), a timeout's TERM or Ctrl-C mid-install must not
        leave the box without its package."""
        script, shim_dir, box, invocations = _box(tmp_path, signal_parent=signal_name)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "Interrupted" in out, out
        assert box.versions("remoteos-mcp") == [OLD_VERSION], out
        assert box.files() == before, "the interrupted upgrade was not rolled back"
        assert "launchctl" not in _invocations(invocations)
        assert box.work_dirs() == []


# ---------------------------------------------------------------------------
# macOS: the upgrade path as a whole
# ---------------------------------------------------------------------------


class TestMacosUpgradePath:
    def test_upgrade_runs_preflight_then_pre_uninstall_then_install(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _box(tmp_path)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode == 0, out
        assert box.verbs() == ["dry-run", "uninstall", "install"], box.entries()
        assert box.versions("remoteos-mcp") == [NEW_VERSION]
        assert box.calls("dry-run")[0]["PIP_CONSTRAINT"] == PINNED_CONSTRAINTS
        assert GIT_URL in box.calls("install")[0]["argv"]
        assert box.work_dirs() == []
        # A fresh mac (no config yet) gets a new 32-char key — the real key line runs.
        key = json.loads((tmp_path / "dot-remoteos-mcp" / "config.json").read_text())["auth_key"]
        assert re.fullmatch(r"[A-Za-z0-9]{32}", key), key

    def test_failed_dry_run_aborts_before_any_uninstall_and_prints_the_full_error(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _box(tmp_path, dry_run_fail=True)
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
        script, shim_dir, box, invocations = _box(tmp_path, install_fail=True)
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
        script, shim_dir, box, invocations = _box(tmp_path, install_broken=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)

    def test_interrupted_install_is_rolled_back(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _box(tmp_path, signal_parent="HUP")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# The rollback is armed by STATE, not by a list of signals
# ---------------------------------------------------------------------------


def _run_and_drop_console(script: Path, shim_dir: Path, tmp_path: Path) -> int:
    """Run the installer and close its console (stdout+stderr pipe) right after
    the snapshot line — a dropped `ssh box 'curl … | sudo bash'` has no tty and
    sends no SIGHUP, the installer just writes into a dead pipe.  The fake pip's
    real install waits for the `console-closed` marker (no timing race)."""
    proc = start_installer(script, shim_dir, tmp_path)
    assert proc.stdout is not None
    for line in proc.stdout:
        if b"for rollback" in line:
            break
    proc.stdout.close()
    (tmp_path / "console-closed").touch()
    return proc.wait(timeout=60)


class TestBlockLostConsole:
    def test_console_lost_during_a_failing_install_still_rolls_back(self, tmp_path: Path) -> None:
        script, shim_dir, box, invocations = _box(
            tmp_path, install_fail=True, wait_for_file=str(tmp_path / "console-closed")
        )
        before = box.files()

        returncode = _run_and_drop_console(script, shim_dir, tmp_path)

        assert returncode != 0
        assert box.versions("remoteos-mcp") == [OLD_VERSION], box.entries()
        assert box.files() == before, "the box was left half-upgraded after the console went away"
        assert "launchctl" not in _invocations(invocations)

    def test_console_lost_during_a_successful_install_rolls_back_instead_of_stopping_half_way(
        self, tmp_path: Path
    ) -> None:
        """The install itself works, but the run cannot finish (verify, report):
        an unfinished upgrade is no upgrade — the old version stays, consistent
        with the still-running service."""
        script, shim_dir, box, invocations = _box(tmp_path, wait_for_file=str(tmp_path / "console-closed"))
        before = box.files()

        returncode = _run_and_drop_console(script, shim_dir, tmp_path)

        assert returncode != 0
        assert box.files() == before
        assert "launchctl" not in _invocations(invocations)

    @pytest.mark.parametrize("signal_name", ["HUP", "INT", "TERM"])
    def test_group_signal_while_pip_writes_a_wheel_rolls_back_cleanly(self, tmp_path: Path, signal_name: str) -> None:
        """Ctrl-C (SIGINT), a tty hang-up (SIGHUP) or a killpg timeout (SIGTERM)
        hit pip itself, not only the installer.  A pip killed mid-wheel leaves
        RECORD-less files nothing can remove; shielded, it finishes the wheel and
        the installer rolls back over complete RECORDs."""
        script, shim_dir, box, invocations = _box(tmp_path, partial_signal=signal_name)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "Interrupted" in out, out
        assert box.files() == before, "a half-written wheel survived the rollback"
        assert "launchctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# A truncated `curl | bash` download never half-runs
# ---------------------------------------------------------------------------


def _truncate_after(script: Path, predicate) -> None:
    lines = script.read_text().split("\n")
    cut = next(i for i, line in enumerate(lines) if predicate(line)) + 1
    script.write_text("\n".join(lines[:cut]) + "\n")


class TestBlockTruncatedDownload:
    def test_cut_off_right_after_the_pre_uninstall_changes_nothing(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _box(tmp_path)
        # The top-level call (unindented), not the definition or the rollback's call.
        _truncate_after(script, lambda line: line.startswith("uninstall_overlapping") and "()" not in line)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.files() == before, _output(result)

    def test_cut_off_after_the_install_does_not_report_success(self, tmp_path: Path) -> None:
        """Ending after a verified install but before config/restart/health: an
        unattended deploy must not read exit 0 as a finished install."""
        script, shim_dir, box, invocations = _box(tmp_path)
        _truncate_after(script, lambda line: line.startswith("# <<< shared install block"))

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert "launchctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# What the rollback removes and what it verifies
# ---------------------------------------------------------------------------


class TestBlockRollbackSet:
    def test_rollback_is_not_stopped_by_a_package_pip_refuses_to_uninstall(self, tmp_path: Path) -> None:
        """aaa-dep's pip copy shadows an apt copy: once the pre-uninstall removed
        it, pip sees the apt copy — which it refuses to uninstall.  That must not
        stop the rollback from removing zzz-dep's half-installed new copy."""
        script, shim_dir, box, _ = _box(
            tmp_path, resolved=[["zzz-dep", "2.0"], ["aaa-dep", "11.0"]], install_fail=True, fail_after=2
        )
        install_dep(box, "aaa-dep", "5.0", installer="debian")
        install_dep(box, "aaa-dep", "10.0")
        install_dep(box, "zzz-dep", "1.0")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.versions("zzz-dep") == ["1.0"], "a stale zzz-dep 2.0 dist-info survived the rollback"
        assert box.versions("aaa-dep") == ["10.0", "5.0"]
        assert box.files() == before

    def test_rollback_removes_what_the_failed_install_added(self, tmp_path: Path) -> None:
        """A new dep, and a pip copy overlaying an apt one, written by the failed
        install: the rollback takes them out again."""
        script, shim_dir, box, _ = _box(
            tmp_path, resolved=[["newdep", "1.0"], ["cryptography", "46.0.1"]], install_fail=True
        )
        install_dep(box, "cryptography", "41.0.7", installer="debian")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)

        assert result.returncode != 0, _output(result)
        assert box.versions("newdep") == []
        assert box.versions("cryptography") == ["41.0.7"]
        assert box.files() == before

    def test_changed_dependency_owned_by_another_installer_aborts_before_anything_changes(
        self, tmp_path: Path
    ) -> None:
        """A uv/conda-installed dep with a RECORD whose pin changes: the overlay
        would overwrite it, and neither the pre-uninstall nor the snapshot may
        touch another installer's package — so refuse, untouched."""
        script, shim_dir, box, _ = _box(tmp_path, resolved=[["uvdep", "2.0"]])
        install_dep(box, "uvdep", "1.0", installer="uv")
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "uvdep" in out and "uv" in out, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.files() == before

    def test_failed_pre_uninstall_rolls_back_instead_of_installing_over_stale_files(self, tmp_path: Path) -> None:
        script, shim_dir, box, _ = _box(tmp_path, uninstall_fail_multi=True)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "Input/output error" in out, out
        assert box.calls("install") == [], "the install ran although the pre-uninstall failed"
        assert box.files() == before

    @pytest.mark.parametrize("damage", ["import", "version"])
    def test_rollback_that_does_not_bring_back_a_working_version_is_a_failure(
        self, tmp_path: Path, damage: str
    ) -> None:
        """The restore 'succeeds' from a damaged rollback copy: the rollback's own
        version + import check must catch it, keep the work dir, exit non-zero."""
        script, shim_dir, box, invocations = _box(tmp_path, install_fail=True, corrupt_snapshot=damage)

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "not back in a working state" in out, out
        assert len(box.work_dirs()) == 1, "the work dir must be kept for manual recovery"
        assert "launchctl" not in _invocations(invocations)


# ---------------------------------------------------------------------------
# No upgrade without a complete rollback copy
# ---------------------------------------------------------------------------


class TestBlockSnapshotCompleteness:
    def test_truncated_record_of_the_installed_version_aborts_before_anything_changes(self, tmp_path: Path) -> None:
        """Its RECORD does not list its own dist-info: the rollback copy could
        not bring the version back, so nothing may be uninstalled."""
        script, shim_dir, box, _ = _box(tmp_path, upgrade=False)
        write_dist(box.site, "remoteos-mcp", OLD_VERSION, dist_files("remoteos-mcp", OLD_VERSION),
                   record_lists_dist_info=False)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.files() == before

    def test_overlapping_package_without_record_aborts_before_anything_changes(self, tmp_path: Path) -> None:
        """pip would refuse to uninstall a RECORD-less fastmcp-slim and abort the
        rest of the pre-uninstall."""
        script, shim_dir, box, _ = _box(tmp_path, upgrade=False)
        write_dist(box.site, "remoteos-mcp", OLD_VERSION, dist_files("remoteos-mcp", OLD_VERSION))
        write_dist(box.site, "fastmcp-slim", OLD_FASTMCP_SLIM, dist_files("fastmcp-slim", OLD_FASTMCP_SLIM),
                   record=False)
        before = box.files()

        result = _run_installer(script, shim_dir, tmp_path)
        out = _output(result)

        assert result.returncode != 0, out
        assert "fastmcp-slim" in out and "RECORD" in out, out
        assert box.calls("uninstall") == [] and box.calls("install") == [], box.entries()
        assert box.files() == before


# ---------------------------------------------------------------------------
# The rollback helper against a REAL environment (the test interpreter's own)
# ---------------------------------------------------------------------------


class TestRollbackHelperOnRealMetadata:
    """The helper reads installed dists through importlib.metadata (RECORD +
    INSTALLER), not by parsing pip's human output — check it on real dists."""

    def _helper(self, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
        helper = rollback_helper(tmp_path / "rollback.py", INSTALLER_MACOS)
        return subprocess.run([sys.executable, "-B", str(helper), *args], capture_output=True, text=True, timeout=60)

    def test_snapshot_copies_exactly_the_record_listed_files_of_a_real_dist(self, tmp_path: Path) -> None:
        dist = importlib.metadata.distribution("pytest")
        expected = sorted(
            os.path.normpath(str(dist.locate_file(f)))
            for f in dist.files or []
            if os.path.isfile(dist.locate_file(f))
        )
        assert expected, "pytest has no RECORD-listed files to check against"

        result = self._helper(tmp_path, "snapshot", str(tmp_path / "snap"), "pytest")

        assert result.returncode == 0, result.stderr
        manifest = sorted(p for p in (tmp_path / "snap" / "manifest").read_text().split("\n") if p)
        assert manifest == expected
        assert any(p.endswith(".dist-info/METADATA") for p in manifest)
        for path in manifest:
            assert (tmp_path / "snap" / "files").joinpath(path.lstrip("/")).read_bytes() == Path(path).read_bytes()

    def test_plan_lists_a_real_pip_owned_dist_whose_pinned_version_changes(self, tmp_path: Path) -> None:
        dist = importlib.metadata.distribution("pytest")
        assert (dist.read_text("INSTALLER") or "").strip() == "pip", "needs a pip-installed pytest"
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"install": [
            {"metadata": {"name": "pytest", "version": "0.0.1"}},
            {"metadata": {"name": "not-installed-anywhere", "version": "1.0"}},
        ]}))

        result = self._helper(tmp_path, "plan", str(report), "", str(tmp_path / "plan"))

        assert result.returncode == 0, result.stderr
        assert (tmp_path / "plan" / "changed.txt").read_text().split() == ["pytest"]
        assert (tmp_path / "plan" / "added.txt").read_text().split() == ["not-installed-anywhere"]


# ---------------------------------------------------------------------------
# Windows: parity of the error output only
# ---------------------------------------------------------------------------


class TestWindowsPipErrorOutput:
    def _main_install(self) -> tuple[str, str]:
        content = INSTALLER_WINDOWS.read_text()
        idx = _find_first_code_line(content, "pip", "install", "archive/", exclude="uninstall")
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
