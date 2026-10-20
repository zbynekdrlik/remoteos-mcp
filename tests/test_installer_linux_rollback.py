"""install-linux.sh: what a failed run leaves running (#12 review round 7).

A failed proof or an interrupted switch may leave only a PROVEN install running: one that answers
/health with its files' version and refuses a request without the key. The rollback re-proves
what it restarts (the files it restores may not be what the process ran, e.g. a key file changed
under it), and the flags that decide "nothing was switched" are pinned. Same fake box
(tests/linux_installer_harness.py).
"""

from __future__ import annotations

from pathlib import Path

from linux_installer_harness import K_EXPLICIT, K_UNIT, LinuxBox, linux_box, output

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
