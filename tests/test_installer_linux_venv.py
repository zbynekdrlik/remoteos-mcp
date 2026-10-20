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

from linux_installer_harness import linux_box, mode_of


def _output(result) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _assert_never_in_an_argv(box, secret: str) -> None:
    for argv in box.argvs():
        assert not any(secret in arg for arg in argv), f"the key is in an argv: {argv}"


class TestKeyNeverInTheUnit:
    def test_fresh_install_keeps_the_key_out_of_the_unit_and_out_of_every_argv(self, tmp_path: Path) -> None:
        box = linux_box(tmp_path)

        result = box.run()
        out = _output(result)

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
        _assert_never_in_an_argv(box, key)
        assert key not in out, "the installer printed the key"
        # the running service got the key from its EnvironmentFile
        assert box.state()["service"]["key"] == key
