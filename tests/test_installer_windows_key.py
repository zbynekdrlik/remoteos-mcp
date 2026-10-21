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


def test_the_explicit_key_leaves_the_environment_before_the_first_child() -> None:
    """A child process inherits $env:, so the key is read and removed before `query user`,
    `python --version`, winget, curl.exe or any other program starts (review round 9)."""
    lines = _code_lines()
    read = next(n for n, line in lines if line.startswith("$explicitKey = $env:REMOTEOS_MCP_AUTH_KEY"))
    removed = next(n for n, line in lines if line.startswith("Remove-Item Env:REMOTEOS_MCP_AUTH_KEY"))
    children = [n for n, line in lines
                if any(child in line for child in ("query user", "--version", "winget", "curl.exe", "Start-Process",
                                                   "icacls", "schtasks", "& $"))]
    assert children, "no child process found: the check would be vacuous"
    assert read < removed < min(children), (read, removed, min(children))


def _start_script_template() -> list[str]:
    """The lines of New-RemoteOSStartScript's here-string (the generated start-remoteos.bat)."""
    lines = [line.strip() for line in INSTALLER_WINDOWS.read_text().splitlines()]
    start = lines.index("function New-RemoteOSStartScript([string]$PythonPath, [int]$Port, [string]$AuthKey) {")
    begin = lines.index('return @"', start) + 1
    return lines[begin:lines.index('"@', begin)]


def test_the_start_script_gives_the_key_to_the_server_only() -> None:
    """Every program cmd.exe starts inherits its environment: the key is set right before the
    server line and cleared right after it, so netstat, findstr and timeout never get it (review
    round 10)."""
    bat = _start_script_template()
    sets = [i for i, line in enumerate(bat) if line == 'set "REMOTEOS_AUTH_KEY=$AuthKey"']
    server = [i for i, line in enumerate(bat) if "-m remoteos" in line]
    assert len(sets) == 1 and len(server) == 1, (sets, server)
    assert sets[0] == server[0] - 1, "something runs between setting the key and starting the server"
    assert bat[server[0] + 1] == "set EXIT_CODE=%errorlevel%"
    assert bat[server[0] + 2] == 'set "REMOTEOS_AUTH_KEY="', "the key stays set for the loop's other programs"
    netstat = next(i for i, line in enumerate(bat) if line.startswith("netstat"))
    assert bat.index(":loop") < netstat < sets[0], "the key is set before the loop's port check"


def test_install_ps1_draws_the_key_from_a_csprng() -> None:
    code = "\n".join(line for _, line in _code_lines())
    assert "Get-Random" not in code, "Get-Random is not a CSPRNG (and -Count 32 draws 32 distinct characters)"
    assert "RandomNumberGenerator" in code


def test_install_ps1_is_ascii_so_windows_powershell_5_1_parses_it() -> None:
    """Windows PowerShell 5.1 reads a BOM-less script file (`-File`, Parser::ParseFile) in the
    ANSI code page. A UTF-8 em dash (E2 80 94) is then cp1252 'â€”', whose 0x94 is U+201D, which
    PowerShell treats as a closing double quote: the string ends early and the whole file stops
    parsing (the windows-powershell job: "Missing closing '}'" at an `if` holding such a line).
    `irm | iex` hides it (the web response is decoded as UTF-8), so ASCII-only is the rule."""
    data = INSTALLER_WINDOWS.read_bytes()
    bad = [(n, line.decode("utf-8", "replace").strip())
           for n, line in enumerate(data.splitlines(), 1) if any(b > 0x7F for b in line)]
    assert bad == [], f"install.ps1 has non-ASCII lines (5.1 misreads them): {bad}"


def test_install_ps1_uses_no_powershell_7_only_syntax() -> None:
    """The fleet's Windows boxes run Windows PowerShell 5.1: no `??` / `??=`, no `?.` / `?[`,
    no `&&` / `||` pipeline chains, no `? :` ternary (all PowerShell 7+)."""
    offenders = [(n, line) for n, line in _code_lines()
                 if any(token in line for token in ("??", "?.", "?[", "&&", "||", " ? "))]
    assert offenders == [], f"PowerShell 7-only syntax in install.ps1: {offenders}"
