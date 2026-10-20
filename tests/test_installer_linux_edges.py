"""install-linux.sh: the edges of the switch, found by the #12 review rounds (rounds 1-3).

The core behaviour (layout, key precedence, migration, failed build, proof, idempotence,
camera-box adoption) is in tests/test_installer_linux_venv.py; this module holds what the review
rounds added: a lost console or a signal at every step, foreign changes to our build, the
system gi, camera-box's inputs, guards, a pending switch, the session's display, exported
names. Same fake box (tests/linux_installer_harness.py).
"""

from __future__ import annotations

import fcntl
import shutil
from pathlib import Path

import pytest
from fake_linux_tools import _venv_purelib
from fake_pip import dist_files, write_dist
from linux_installer_harness import (
    CB_SOURCE_ID,
    CB_VERSION,
    K_ENV,
    K_UNIT,
    LEGACY_VERSION,
    MAIN_SOURCE_ID,
    SERVICE_UID,
    SOURCE_VERSION,
    LinuxBox,
    builds,
    exec_start,
    linux_box,
    output,
    restarted,
    write_config,
)

# ---------------------------------------------------------------------------
# Review round 1: the edges of the switch
# ---------------------------------------------------------------------------


def _assert_legacy_box_restored(box: LinuxBox, unit_before: str) -> None:
    assert box.unit() == unit_before, "the previous unit was not restored"
    assert not box.env_file.exists() and not box.venv_link.is_symlink() and builds(box) == []
    assert box.pip.versions("remoteos-mcp") == [LEGACY_VERSION], "the legacy copy was removed"
    service = box.state()["service"]
    assert service["python"].endswith("/shims/python3") and service["key"] == K_UNIT
    assert not (box.config_dir / ".switch-pending").exists(), "a restored, proven box is marked pending"


class TestLostConsoleDuringTheProof:
    def test_a_console_lost_while_a_failing_proof_runs_is_rolled_back(self, tmp_path: Path) -> None:
        """The proof's own failure path must roll back with no console (round 1: health_failed
        disarmed first, and its next echo killed bash by SIGPIPE before anything was restored)."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        unit_before = box.unit()
        release = tmp_path / "console-closed"
        box.configure(service={"unauth_code": 200, "health_waits_for": str(release)})

        returncode = box.run_and_drop_console("Proving the install", release)

        assert returncode != 0
        _assert_legacy_box_restored(box, unit_before)

    def test_a_console_lost_while_a_passing_proof_runs_keeps_the_proven_install(self, tmp_path: Path) -> None:
        """The proof decides, not the console: a write after it must not undo a proven switch."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        release = tmp_path / "console-closed"
        box.configure(service={"health_waits_for": str(release)})

        box.run_and_drop_console("Proving the install", release)

        assert exec_start(box) in box.unit().splitlines()
        assert box.venv_link.is_symlink() and box.env_file_key() == K_UNIT
        service = box.state()["service"]
        assert service["python"] == str(box.venv_link / "bin" / "python")
        assert (service["version"], service["key"]) == (SOURCE_VERSION, K_UNIT)


