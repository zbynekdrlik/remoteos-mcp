"""install-linux.sh: what a failed run leaves running (#12 review round 7).

A failed proof or an interrupted switch may leave only a PROVEN install running: one that answers
/health with its files' version and refuses a request without the key. The rollback re-proves
what it restarts (the files it restores may not be what the process ran, e.g. a key file changed
under it), and the flags that decide "nothing was switched" are pinned. Same fake box
(tests/linux_installer_harness.py).
"""

from __future__ import annotations

import stat
from pathlib import Path

from linux_installer_harness import (
    K_ENV,
    K_EXPLICIT,
    K_UNIT,
    MAIN_SOURCE_ID,
    SOURCE_VERSION,
    LinuxBox,
    linux_box,
    output,
)

INSTALLER = Path(__file__).resolve().parent.parent / "install-linux.sh"


def _pending(box: LinuxBox) -> bool:
    return (box.config_dir / ".switch-pending").exists()


def _stopped_and_disabled(box: LinuxBox) -> None:
    state = box.state()
    assert not state["active"] and not state["enabled"], state


class TestARollbackProvesWhatItRestarts:
    def test_previous_files_with_authentication_off_are_stopped_never_rolled_back_into(
        self, tmp_path: Path
    ) -> None:
        """The EnvironmentFile changed under the running, proven process (an empty key switches
        authentication OFF). Restoring it after a failed proof would start a full shell on
        0.0.0.0 with no key."""
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        box.env_file.write_text("REMOTEOS_AUTH_KEY=\n")
        box.env_file.chmod(0o600)
        box.configure(service={"initialize_code": 500})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Rolled back:" not in output(result)
        _stopped_and_disabled(box)

    def test_a_key_rotation_whose_proof_fails_puts_the_old_key_back(self, tmp_path: Path) -> None:
        """Same source, only the key files change: they are this run's switch and go back."""
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        old_key = box.config_key()
        box.configure(service={"initialize_code": 500})

        result = box.run(REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)

        assert result.returncode != 0, output(result)
        assert "Nothing was switched" not in output(result)
        assert box.config_key() == old_key and box.env_file_key() == old_key
        state = box.state()
        assert state["active"] and state["service"]["key"] == old_key, state

    def test_a_pending_key_change_on_an_unchanged_venv_is_never_taken_as_proven(self, tmp_path: Path) -> None:
        """enable-only rotated the key (the process still runs the old one), then a restart run's
        proof fails: the restarted new-key state was never proven."""
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        assert box.run(REMOTEOS_MCP_POLICY="enable-only", REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT).returncode == 0
        box.configure(service={"initialize_code": 500})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Nothing was switched" not in output(result)
        _stopped_and_disabled(box)
        assert _pending(box)


class TestNothingSwitched:
    def test_a_desktop_box_without_a_system_gi_is_left_as_it_ran(self, tmp_path: Path) -> None:
        """No gi to link means nothing was switched: no restart, no "Rolled back"."""
        box = linux_box(tmp_path, graphical=True)
        assert box.run().returncode == 0
        restarts = box.state()["restarts"]
        box.configure(service={"initialize_code": 500})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Nothing was switched" in output(result)
        assert box.state()["restarts"] == restarts and box.state()["active"]


