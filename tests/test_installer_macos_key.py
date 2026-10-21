"""install.sh (macOS): the key leaves the plist's ProgramArguments (#12, design
issuecomment-6293340521).

The LaunchAgent plist used to carry `--auth-key <key>` in ProgramArguments, so the key was in
the process argv (`ps`) and in a plist other accounts could read. Now it is in the plist's
EnvironmentVariables (REMOTEOS_AUTH_KEY, which remoteos reads), the plist and config.json are
mode 600, and the key precedence is the Linux one: explicit REMOTEOS_MCP_AUTH_KEY, then the
running plist's key (a legacy `--auth-key` or EnvironmentVariables), then config.json, then a
new key.

The real installer runs on the fake mac of the preflight/rollback tests (installer_harness +
fake_pip), with every command it starts logged.
"""

from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path

import pytest
from fake_linux_tools import records
from installer_harness import (
    NEW_VERSION,
    _create_macos_shims,
    _prepare_macos_installer,
    _run_installer,
    health_shim,
    install_old_remoteos,
    log_every_command,
    setup_fake_pip,
)

K_PLIST = "PlistKey0123456789abcdefghijklmn"
K_PLIST_ENV = "PlistEnvKey0123456789abcdefghijk"
K_CONFIG = "MacConfigKey0123456789abcdefghij"
K_EXPLICIT = "MacExplicitKey0123456789abcdefgh"


class Mac:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.script = _prepare_macos_installer(tmp_path)
        self.shims = tmp_path / "shims"
        self.shims.mkdir()
        _create_macos_shims(self.shims, tmp_path / "invocations.log")
        self.pip = setup_fake_pip(tmp_path, self.shims)
        health_shim(self.shims, NEW_VERSION)
        install_old_remoteos(self.pip)
        self.records = tmp_path / "argv.records"
        log_every_command(self.shims, self.records)
        self.plist = tmp_path / "LaunchAgents" / "com.remoteos-mcp.plist"
        self.config = tmp_path / "dot-remoteos-mcp" / "config.json"

    def run(self, **env: str):
        return _run_installer(self.script, self.shims, self.tmp, extra_env=env)

    def write_plist(self, *, args_key: str | None = None, env_key: str | None = None) -> None:
        args = ["/usr/local/bin/python3", "-m", "remoteos", "--transport", "streamable-http", "--port", "8092"]
        if args_key:
            args += ["--auth-key", args_key]
        plist = {"Label": "com.remoteos-mcp", "ProgramArguments": args, "RunAtLoad": True}
        if env_key:
            plist["EnvironmentVariables"] = {"REMOTEOS_AUTH_KEY": env_key}
        self.plist.write_bytes(plistlib.dumps(plist))

    def write_config(self, key: str) -> None:
        self.config.write_text(json.dumps({"port": 8092, "auth_key": key, "host": "0.0.0.0"}))

    def loaded(self) -> dict:
        return plistlib.loads(self.plist.read_bytes())

    def config_key(self) -> str:
        return json.loads(self.config.read_text())["auth_key"]

    def argvs(self) -> list[list[str]]:
        return records(self.records)


def _output(result) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestMacosPlistKey:
    def test_the_key_is_in_environment_variables_never_in_program_arguments(self, tmp_path: Path) -> None:
        mac = Mac(tmp_path)

        result = mac.run()
        out = _output(result)

        assert result.returncode == 0, out
        key = mac.config_key()
        assert re.fullmatch(r"[A-Za-z0-9]{32}", key), key
        plist = mac.loaded()
        assert "--auth-key" not in plist["ProgramArguments"], plist
        assert key not in " ".join(plist["ProgramArguments"]), plist
        assert plist["EnvironmentVariables"]["REMOTEOS_AUTH_KEY"] == key
        assert plist["ProgramArguments"][1:] == [
            "-m", "remoteos", "--transport", "streamable-http", "--enable-all", "--host", "0.0.0.0", "--port", "8092",
        ]
        assert _mode(mac.plist) == 0o600
        assert _mode(mac.config) == 0o600
        assert key not in out, "the installer printed the key"
        for argv in mac.argvs():
            assert not any(key in a for a in argv), f"the key is in an argv: {argv}"


