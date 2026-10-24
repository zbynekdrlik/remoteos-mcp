"""Python 3.11+ in all three installers (#64, decided on #39).

Pillow 13, the fixed release of its 2026 CVEs, needs Python 3.11, so 3.10 boxes kept an unaudited
Pillow 12.3.0. No fleet box runs 3.10: the installers now refuse it, `requires-python` is >=3.11
and constraints.txt has no 3.10 branch. The bash installers' check is ONE function, run here
exactly as written in each script (under bash 3.2 too: the `shell` CI job runs this file there).
install.ps1's Test-RemoteOSPythonVersion runs in real PowerShell 5.1 (tests_windows).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH_INSTALLERS = [ROOT / "install-linux.sh", ROOT / "install.sh"]


def _bash_function(script: Path, name: str) -> str:
    match = re.search(rf"^([ \t]*){name}\(\) {{\n.*?^\1}}\n", script.read_text(), re.MULTILINE | re.DOTALL)
    assert match, f"{script.name} has no {name}() function"
    return match.group(0)


@pytest.mark.parametrize("script", BASH_INSTALLERS, ids=lambda p: p.name)
@pytest.mark.parametrize(("text", "ok"), [
    ("Python 3.12.3", True), ("Python 3.11.0", True), ("Python 3.14.0", True),
    ("Python 3.10.12", False), ("Python 3.9.6", False), ("Python 2.7.18", False), ("", False),
])
def test_the_bash_installers_take_python_3_11_or_newer(script: Path, text: str, ok: bool) -> None:
    code = _bash_function(script, "python_ok") + 'python_ok "$1"\n'
    result = subprocess.run([os.environ.get("INSTALLER_TEST_BASH", "bash"), "-c", code, "probe", text],
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == (0 if ok else 1), result.stderr


@pytest.mark.parametrize("script", BASH_INSTALLERS, ids=lambda p: p.name)
def test_the_python_search_uses_the_one_check(script: Path) -> None:
    text = script.read_text()
    assert 'if python_ok "$ver"; then' in text, f"{script.name} does not check the version with python_ok"
    assert ">= 10" not in text and "3.10+" not in text, f"{script.name} still accepts Python 3.10"
    assert "3.11+" in text, f"{script.name} does not say Python 3.11+ is required"


def test_the_package_requires_python_3_11() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert re.findall(r'^requires-python = "([^"]+)"$', text, re.MULTILINE) == [">=3.11"]
    assert "python_version < '3.11'" not in text, "a 3.10-only pin is left"


def test_constraints_have_no_python_3_10_branch() -> None:
    text = (ROOT / "constraints.txt").read_text()
    assert "--python-version 3.11" in text.splitlines()[1], "constraints.txt was not compiled for 3.11"
    assert "< '3.11'" not in text, "constraints.txt still carries a Python 3.10 branch"