class TestSignalMidSwap:
    @pytest.mark.parametrize(
        "after",
        ['mv -T "$VENV_LINK" "$ADOPTED_DIR"', 'ln -sfn "$NEW_BUILD" "$tmp_link"', 'mv -Tf "$tmp_link" "$VENV_LINK"'],
        ids=["dir-moved-aside", "between-the-renames", "link-swapped"],
    )
    def test_a_signal_inside_the_swap_puts_camera_box_s_venv_back(self, tmp_path: Path, after: str) -> None:
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV)
        unit_before = box.unit()
        venv_before = {k: v for k, v in box.snapshot().items() if k.startswith(str(box.venv_link))}
        box.inject_after(after, "kill -HUP $$")

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Interrupted" in output(result)
        assert not box.venv_link.is_symlink() and box.venv_link.is_dir()
        assert {k: v for k, v in box.snapshot().items() if k.startswith(str(box.venv_link))} == venv_before
        assert box.unit() == unit_before
        assert builds(box) == []
        service = box.state()["service"]
        assert (service["version"], service["key"]) == (CB_VERSION, K_ENV)

    def test_a_signal_inside_the_swap_puts_the_previous_build_back(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        old_build = box.venv_target()
        box.add_source("main", "def5678", "0.7.0.dev33")
        box.inject_after('ln -sfn "$NEW_BUILD" "$tmp_link"', "kill -TERM $$")

        result = box.run()

        assert result.returncode != 0, output(result)
        assert box.venv_target() == old_build and builds(box) == [old_build]
        assert box.state()["service"]["version"] == SOURCE_VERSION


class TestForeignChangesToOurBuild:
    def _camera_box_upgrades_in_place(self, box: LinuxBox, version: str, marker: bool) -> None:
        """What camera-box's lib (before it delegates) does to the venv it finds at
        /opt/remoteos-mcp-venv: `pip install --upgrade` of its pin in place, its marker, a
        restart."""
        target = box.venv_target()
        site = Path(_venv_purelib(target))
        for old in [site / "remoteos", *site.glob("remoteos_mcp-*.dist-info")]:
            shutil.rmtree(old)
        write_dist(site, "remoteos-mcp", version, dist_files("remoteos-mcp", version))
        if marker:
            (target / ".camera-box-source").write_text(CB_SOURCE_ID + "\n")
        box.boot()

    def test_a_build_camera_box_s_lib_upgraded_in_place_is_rebuilt(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        self._camera_box_upgrades_in_place(box, CB_VERSION, marker=True)
        assert box.state()["service"]["version"] == CB_VERSION

        result = box.run()

        assert result.returncode == 0, output(result)
        assert len(box.venv_installs()) == 2, "a build another installer changed was taken as unchanged"
        assert box.state()["service"]["version"] == SOURCE_VERSION

    def test_a_build_whose_version_is_not_its_source_s_is_rebuilt(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        self._camera_box_upgrades_in_place(box, "0.7.0.dev1", marker=False)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert len(box.venv_installs()) == 2
        assert box.state()["service"]["version"] == SOURCE_VERSION


class TestDesktopGi:
    def test_a_fresh_desktop_box_links_the_gi_its_desktop_tools_just_installed(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical=True)
        gi = box.system_extra / "gi"
        box.shim("apt-get", f'case " $* " in *" python3-gi "*) mkdir -p "{gi}"; '
                            f'echo "SYSTEM_GI = True" > "{gi}/__init__.py" ;; esac\nexit 0\n')

        result = box.run()

        assert result.returncode == 0, output(result)
        probe = box.venv_python("import gi; print(gi.SYSTEM_GI)")
        assert probe.returncode == 0 and probe.stdout.strip() == "True", probe.stderr

    def test_an_unchanged_desktop_build_gets_gi_once_the_system_has_it(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical=True)
        assert box.run().returncode == 0
        assert box.venv_python("import gi").returncode != 0
        (box.system_extra / "gi").mkdir()
        (box.system_extra / "gi" / "__init__.py").write_text("SYSTEM_GI = True\n")

        result = box.run()

        assert result.returncode == 0, output(result)
        assert len(box.venv_installs()) == 1, "linking gi rebuilt the venv"
        assert box.venv_python("import gi").returncode == 0
        assert restarted(box), "the running service never saw the linked gi"

    def test_only_gi_is_borrowed_from_the_system_python(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical=True)
        for name in ("gi", "aptonly"):
            (box.system_extra / name).mkdir()
            (box.system_extra / name / "__init__.py").write_text("")

        assert box.run().returncode == 0

        assert box.venv_python("import gi").returncode == 0
        assert box.venv_python("import aptonly").returncode != 0, "the venv sees every system package"


class TestCameraBoxInputs:
    def test_the_mode_and_the_account_can_be_given(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)  # no graphical session: the session check alone says headless

        result = box.run(REMOTEOS_MCP_MODE="desktop", REMOTEOS_MCP_USER="newlevel")

        assert result.returncode == 0, output(result)
        lines = box.unit().splitlines()
        assert "User=newlevel" in lines and "Environment=DISPLAY=:0" in lines
        assert "Environment=XDG_RUNTIME_DIR=/run/user/1000" in lines

    @pytest.mark.parametrize(
        "env",
        [{"REMOTEOS_MCP_MODE": "kiosk"}, {"REMOTEOS_MCP_USER": "no-such-account"},
         {"REMOTEOS_MCP_USER": "bad;name"}, {"REMOTEOS_MCP_POLICY": "sometimes"}],
        ids=["mode", "missing-account", "bad-account", "policy"],
    )
    def test_an_invalid_input_is_refused_before_anything_changes(self, tmp_path: Path, env: dict) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        before = box.snapshot()

        result = box.run(**env)

        assert result.returncode != 0, output(result)
        assert next(iter(env)) in output(result)
        assert box.snapshot() == before and box.venv_installs() == []

    def test_enable_only_installs_and_enables_but_never_starts(self, tmp_path: Path) -> None:
        """camera-box's cam provisioning (`remoteos_mcp_install root headless enable-only`)."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        state_before = box.state()

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")
        out = output(result)

        assert result.returncode == 0, out
        assert exec_start(box) in box.unit().splitlines() and box.env_file_key() == K_UNIT
        assert box.state()["enabled"] and "next boot" in out
        assert not any(a[:1] in (["start"], ["restart"]) for a in box.calls("systemctl")), box.calls("systemctl")
        assert box.state()["service"] == state_before["service"], "the running service was touched"
        assert box.curl_requests() == [r for r in box.curl_requests() if "/tarball/" in r["url"]]
        assert box.pip.versions("remoteos-mcp") == [LEGACY_VERSION], "the legacy copy the old process runs went"


class TestGuards:
    @pytest.mark.parametrize(
        "where", ["config", "unit"], ids=["config-json", "legacy-unit"],
    )
    def test_an_existing_key_the_installer_cannot_carry_is_refused_never_replaced(
        self, tmp_path: Path, where: str
    ) -> None:
        """The pre-#12 installers reused ANY config.json key; minting a new one would silently
        cut the client off (#51)."""
        box = linux_box(tmp_path)
        if where == "config":
            write_config(box, "has-dash_and.dot")
        else:
            box.install_legacy("has-dash_and.dot")
        before = box.snapshot()

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "REMOTEOS_MCP_AUTH_KEY" in output(result)
        assert box.snapshot() == before and box.venv_installs() == []

    def test_a_second_run_while_one_runs_is_refused(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        before = box.snapshot()
        with open(box.lock_file, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = box.run()

        assert result.returncode != 0, output(result)
        assert "another" in output(result).lower()
        assert box.snapshot() == before and box.curl_requests() == []

    def test_a_unit_that_cannot_be_enabled_is_rolled_back(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        unit_before = box.unit()
        box.configure(service={"enable_fails": True})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "enabled" in output(result)
        _assert_legacy_box_restored(box, unit_before)


# ---------------------------------------------------------------------------
# Review round 2
# ---------------------------------------------------------------------------


def _runs_the_new_install(box: LinuxBox) -> None:
    service = box.state()["service"]
    assert service["python"] == str(box.venv_link / "bin" / "python"), service
    assert service["version"] == SOURCE_VERSION


class TestPendingSwitch:
    def test_a_restart_run_after_enable_only_restarts_into_the_new_install(self, tmp_path: Path) -> None:
        """enable-only switched the files but started nothing: the next restart run must not take
        the box as unchanged and prove the OLD process."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0
        restarts = box.state()["restarts"]

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.state()["restarts"] == restarts + 1, "the switched files never got a restart"
        _runs_the_new_install(box)
        assert box.pip.versions("remoteos-mcp") == [], "the proven switch kept the legacy copy"

    def test_enable_only_prunes_the_old_builds(self, tmp_path: Path) -> None:
        """A venv process imports through the link, which already points at the new build, so
        an old build protects nothing: the cams (enable-only) must not pile them up."""
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        old_build = box.venv_target()
        box.add_source("main", "def5678", "0.7.0.dev33")

        assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0

        assert not old_build.exists() and builds(box) == [box.venv_target()]

    def test_a_restart_run_after_a_killed_switch_restarts_into_the_new_install(self, tmp_path: Path) -> None:
        """SIGKILL (no trap runs) after the files were switched, before the restart."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        box.inject_after("systemctl daemon-reload", "kill -KILL $$")
        assert box.run().returncode != 0
        box.uninject()

        result = box.run()

        assert result.returncode == 0, output(result)
        _runs_the_new_install(box)
        assert box.state()["service"]["key"] == K_UNIT


class TestFreshBoxProofFailure:
    def test_a_fresh_box_whose_proof_fails_is_stopped_and_disabled(self, tmp_path: Path) -> None:
        """Nothing to roll back to, but a full-shell agent on 0.0.0.0 with authentication off must
        not stay up (the proof exists to catch exactly that)."""
        box = linux_box(tmp_path)
        box.configure(service={"unauth_code": 200})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "not enforced" in output(result)
        state = box.state()
        assert not state["active"] and not state["enabled"], state
        assert box.unit_path.exists() and box.env_file.exists(), "the files are kept for a re-run"


def _stopped_disabled_pending(box: LinuxBox) -> None:
    state = box.state()
    assert not state["active"] and not state["enabled"], state
    assert (box.config_dir / ".switch-pending").exists(), "the unproven switch is no longer marked pending"


class TestNeverRestartAnUnprovenState:
    """Review round 3: a rollback may only restart a PROVEN state. An unproven one (a fresh box's
    first attempt, an enable-only switch, a killed switch) is stopped, never "rolled back" into
    running: it may be an agent with authentication off."""

    def test_a_fresh_box_whose_proof_fails_twice_stays_stopped(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(service={"unauth_code": 200})
        assert box.run().returncode != 0

        result = box.run()

        assert result.returncode != 0, output(result)
        _stopped_disabled_pending(box)
        assert "Rolled back" not in output(result)

    def test_an_enable_only_switch_whose_proof_fails_is_stopped(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0
        box.configure(service={"unauth_code": 200})

        result = box.run()

        assert result.returncode != 0, output(result)
        _stopped_disabled_pending(box)

    def test_a_fresh_box_interrupted_while_switching_is_stopped(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(service={"signal_on_restart": "HUP"})

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "Interrupted" in output(result)
        _stopped_disabled_pending(box)


class TestRunningVersion:
    def test_an_unchanged_box_running_another_version_is_restarted(self, tmp_path: Path) -> None:
        """camera-box's lib (enable-only, the cams) upgraded the venv in place to our source and
        set its marker, but never restarted: the files are current, the process is not."""
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV)  # running CB_VERSION
        site = Path(_venv_purelib(box.venv_link))
        for old in [site / "remoteos", *site.glob("remoteos_mcp-*.dist-info")]:
            shutil.rmtree(old)
        write_dist(site, "remoteos-mcp", SOURCE_VERSION, dist_files("remoteos-mcp", SOURCE_VERSION))
        (box.venv_link / ".camera-box-source").write_text(MAIN_SOURCE_ID + "\n")
        restarts = box.state()["restarts"]
        assert box.state()["service"]["version"] == CB_VERSION

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.venv_installs() == []
        assert box.state()["restarts"] == restarts + 1
        assert box.state()["service"]["version"] == SOURCE_VERSION


class TestDesktopDisplay:
    def test_an_x11_session_on_another_display_gets_that_display(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical="x11", display=":1")

        assert box.run().returncode == 0

        lines = box.unit().splitlines()
        assert "Environment=DISPLAY=:1" in lines and "Environment=DISPLAY=:0" not in lines

    def test_a_wayland_session_gets_the_desktop_unit_without_a_display(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical="wayland")

        assert box.run().returncode == 0

        lines = box.unit().splitlines()
        assert not any(line.startswith("Environment=DISPLAY") for line in lines), lines
        assert f"Environment=XDG_RUNTIME_DIR=/run/user/{SERVICE_UID}" in lines
        assert "After=network.target graphical-session.target" in lines

    def test_a_re_run_keeps_the_unit_s_display(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical="x11", display=":1")
        assert box.run().returncode == 0
        box.shim("loginctl", 'if [[ "$1" == "list-sessions" ]]; then echo ""; fi\nexit 0\n')  # logged out

        assert box.run().returncode == 0

        assert "Environment=DISPLAY=:1" in box.unit().splitlines()

    def test_a_wayland_re_run_while_logged_out_keeps_no_display(self, tmp_path: Path) -> None:
        """Review round 3: a desktop unit without DISPLAY is the saved Wayland choice; :0 would
        stick for good (the existing unit's display wins on every later run)."""
        box = linux_box(tmp_path, graphical="wayland")
        assert box.run().returncode == 0
        box.shim("loginctl", 'if [[ "$1" == "list-sessions" ]]; then echo ""; fi\nexit 0\n')  # logged out

        assert box.run().returncode == 0

        assert not any(line.startswith("Environment=DISPLAY") for line in box.unit().splitlines())


class TestNoSecretInAChildEnvironment:
    def test_names_the_caller_exported_never_carry_the_key_to_a_child(self, tmp_path: Path) -> None:
        """bash keeps the export flag of a name the caller exported, for globals AND locals: the
        installer's own AUTH_KEY / cfg_key / … would hand the key to every child."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        exported = {name: "from-the-caller" for name in (
            "AUTH_KEY", "EXPLICIT_KEY", "GH_TOKEN_VALUE", "KEY_SOURCE", "unit_key", "env_key", "cfg_key",
            "k", "key", "line", "rest", "OLD_UNIT", "OLD_DESCRIPTION",  # OLD_UNIT holds a legacy --auth-key
        )}

        result = box.run(**exported)

        assert result.returncode == 0, output(result)
        assert K_UNIT not in box.child_environments(), "a child was started with the key in its environment"


class TestCameraBoxAdoptionChurn:
    def test_a_camera_box_box_at_the_same_source_is_not_restarted(self, tmp_path: Path) -> None:
        """Only Description= differed from camera-box's unit, and that alone restarted a live box
        on every run (and flip-flopped with camera-box's lib)."""
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV, source_id=MAIN_SOURCE_ID, version=SOURCE_VERSION)
        unit_before, restarts = box.unit(), box.state()["restarts"]

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.unit() == unit_before
        assert box.state()["restarts"] == restarts

    def test_a_same_version_in_place_upgrade_by_camera_box_s_lib_is_rebuilt(self, tmp_path: Path) -> None:
        """Only the marker rule can see this one: same version, camera-box's marker in our build."""
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        (box.venv_target() / ".camera-box-source").write_text(CB_SOURCE_ID + "\n")

        result = box.run()

        assert result.returncode == 0, output(result)
        assert len(box.venv_installs()) == 2


class TestInterruptedBuild:
    def test_a_signal_after_the_build_leaves_no_partial_build(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        before = box.snapshot()
        box.inject_after('printf \'%s\\n\' "$SOURCE_ID" >"$NEW_BUILD/.remoteos-mcp-source"', "kill -TERM $$")

        result = box.run()

        assert result.returncode != 0, output(result)
        assert builds(box) == [] and box.snapshot() == before


class TestLockPlace:
    def test_the_lock_is_not_in_a_world_writable_directory(self) -> None:
        """/run/lock is 1777: any local user could hold or pre-create the lock and block every
        install (and fs.protected_regular makes root's open of a foreign file there fail)."""
        text = Path(__file__).resolve().parent.parent.joinpath("install-linux.sh").read_text()
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith("LOCK_FILE=")]
        assert lines == ['LOCK_FILE="/run/remoteos-mcp-install.lock"'], lines


# ---------------------------------------------------------------------------
# Review round 4
# ---------------------------------------------------------------------------


class TestEnableOnlyNeverStops:
    """Review round 4: enable-only promises never to start (or stop) anything. A failure in an
    enable-only run puts the previous files back and leaves the running process alone; and a
    proven box it rolls back is not left marked pending."""

    def test_an_interrupted_enable_only_run_leaves_the_running_agent_alone(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0
        unit_before, target_before = box.unit(), box.venv_target()
        box.add_source("main", "def5678", "0.7.0.dev33")
        box.inject_after('mv -Tf "$tmp_link" "$VENV_LINK"', "kill -HUP $$")

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")

        assert result.returncode != 0, output(result)
        state = box.state()
        assert state["active"] and state["enabled"], "enable-only stopped the agent it never started"
        assert state["service"]["python"].endswith("/shims/python3"), "the running legacy agent was touched"
        assert box.unit() == unit_before and box.venv_target() == target_before
        assert (box.config_dir / ".switch-pending").exists(), "the earlier enable-only switch is still unproven"

    def test_an_enable_only_run_that_cannot_enable_leaves_the_running_agent_alone(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        assert box.run(REMOTEOS_MCP_POLICY="enable-only").returncode == 0
        box.configure(service={"enable_fails": True})

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")

        assert result.returncode != 0, output(result)
        assert box.state()["active"], "enable-only stopped the agent it never started"
        assert "running service was not touched" in output(result)

    def test_a_failed_enable_only_run_on_a_proven_box_leaves_it_proven(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        box.configure(service={"enable_fails": True})

        result = box.run(REMOTEOS_MCP_POLICY="enable-only")

        assert result.returncode != 0, output(result)
        assert not (box.config_dir / ".switch-pending").exists(), "a proven box was left marked pending"
        assert box.state()["active"]


class TestNoOrphanAfterArm:
    def test_a_signal_right_after_arming_leaves_no_build_and_no_pending_mark(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        unit_before = box.unit()
        box.inject_after("arm", "kill -TERM $$")

        result = box.run()

        assert result.returncode != 0, output(result)
        _assert_legacy_box_restored(box, unit_before)


class TestHungProcess:
    def test_an_unchanged_box_whose_process_answers_nothing_is_restarted(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        box.hang()
        restarts = box.state()["restarts"]

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.state()["restarts"] == restarts + 1
        assert box.venv_installs()[1:] == [], "a hung process made pip run again"