class TestMacosKeyPrecedence:
    @pytest.mark.parametrize(
        ("plist", "expected"),
        [
            ({"args_key": K_PLIST}, K_PLIST),
            ({"env_key": K_PLIST_ENV}, K_PLIST_ENV),
            ({}, K_CONFIG),
        ],
        ids=["legacy-auth-key", "environment-variables", "config-json"],
    )
    def test_the_running_plist_s_key_wins_over_config_json(self, tmp_path: Path, plist: dict, expected: str) -> None:
        mac = Mac(tmp_path)
        mac.write_plist(**plist)
        mac.write_config(K_CONFIG)

        result = mac.run()

        assert result.returncode == 0, _output(result)
        assert mac.loaded()["EnvironmentVariables"]["REMOTEOS_AUTH_KEY"] == expected
        assert mac.config_key() == expected

    def test_a_new_key_is_kept_by_every_rerun(self, tmp_path: Path) -> None:
        mac = Mac(tmp_path)

        first = mac.run()
        key = mac.config_key()
        second = mac.run()

        assert first.returncode == second.returncode == 0, _output(second)
        assert mac.config_key() == key
        assert mac.loaded()["EnvironmentVariables"]["REMOTEOS_AUTH_KEY"] == key

    def test_an_explicit_key_wins_and_never_reaches_an_argv(self, tmp_path: Path) -> None:
        mac = Mac(tmp_path)
        mac.write_plist(args_key=K_PLIST)

        result = mac.run(REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)

        assert result.returncode == 0, _output(result)
        assert mac.loaded()["EnvironmentVariables"]["REMOTEOS_AUTH_KEY"] == K_EXPLICIT
        for argv in mac.argvs():
            assert not any(K_EXPLICIT in a for a in argv), argv

    def test_names_the_caller_exported_never_carry_the_key_to_a_child(self, tmp_path: Path) -> None:
        """Review round 2: bash keeps the export flag of a name the caller exported, so the
        installer's own AUTH_KEY / CFG_KEY / … handed the key to every child."""
        mac = Mac(tmp_path)
        mac.write_plist(env_key=K_PLIST_ENV)
        exported = {name: "from-the-caller" for name in (
            "AUTH_KEY", "EXPLICIT_KEY", "CFG_KEY", "PLIST_KEY", "KEY_SOURCE", "k",
        )}

        result = mac.run(**exported)

        assert result.returncode == 0, _output(result)
        envs = Path(str(mac.records) + ".env")
        assert K_PLIST_ENV not in envs.read_text(), "a child was started with the key in its environment"

    def test_the_health_check_goes_direct_with_a_proxy_set(self, tmp_path: Path) -> None:
        """Review round 12: with http_proxy set, a proxied /health check would fail after the
        LaunchAgent already runs, reporting a working install as broken."""
        mac = Mac(tmp_path)

        result = mac.run(http_proxy="http://proxy.invalid:3128")

        assert result.returncode == 0, _output(result)

    def test_a_signal_while_a_key_file_is_written_leaves_no_temp_copy(self, tmp_path: Path) -> None:
        """Review round 11: write_private's 0600 temp file (it holds the key) goes on any exit,
        as Linux's WRITE_TMP does since round 7."""
        mac = Mac(tmp_path)
        lines = mac.script.read_text().split("\n")
        hits = [i for i, line in enumerate(lines) if line.strip() == 'cat >"$tmp"']
        assert len(hits) == 1, hits
        lines.insert(hits[0] + 1, "    kill -TERM $$")
        mac.script.write_text("\n".join(lines))

        result = mac.run(REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)

        assert result.returncode != 0, _output(result)
        leftovers = [p.name for d in (mac.config.parent, mac.plist.parent) if d.exists() for p in d.iterdir()
                     if p.name.startswith(("config.json.", "com.remoteos-mcp.plist."))]
        assert leftovers == [], "a temp copy of a key file is left behind"

    def test_an_explicit_key_never_reaches_a_child(self, tmp_path: Path) -> None:
        """Review round 9: the key is dropped from the environment before the first child
        (`python3 --version`, a brew fallback), not after the Python check."""
        mac = Mac(tmp_path)
        mac.write_plist(env_key=K_PLIST_ENV)

        result = mac.run(REMOTEOS_MCP_AUTH_KEY=K_EXPLICIT)

        assert result.returncode == 0, _output(result)
        envs = Path(str(mac.records) + ".env").read_text()
        assert K_EXPLICIT not in envs, "a child was started with the explicit key in its environment"

    @pytest.mark.parametrize("where", ["config", "plist"])
    def test_an_existing_key_the_installer_cannot_carry_is_refused_never_replaced(
        self, tmp_path: Path, where: str
    ) -> None:
        """Review round 1: a key with other characters used to be skipped and a NEW key minted,
        silently cutting the client off (#51). The first key the mac has decides."""
        mac = Mac(tmp_path)
        if where == "config":
            mac.write_config("has-dash_and.dot")
        else:
            mac.write_plist(args_key="has-dash_and.dot")
            mac.write_config(K_CONFIG)
        before = (mac.plist.read_bytes() if mac.plist.exists() else b"", mac.config.read_bytes())

        result = mac.run()

        assert result.returncode != 0, _output(result)
        assert "REMOTEOS_MCP_AUTH_KEY" in _output(result)
        assert (mac.plist.read_bytes() if mac.plist.exists() else b"", mac.config.read_bytes()) == before
        assert mac.pip.calls("install") == []

    def test_an_invalid_explicit_key_is_refused_before_anything_changes(self, tmp_path: Path) -> None:
        mac = Mac(tmp_path)
        mac.write_plist(args_key=K_PLIST)
        plist_before = mac.plist.read_bytes()

        result = mac.run(REMOTEOS_MCP_AUTH_KEY="not a valid key!")

        assert result.returncode != 0, _output(result)
        assert "REMOTEOS_MCP_AUTH_KEY" in _output(result)
        assert mac.plist.read_bytes() == plist_before
        assert mac.pip.calls("install") == [] and mac.pip.calls("uninstall") == []
