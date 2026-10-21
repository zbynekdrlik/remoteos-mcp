"""install-linux.sh: ONE installer on camera-box's venv layout, and the key never in a unit or
an argv (#12, design issuecomment-6293340521).

Two installers fought over remoteos-mcp.service. Ours put the key in the world-readable unit's
ExecStart (`--auth-key`, readable with `systemctl cat` and in `ps`), and installed into the
system python from `main`. camera-box's scripts/lib/remoteos-mcp.sh used a venv at
/opt/remoteos-mcp-venv, kept the key in 0600 files only, and pinned a ref. On 16.10 its run
reverted strih-pp/stream-pp to its pin.

These tests run the real installer on a fake box (tests/linux_installer_harness.py): real
venvs, a stateful systemd/curl at the tool boundary, and the argv of every process the
installer starts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from linux_installer_harness import (
    CB_SOURCE_ID,
    CB_VERSION,
    K_CONFIG,
    K_ENV,
    K_EXPLICIT,
    K_UNIT,
    LEGACY_VERSION,
    MAIN_SOURCE_ID,
    SERVICE_UID,
    SERVICE_USER,
    SOURCE_VERSION,
    assert_never_in_an_argv,
    builds,
    camera_box_unit,
    exec_start,
    legacy_removals,
    linux_box,
    mode_of,
    output,
    restarted,
    without_description,
    write_config,
)


class TestKeyNeverInTheUnit:
    def test_fresh_install_keeps_the_key_out_of_the_unit_and_out_of_every_argv(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)

        result = box.run()
        out = output(result)

        assert result.returncode == 0, out
        key = box.config_key()
        assert re.fullmatch(r"[A-Za-z0-9]{32}", key), key
        unit = box.unit()
        assert "--auth-key" not in unit, unit
        assert key not in unit, unit
        assert f"EnvironmentFile={box.env_file}" in unit.splitlines(), unit
        assert box.env_file_key() == key
        assert mode_of(box.env_file) == 0o600
        assert mode_of(box.config_file) == 0o600
        assert_never_in_an_argv(box, key)
        assert key not in out, "the installer printed the key"
        # the running service got the key from its EnvironmentFile
        assert box.state()["service"]["key"] == key

    def test_fresh_install_runs_the_venv_with_camera_box_s_headless_unit(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert exec_start(box) in box.unit().splitlines()
        assert without_description(box.unit()) == without_description(
            camera_box_unit(box.venv_link, box.env_file, mode="headless"))
        assert box.venv_link.is_symlink() and box.venv_target().parent == box.venv_store.resolve()
        service = box.state()["service"]
        assert service["version"] == SOURCE_VERSION
        assert service["user"] == SERVICE_USER
        assert (box.venv_target() / ".remoteos-mcp-source").read_text().strip() == MAIN_SOURCE_ID


# ---------------------------------------------------------------------------
# The key: explicit, else the running unit's, else config.json, else new — and a re-run keeps it
# ---------------------------------------------------------------------------


class TestKeyPrecedence:
    def test_the_legacy_unit_s_auth_key_wins_over_config_json(self, tmp_path: Path) -> None:
        """The key the RUNNING service accepts is the one dev1's .mcp.json holds."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        write_config(box, K_CONFIG)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.env_file_key() == K_UNIT
        assert box.config_key() == K_UNIT
        assert box.state()["service"]["key"] == K_UNIT
        assert_never_in_an_argv(box, K_UNIT)

    def test_the_environment_file_key_wins_over_config_json(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV)
        write_config(box, K_CONFIG)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.env_file_key() == K_ENV
        assert box.config_key() == K_ENV
        assert box.state()["service"]["key"] == K_ENV

    def test_a_config_json_key_is_reused(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        write_config(box, K_CONFIG)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.env_file_key() == K_CONFIG
        assert box.state()["service"]["key"] == K_CONFIG
        assert "generated" not in output(result).lower()

    def test_no_key_anywhere_gets_a_new_one_and_every_rerun_keeps_it(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)

        first = box.run()
        key = box.config_key()
        second = box.run()
        third = box.run()

        assert first.returncode == second.returncode == third.returncode == 0, output(third)
        assert re.fullmatch(r"[A-Za-z0-9]{32}", key)
        assert "new auth key" in output(first).lower()
        assert "new auth key" not in output(second).lower()
        assert box.env_file_key() == box.config_key() == key
        assert box.state()["service"]["key"] == key

    def test_an_explicit_key_wins_and_never_reaches_an_argv(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)

        result = box.run(REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)

        assert result.returncode == 0, output(result)
        assert box.env_file_key() == box.config_key() == K_EXPLICIT
        assert box.state()["service"]["key"] == K_EXPLICIT
        assert_never_in_an_argv(box, K_EXPLICIT)
        assert K_EXPLICIT not in output(result)

    @pytest.mark.parametrize("bad", ["has space", "semi;colon", "x" * 129])
    def test_an_invalid_explicit_key_is_refused_before_anything_changes(self, tmp_path: Path, bad: str) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        before = box.snapshot()

        result = box.run(REMOTEOS_MCP_AUTH_KEY=bad)

        assert result.returncode != 0, output(result)
        assert "REMOTEOS_MCP_AUTH_KEY" in output(result)
        assert box.snapshot() == before
        assert box.venv_installs() == []


# ---------------------------------------------------------------------------
# Migration from our old system-python layout
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    def test_a_system_python_box_moves_to_the_venv_keeping_its_key_and_user(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)

        result = box.run()
        out = output(result)

        assert result.returncode == 0, out
        assert exec_start(box) in box.unit().splitlines()
        assert "--auth-key" not in box.unit() and K_UNIT not in box.unit()
        service = box.state()["service"]
        assert service["python"] == str(box.venv_link / "bin" / "python")
        assert (service["version"], service["key"], service["user"]) == (SOURCE_VERSION, K_UNIT, SERVICE_USER)
        assert len(builds(box)) == 1 and builds(box)[0] == box.venv_target()

    def test_the_legacy_system_copy_is_removed_only_after_the_venv_proved_itself(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.pip.versions("remoteos-mcp") == [], "the old system-python copy is still there"
        removals = legacy_removals(box)
        assert len(removals) == 1, removals
        assert "--break-system-packages" in removals[0]
        assert {"remoteos-mcp", "fastmcp", "fastmcp-slim"} <= set(removals[0])
        argvs = box.argvs()
        removal_at = argvs.index(["python3", *removals[0]])
        mcp_at = max(i for i, a in enumerate(argvs) if a[:1] == ["curl"] and any(x.endswith("/mcp") for x in a))
        assert mcp_at < removal_at, "the system copy was removed before the venv answered an authenticated request"
        authed = [r for r in box.curl_requests() if r["url"].endswith("/mcp") and '"initialize"' in r["data"]]
        assert authed and f"Authorization: Bearer {K_UNIT}" in authed[-1]["headers"]


# ---------------------------------------------------------------------------
# A failed build changes nothing: the old venv, the old unit, the old service, the legacy copy
# ---------------------------------------------------------------------------


class TestFailedBuild:
    @pytest.mark.parametrize("failure", ["install_fail", "install_broken", "install_noop"])
    def test_a_failed_venv_build_keeps_the_legacy_box_as_it_was(self, tmp_path: Path, failure: str) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        box.configure(venv_pip={failure: True, "error_lines": 15})
        before, state_before = box.snapshot(), box.state()

        result = box.run()
        out = output(result)

        assert result.returncode != 0, out
        assert box.snapshot() == before, "the box changed although the build failed"
        assert box.state()["service"] == state_before["service"] and box.state()["active"]
        assert not restarted(box)
        assert box.pip.versions("remoteos-mcp") == [LEGACY_VERSION], "the legacy copy was removed"
        assert legacy_removals(box) == []
        assert builds(box) == [], "a failed build was left in the store"
        if failure == "install_fail":
            for i in range(1, 16):
                assert f"install-error-line-{i:02d}" in out, f"line {i} of the pip error is missing:\n{out}"

    def test_a_failed_venv_build_keeps_a_camera_box_venv_untouched(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV)
        box.configure(venv_pip={"install_fail": True})
        before = box.snapshot()

        result = box.run()

        assert result.returncode != 0, output(result)
        assert box.snapshot() == before
        assert not box.venv_link.is_symlink() and box.state()["service"]["version"] == CB_VERSION
        assert not restarted(box)

    def test_a_failed_download_changes_nothing(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        box.configure(fetch_fail=True)
        before = box.snapshot()

        result = box.run()

        assert result.returncode != 0, output(result)
        assert "404" in output(result)
        assert box.snapshot() == before
        assert box.venv_installs() == [] and not restarted(box)


# ---------------------------------------------------------------------------
# The new install must prove itself, or the box goes back to what it was
# ---------------------------------------------------------------------------


class TestProveHealth:
    @pytest.mark.parametrize(
        ("service", "message"),
        [
            ({"initialize_code": 403}, "authenticated"),
            ({"unauth_code": 200}, "not enforced"),
            ({"start_fails": True}, "not active"),
        ],
        ids=["initialize-refused", "auth-off", "service-down"],
    )
    def test_a_new_install_that_does_not_prove_itself_is_rolled_back(
        self, tmp_path: Path, service: dict, message: str
    ) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        unit_before, config_before = box.unit(), box.config_file.read_bytes()
        box.configure(service={**service, "only_version": SOURCE_VERSION})  # the NEW install's server

        result = box.run()
        out = output(result)

        assert result.returncode != 0, out
        assert message in out, out
        assert box.unit() == unit_before, "the previous unit was not restored"
        assert box.config_file.read_bytes() == config_before
        assert not box.env_file.exists() and not box.venv_link.exists() and not box.venv_link.is_symlink()
        assert box.pip.versions("remoteos-mcp") == [LEGACY_VERSION]
        assert legacy_removals(box) == []
        assert builds(box) == []
        loaded = box.state()["service"]
        if loaded is not None:  # start_fails keeps the old unit down too; the others restart it
            assert loaded["python"].endswith("/shims/python3") and loaded["key"] == K_UNIT

    @pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
    def test_an_interrupted_switch_is_rolled_back(self, tmp_path: Path, signal_name: str) -> None:
        """An ssh drop (HUP), a timeout's TERM or Ctrl-C while the box switches to the new install
        (venv swapped, files written, service restarting) must leave the previous install."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        unit_before, config_before = box.unit(), box.config_file.read_bytes()
        box.configure(service={"signal_on_restart": signal_name})

        result = box.run()
        out = output(result)

        assert result.returncode != 0, out
        assert "Interrupted" in out, out
        assert box.unit() == unit_before
        assert box.config_file.read_bytes() == config_before
        assert not box.env_file.exists() and not box.venv_link.is_symlink() and builds(box) == []
        assert box.pip.versions("remoteos-mcp") == [LEGACY_VERSION]
        service = box.state()["service"]
        assert service["python"].endswith("/shims/python3") and service["key"] == K_UNIT

    def test_a_console_lost_while_switching_is_rolled_back(self, tmp_path: Path) -> None:
        """A dropped no-tty `ssh box 'curl … | sudo bash'` sends no signal: the installer's next
        write fails. Unproven, the switch must not stay half done."""
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        unit_before = box.unit()
        release = tmp_path / "console-closed"
        box.configure(service={"restart_waits_for": str(release)})

        returncode = box.run_and_drop_console("Restarting", release)

        assert returncode != 0
        assert box.unit() == unit_before
        assert not box.env_file.exists() and not box.venv_link.is_symlink() and builds(box) == []
        assert box.pip.versions("remoteos-mcp") == [LEGACY_VERSION]
        assert box.state()["service"]["python"].endswith("/shims/python3")

    def test_a_service_reporting_another_version_is_a_failure_naming_both(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(service={"serve_version": "0.0.1"})

        result = box.run()
        out = output(result)

        assert result.returncode != 0, out
        assert SOURCE_VERSION in out and "0.0.1" in out, out


# ---------------------------------------------------------------------------
# Idempotent: an unchanged source skips pip, nothing is rewritten, no restart
# ---------------------------------------------------------------------------


class TestIdempotent:
    def test_an_unchanged_source_skips_pip_rewrites_nothing_and_does_not_restart(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        before, installs = box.snapshot(), len(box.venv_installs())
        restarts = box.state()["restarts"]

        result = box.run()

        assert result.returncode == 0, output(result)
        assert len(box.venv_installs()) == installs, "pip ran for an unchanged source"
        assert box.snapshot() == before, "an unchanged box was rewritten"
        assert box.state()["restarts"] == restarts, "an unchanged box was restarted"
        assert any(r["url"].endswith("/health") for r in box.curl_requests())

    def test_a_new_source_is_built_side_by_side_swapped_in_and_the_old_build_pruned(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        old_build, key = box.venv_target(), box.config_key()
        box.add_source("main", "def5678", "0.7.0.dev33")

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.venv_target() != old_build and not old_build.exists()
        assert builds(box) == [box.venv_target()]
        assert box.state()["service"]["version"] == "0.7.0.dev33"
        assert box.config_key() == key
        assert restarted(box)


# ---------------------------------------------------------------------------
# A box in camera-box's layout is adopted: same paths, same key
# ---------------------------------------------------------------------------


class TestCameraBoxAdoption:
    def test_a_camera_box_box_at_the_same_source_is_adopted_in_place(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV, source_id=MAIN_SOURCE_ID, version=SOURCE_VERSION)
        venv_before = {k: v for k, v in box.snapshot().items() if k.startswith(str(box.venv_link))}
        unit_before = box.unit()

        result = box.run()

        assert result.returncode == 0, output(result)
        assert box.venv_installs() == [], "pip ran although the box runs this source already"
        assert {k: v for k, v in box.snapshot().items() if k.startswith(str(box.venv_link))} == venv_before
        assert not box.venv_link.is_symlink()
        assert without_description(box.unit()) == without_description(unit_before)
        assert box.env_file_key() == box.config_key() == K_ENV
        assert box.state()["service"]["key"] == K_ENV

    def test_key_files_with_the_right_content_but_a_readable_mode_are_rewritten_0600(self, tmp_path: Path) -> None:
        """Review round 12: write_if_changed compares content AND mode/owner. A key file restored
        with `cp` or saved by an editor under umask 022 keeps its content but is world-readable;
        camera-box's verify wants exactly 600."""
        box = linux_box(tmp_path)
        box.install_camera_box(K_ENV, source_id=MAIN_SOURCE_ID, version=SOURCE_VERSION)
        for path in (box.env_file, box.config_file):
            path.chmod(0o644)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert mode_of(box.env_file) == 0o600 and mode_of(box.config_file) == 0o600
        assert "EnvironmentFile: written" in output(result) and "config.json: written" in output(result)
        assert box.env_file_key() == box.config_key() == K_ENV

    def test_a_camera_box_box_at_its_pin_is_upgraded_keeping_its_paths_mode_and_key(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)  # no graphical session right now: the unit's desktop shape wins
        box.install_camera_box(K_ENV)
        unit_before = box.unit()

        result = box.run()

        assert result.returncode == 0, output(result)
        assert len(box.venv_installs()) == 1
        assert without_description(box.unit()) == without_description(unit_before)
        assert box.venv_link.is_symlink() and builds(box) == [box.venv_target()]
        service = box.state()["service"]
        assert (service["version"], service["key"]) == (SOURCE_VERSION, K_ENV)
        assert service["environment"]["DISPLAY"] == ":0"
        assert (box.venv_target() / ".remoteos-mcp-source").read_text().strip() == MAIN_SOURCE_ID
        assert CB_SOURCE_ID not in " ".join(str(p) for p in builds(box))


# ---------------------------------------------------------------------------
# The source: a pinned ref, fetched as one tarball; build code never sees a secret
# ---------------------------------------------------------------------------


class TestSource:
    def test_a_pinned_ref_is_installed(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        source_id = box.add_source("v0.7.0-test", "1111111", "0.7.0.dev40")

        result = box.run(REMOTEOS_MCP_REF="v0.7.0-test")

        assert result.returncode == 0, output(result)
        tarballs = [r["url"] for r in box.curl_requests() if "/tarball/" in r["url"]]
        assert tarballs == ["https://api.github.com/repos/zbynekdrlik/remoteos-mcp/tarball/v0.7.0-test"]
        assert box.state()["service"]["version"] == "0.7.0.dev40"
        assert (box.venv_target() / ".remoteos-mcp-source").read_text().strip() == source_id

    def test_an_empty_ref_means_main(self, tmp_path: Path) -> None:
        """`${REMOTEOS_MCP_REF:-main}`, as camera-box's lib reads its ref."""
        box = linux_box(tmp_path)

        result = box.run(REMOTEOS_MCP_REF="")

        assert result.returncode == 0, output(result)
        assert [r["url"] for r in box.curl_requests() if "/tarball/" in r["url"]][0].endswith("/tarball/main")

    @pytest.mark.parametrize("bad", ["main;id", "../../x", "a b", "-main", "x/../y"])
    def test_an_invalid_ref_is_refused_before_anything_changes(self, tmp_path: Path, bad: str) -> None:
        box = linux_box(tmp_path)
        box.install_legacy(K_UNIT)
        before = box.snapshot()

        result = box.run(REMOTEOS_MCP_REF=bad)

        assert result.returncode != 0, output(result)
        assert "REMOTEOS_MCP_REF" in output(result)
        assert box.snapshot() == before
        assert box.curl_requests() == []

    def test_pip_never_sees_a_secret_and_the_github_token_goes_on_stdin(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        token = "ghp_TestToken0123456789"

        result = box.run(GH_TOKEN=token, REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT, SOME_EXPORTED_SECRET="s3cr3t")

        assert result.returncode == 0, output(result)
        (install,) = box.venv_installs()
        env = install["env"]
        for secret in (token, K_EXPLICIT, "s3cr3t"):
            assert secret not in " ".join(env.values()), f"pip saw a secret: {env}"
        assert set(env) <= {"PATH", "HOME", "LANG", "TMPDIR", "PIP_CONSTRAINT", "PIP_DISABLE_PIP_VERSION_CHECK",
                            "FAKE_PIP_CONFIG", "PYTHONHASHSEED", "LC_CTYPE", "PWD", "SHLVL", "_"}, env
        assert env["PIP_CONSTRAINT"].endswith("/constraints.txt")
        (download,) = [r for r in box.curl_requests() if "/tarball/" in r["url"]]
        assert f"Authorization: token {token}" in download["headers"]
        assert_never_in_an_argv(box, token)
        assert_never_in_an_argv(box, K_EXPLICIT)


# ---------------------------------------------------------------------------
# Desktop vs headless
# ---------------------------------------------------------------------------


class TestDesktopMode:
    def test_a_desktop_box_gets_the_desktop_unit_and_the_system_gi_bindings(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path, graphical=True)
        (box.system_extra / "gi").mkdir()
        (box.system_extra / "gi" / "__init__.py").write_text("SYSTEM_GI = True\n")

        result = box.run()

        assert result.returncode == 0, output(result)
        assert without_description(box.unit()) == without_description(
            camera_box_unit(box.venv_link, box.env_file, mode="desktop"))
        assert f"Environment=XDG_RUNTIME_DIR=/run/user/{SERVICE_UID}" in box.unit().splitlines()
        gi = box.venv_python("import gi, os; print(gi.SYSTEM_GI, os.path.realpath(gi.__file__))")
        assert gi.returncode == 0, gi.stderr
        assert gi.stdout.split()[0] == "True" and str(box.system_extra.resolve()) in gi.stdout
        assert any("xdotool" in a for a in box.calls("apt-get")), box.calls("apt-get")

    def test_a_headless_box_gets_the_headless_unit_and_no_system_packages(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        (box.system_extra / "gi").mkdir()
        (box.system_extra / "gi" / "__init__.py").write_text("SYSTEM_GI = True\n")

        result = box.run()

        assert result.returncode == 0, output(result)
        assert not any(line.startswith("Environment=DISPLAY") for line in box.unit().splitlines())
        assert box.venv_python("import gi").returncode != 0, "a headless venv imports the system gi"
        assert not any("xdotool" in a for a in box.calls("apt-get"))

    def test_a_missing_python3_venv_is_installed_with_apt(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        box.configure(venv_fail=True)

        result = box.run()

        assert result.returncode == 0, output(result)
        assert any("python3-venv" in a for a in box.calls("apt-get")), box.calls("apt-get")
        assert box.state()["service"]["version"] == SOURCE_VERSION


# ---------------------------------------------------------------------------
# uninstall-linux.sh knows the layout
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_uninstall_removes_the_venv_layout_and_an_old_system_copy(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)
        assert box.run().returncode == 0
        box.install_legacy(K_UNIT)  # a box that still has the old system-python copy too
        uninstaller = box.uninstaller()

        result = box.run(script=uninstaller)

        assert result.returncode == 0, output(result)
        assert not box.unit_path.exists() and not box.config_dir.exists()
        assert not box.venv_link.exists() and not box.venv_link.is_symlink()
        assert not box.venv_store.exists()
        assert box.pip.versions("remoteos-mcp") == [] and box.pip.versions("fastmcp-slim") == []
        assert all("--break-system-packages" in a for a in legacy_removals(box)), legacy_removals(box)
        assert not box.state()["active"]
