"""install.ps1, static checks on the Linux job (#12): the key never goes onto a command line
and is never printed. The behaviour (the generated .bat, the key precedence including
remoteos.toml, the ACL) is tested in real PowerShell 5.1: tests_windows/test_install_ps1_key.py.
"""

from __future__ import annotations

from installer_harness import INSTALLER_WINDOWS


def _code_lines() -> list[tuple[int, str]]:
    """install.ps1's lines without comments (# ... and <# ... #> blocks)."""
    lines, in_block = [], False
    for number, line in enumerate(INSTALLER_WINDOWS.read_text().splitlines(), 1):
        stripped = line.strip()
        if in_block:
            in_block = "#>" not in stripped
            continue
        if stripped.startswith("<#"):
            in_block = "#>" not in stripped
            continue
        if stripped and not stripped.startswith("#"):
            lines.append((number, stripped))
    return lines


def test_install_ps1_never_writes_auth_key_onto_a_command_line() -> None:
    offenders = [(n, line) for n, line in _code_lines() if "--auth-key" in line]
    assert offenders == [] or all("-match" in line or "Get-RemoteOSKey" in line for _, line in offenders), offenders


def test_install_ps1_never_prints_the_key() -> None:
    offenders = [(n, line) for n, line in _code_lines() if "Write-Host" in line and "$AuthKey" in line]
    assert offenders == [], f"install.ps1 prints the key: {offenders}"


def test_install_ps1_draws_the_key_from_a_csprng() -> None:
    code = "\n".join(line for _, line in _code_lines())
    assert "Get-Random" not in code, "Get-Random is not a CSPRNG (and -Count 32 draws 32 distinct characters)"
    assert "RandomNumberGenerator" in code