class TestNoKeyCopyLeftBehind:
    def test_a_signal_while_a_key_file_is_written_leaves_no_temp_copy(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        box.inject_after('cat >"$tmp"', "kill -HUP $$")

        result = box.run()

        assert result.returncode != 0, output(result)
        leftovers = [p.name for p in box.config_dir.iterdir()
                     if p.name.startswith(("config.json.", "remoteos-mcp.env."))]
        leftovers += [p.name for p in box.unit_path.parent.iterdir() if p.name.startswith("remoteos-mcp.service.")]
        assert leftovers == [], "a temp copy of a key file (or the unit) is left behind"


class TestPreflightAndMessages:
    def test_a_restart_run_checks_for_ss_before_it_changes_anything(self) -> None:
        """The proof needs `ss`; without the preflight a box with no iproute2 would fail the proof
        AFTER the switch, saying another process holds the port."""
        lines = [line.strip() for line in INSTALLER.read_text().splitlines()]
        preflight = next(i for i, line in enumerate(lines) if "command -v ss" in line)
        assert "nothing was changed on this box" in " ".join(lines[preflight:preflight + 2])
        for later in ('exec 9>"$LOCK_FILE"', "fetch_source", "arm"):
            assert preflight < lines.index(later), later

    def test_an_enable_only_run_never_says_it_restarts(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        assert box.run(REMOTEOS_MCP_POLICY="enable-only", REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT).returncode == 0
        restarts = box.state()["restarts"]

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")

        assert result.returncode == 0, output(result)
        assert box.state()["restarts"] == restarts
        assert "restarting" not in output(result).lower(), output(result)


def _reboot_into_an_enable_only_switch(box: LinuxBox) -> None:
    """A proven cam, an enable-only switch to dev33 (pending), then the cam reboots: systemd
    starts what the files say, and dev33 runs fine."""
    assert box.run().returncode == 0
    box.add_source("main", "bbb2222", "0.7.0.dev33")
    assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0
    assert _pending(box)
    box.boot()
    assert box.state()["service"]["version"] == "0.7.0.dev33"


class TestAPendingSwitchThatHasRunSince:
    """Review round 8: the pending mark means "not started since the switch". Once the unit has
    started (a reboot, a crash restart), the running process IS what the files say: provable."""

    def test_a_failing_restart_run_rolls_back_to_it(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        _reboot_into_an_enable_only_switch(box)
        box.add_source("main", "ccc3333", "0.7.0.dev34")
        box.configure(service={"initialize_code": 500, "only_version": "0.7.0.dev34"})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Rolled back" in output(result)
        state = box.state()
        assert state["active"] and state["enabled"], "a working, rebooted cam was stopped and disabled"
        assert state["service"]["version"] == "0.7.0.dev33" and not _pending(box)

    def test_a_no_op_enable_only_run_leaves_it_proven(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        _reboot_into_an_enable_only_switch(box)

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")

        assert result.returncode == 0, output(result)
        assert not _pending(box), "a stale mark: the switch has run since the reboot"

    def test_a_proven_enable_only_box_loses_the_old_system_copy(self, tmp_path: Path) -> None:
        """Review round 14: the copy went only after a restart run's proof, which the cams (an
        enable-only convention) never get. A proven, no-op enable-only run runs the venv: the
        system copy protects nothing."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0
        assert box.pip.versions("remoteos-mcp") != [], "enable-only removed the copy the legacy process runs"
        box.boot()  # the cam reboots into the venv

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")

        assert result.returncode == 0, output(result)
        assert box.pip.versions("remoteos-mcp") == [], "the old system-python copy stays forever"
        assert box.state()["active"]


class TestAServerThatTakesAMomentToBind:
    """Review round 8: the proof and the re-proof poll; a real server binds a moment after
    `systemctl restart` (Type=simple is active right after the fork)."""

    def test_a_restored_install_that_comes_up_slowly_is_kept(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        box.configure(service={"initialize_code": 500, "only_version": SOURCE_VERSION, "health_misses": 2})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Rolled back" in output(result) and "proved itself" in output(result)
        state = box.state()
        assert state["active"] and state["enabled"] and state["service"]["python"].endswith("/shims/python3")

    def test_a_new_install_that_comes_up_slowly_proves_itself(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(service={"health_misses": 2})

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.state()["active"]


class TestTheReRunAfterAStop:
    def test_a_switch_stopped_by_a_failed_proof_is_still_pending_on_the_re_run(self, tmp_path: Path) -> None:
        """Review round 9: the run's own restart made a new InvocationID, so the kept mark must
        name the stopped unit's, or the re-run calls it "has run since" (a reboot's message)."""
        box = linux_box(tmp_path)
        box.configure(service={"initialize_code": 500})
        assert box.run().returncode != 0 and _pending(box)
        box.configure(service={})

        result = box.run()

        assert result.returncode == 0, output(result)
        assert "has run since" not in output(result)
        assert "An earlier switch was never proven: restarting into what the files say" in output(result)
        assert box.state()["active"] and not _pending(box)


class TestTheRunningInstallIsReadWithPatience:
    """Review round 10: the read of what runs (before a restart run's switch, at a no-op
    enable-only run's end) gives an active unit that does not answer yet the same polls as the
    proof; one missed poll must not turn a working box into "nothing proven"."""

    def test_a_proven_box_that_misses_one_poll_is_still_rolled_back_to(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(service={"initialize_code": 500, "only_version": SOURCE_VERSION, "health_misses": 1})
        box.install_legacy(K_UNIT)  # boots: the legacy agent misses its first /health poll

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Rolled back" in output(result)
        state = box.state()
        assert state["active"] and state["enabled"], "a working agent was stopped and disabled"

    def test_a_no_op_enable_only_run_that_misses_one_poll_leaves_the_cam_proven(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(service={"health_misses": 1})
        box.install_camera_box(K_ENV, mode="headless", source_id=MAIN_SOURCE_ID, version=SOURCE_VERSION,
                               user="root")

        result = box.run(REMOTEOS_MCP_POLICY="enable-only", REMOTEOS_MCP_MODE="headless", REMOTEOS_MCP_USER="root")

        assert result.returncode == 0, output(result)
        assert not _pending(box), "one missed poll left a sticky pending mark on a working cam"


class TestAProxiedBox:
    def test_the_loopback_checks_never_go_through_a_proxy(self, tmp_path: Path) -> None:
        """Review round 11: curl sends http://127.0.0.1 to an http_proxy from the environment
        (pip needs it, so the installer keeps it). A remote proxy cannot reach the box, so every
        check failed and a working agent was stopped; a local one would have seen the key."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)

        result = box.run(http_proxy="http://proxy.invalid:3128")

        assert result.returncode == 0, output(result)
        state = box.state()
        assert state["active"] and state["enabled"] and state["service"]["version"] == SOURCE_VERSION


class TestNeverFromInsideTheUnit:
    """Review round 13: from the agent's own MCP Shell the run is inside remoteos-mcp.service's
    cgroup, so the switch's restart (or the uninstaller's stop) kills the run half-way. It is
    refused before anything changes."""

    def test_an_install_from_the_agent_s_own_shell_changes_nothing(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        box.run_inside_the_service()
        before = box.snapshot()

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "nothing was changed" in output(result) and "SSH" in output(result)
        assert box.snapshot() == before and box.curl_requests() == []
        assert box.state()["active"]

    def test_an_uninstall_from_the_agent_s_own_shell_changes_nothing(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        box.run_inside_the_service()
        before = box.snapshot()

        result = box.run(script=box.uninstaller())

        assert result.returncode != 0, output(result)
        assert "nothing was changed" in output(result)
        assert box.snapshot() == before and box.state()["active"]


class TestTheBuildIsReadableByTheServiceAccount:
    def test_every_directory_and_file_of_a_new_build_is_readable_by_another_account(self, tmp_path: Path) -> None:
        """Review round 13: a desktop unit runs as the desktop user. `mktemp -d` makes the build
        0700 and the lock's umask 077 would make pip's files 0600; the installer's chmod 755 and
        `umask 022` keep the build readable (the fake systemd never runs as User=, so only this
        pins them)."""
        box = linux_box(tmp_path, graphical=True)

        assert box.run().returncode == 0
        build = box.venv_target()
        bad = []
        for path in [build, *build.rglob("*")]:
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            need = 0o005 if stat.S_ISDIR(mode) else 0o004
            if mode & need != need:
                bad.append(f"{oct(mode & 0o777)} {path.relative_to(build)}")
        assert bad == [], bad[:8]
