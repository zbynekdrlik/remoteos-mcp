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


# --- #57: a non-admin run never leaves a half install ------------------------------------------

# Lines of the script's own flow that change the box: the first one must come after the gate.
_STATE_CHANGES = ("pip uninstall", "pip install", "Remove-Item -Recurse", "Unregister-ScheduledTask",
                  "Remove-NetFirewallRule", "schtasks /End", "Stop-Process", "Set-Content", "New-Item",
                  "winget install", "curl.exe", "& $installerPath")


def _function_names() -> set[str]:
    return {line.split()[1].split("(")[0] for _, line in _code_lines() if line.startswith("function ")}


def _flow_lines() -> list[tuple[int, str]]:
    """The script's own flow: the code lines after its first top-level statement that is not a
    function definition (every function is defined above it)."""
    lines = INSTALLER_WINDOWS.read_text().splitlines()
    depth, flow_start = 0, None
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if depth == 0 and stripped.startswith("Write-Host") and flow_start is None:
            flow_start = number
        if not stripped.startswith("#"):
            depth += stripped.count("{") - stripped.count("}")
    assert flow_start, "no top-level Write-Host: the script's flow was not found"
    return [(n, line) for n, line in _code_lines() if n >= flow_start]


def test_the_elevation_gate_is_a_named_function_tests_windows_can_load() -> None:
    assert {"Get-RemoteOSElevationAction", "Test-RemoteOSInteractiveSession", "Get-RemoteOSRelaunchCommand",
            "Exit-RemoteOSInstaller"} <= _function_names()


def test_the_elevation_gate_runs_before_the_first_change_to_the_box() -> None:
    """install.ps1 used to print two yellow [!] lines for a non-admin run and carry on: a server
    without admin rights, no scheduled task, no firewall rule (alex-nb, #57)."""
    flow = _flow_lines()
    gate = [n for n, line in flow if "Get-RemoteOSElevationAction -IsAdmin" in line]
    assert len(gate) == 1, f"the elevation gate must be called exactly once: {gate}"
    changes = [n for n, line in flow if any(change in line for change in _STATE_CHANGES)]
    assert changes, "no state-changing line found: the check would be vacuous"
    assert gate[0] < min(changes), (gate[0], min(changes))


def _if_block(header: str) -> list[tuple[int, str]]:
    """The code lines of the top-level `if` block whose first line is `header`, braces included."""
    lines = INSTALLER_WINDOWS.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == header)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return [(i + 1, lines[i].strip()) for i in range(start, end + 1)]


def test_the_fail_and_relaunch_branches_end_the_run_before_any_change() -> None:
    """Review round 1: without its exit, the fail branch would go on non-elevated and the
    relaunch branch would install twice (elevated and not)."""
    changes = [n for n, line in _flow_lines() if any(change in line for change in _STATE_CHANGES)]
    for header in ("if ($elevation -eq 'fail') {", "if ($elevation -eq 'relaunch') {"):
        block = _if_block(header)
        last = block[-2][1]
        assert last.startswith(("exit ", "Exit-RemoteOSInstaller ")), f"{header} does not end the run: {last}"
        assert block[-1][0] < min(changes), (header, block[-1][0], min(changes))


def test_a_non_admin_run_is_no_longer_only_a_warning() -> None:
    code = "\n".join(line for _, line in _code_lines())
    assert "Not running as admin" not in code, "the old warn-and-continue admin check is still there"


def _catch_block_after(marker: str) -> list[str]:
    """The lines of the catch block that follows the try holding `marker`."""
    lines = INSTALLER_WINDOWS.read_text().splitlines()
    at = next(i for i, line in enumerate(lines) if marker in line and not line.strip().startswith("#"))
    catch = next(i for i in range(at, len(lines)) if lines[i].strip().startswith("} catch"))
    indent = len(lines[catch]) - len(lines[catch].lstrip())
    end = next(i for i in range(catch + 1, len(lines))
               if lines[i].strip() == "}" and len(lines[i]) - len(lines[i].lstrip()) == indent)
    return [line.strip() for line in lines[catch + 1:end]]


def test_a_failing_scheduled_task_or_firewall_rule_fails_the_run() -> None:
    """After the gate the run is elevated, so a failure there is real: exit 1, never a warning."""
    for marker in ("Register-ScheduledTask -TaskName", "New-NetFirewallRule -DisplayName"):
        block = _catch_block_after(marker)
        assert "exit 1" in block or "Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit" in block, (
            f"the catch after {marker} only warns: {block}")


def test_an_ssh_run_starts_the_server_through_the_scheduled_task() -> None:
    """A server started from session 0 (SSH) dies with the SSH session (resolume-pp, #57
    amendment). Off the desktop the installer runs the task instead and proves the listener is
    the task's process; only an interactive run starts the server itself."""
    flow = _flow_lines()
    code = "\n".join(line for _, line in flow)
    assert 'schtasks /Run /TN "RemoteOSMCP"' in code
    assert "Get-RemoteOSListenerSessionId" in _function_names()
    direct = [n for n, line in flow if line.startswith("Start-Process") and "wscript.exe" in line]
    assert len(direct) == 1, direct
    branch = [line for n, line in flow if n < direct[0] and line.startswith("if (")][-1]
    assert branch.startswith("if ($startDirect)"), f"the direct start is not inside the startDirect branch: {branch}"
    # Review round 2: directly only from the desktop user's own desktop run; another account (an
    # admin's password typed at the UAC prompt) goes through the task as well
    assert "$startDirect = $interactive -and ($env:USERNAME -ieq $desktopUser)" in code


def test_the_probe_and_the_relaunch_marker_reach_the_gate() -> None:
    """Review round 2: these call-site arguments were droppable without any test failing."""
    flow_code = "\n".join(line for _, line in _flow_lines())
    assert "-InputRedirected ([Console]::IsInputRedirected)" in flow_code
    assert "-Relaunched $relaunched" in flow_code
    assert "$pauseOnExit = $interactive -and -not $PSCommandPath -and -not $relaunched" in flow_code
    lines = _code_lines()
    read = next(n for n, line in lines if line == "$relaunched = [bool]$env:REMOTEOS_MCP_RELAUNCHED")
    removed = next(n for n, line in lines if line.startswith("Remove-Item Env:REMOTEOS_MCP_RELAUNCHED"))
    assert read < removed < _gate_line(), (read, removed)


def _gate_line() -> int:
    return next(n for n, line in _flow_lines() if "Get-RemoteOSElevationAction -IsAdmin" in line)


def test_every_failure_after_the_gate_exits_1_through_the_helper() -> None:
    """Review rounds 1-2: a bare `return` exited 0 (the relaunch forwarded it as success), and a
    bare `exit` closes a piped run's console with the message unread."""
    after_gate = [(n, line) for n, line in _flow_lines() if n > _gate_line()]
    bare = [(n, line) for n, line in after_gate if line == "return" or line.startswith("exit ")]
    assert bare == [], f"failure exits that bypass Exit-RemoteOSInstaller: {bare}"
    content = INSTALLER_WINDOWS.read_text()
    for message in ("[X] pip install failed", "[X] Could not install Python"):
        start = content.index(message)
        block = content[start:content.index("\n}", start)]
        assert "Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit" in block, (message, block)


# --- #12: the code and its constraints come from ONE commit ------------------------------------

def test_the_constraints_and_the_source_archive_come_from_one_resolved_commit() -> None:
    """install.ps1 fetched constraints.txt from raw .../main/ and installed archive/main.zip: two
    requests for "main", and raw caches /main/ for minutes, so a merge that changes the pins could
    pair new code with old constraints (#12). Now $Ref resolves to ONE sha that both URLs use."""
    flow = _flow_lines()
    constraint = [line for _, line in flow if line.startswith("$env:PIP_CONSTRAINT")]
    archive = [line for _, line in flow
               if "pip install" in line and "archive/" in line and not line.startswith("Write-Host")]
    assert len(constraint) == 1 and len(archive) == 1, (constraint, archive)
    assert "/$sourceSha/constraints.txt" in constraint[0], constraint
    assert "archive/$sourceSha.zip" in archive[0], archive
    code = "\n".join(line for _, line in flow)
    assert "main.zip" not in code and "/main/constraints.txt" not in code


def test_the_ref_is_resolved_through_the_github_api_before_anything_changes() -> None:
    flow = _flow_lines()
    resolve = [n for n, line in flow if "api.github.com/repos/zbynekdrlik/remoteos-mcp/commits/" in line]
    assert len(resolve) == 1, resolve
    changes = [n for n, line in flow if any(change in line for change in _STATE_CHANGES)]
    assert resolve[0] < min(changes), (resolve[0], min(changes))
    code = "\n".join(line for _, line in _code_lines())
    assert "$env:REMOTEOS_MCP_REF" in code, "the ref is not an input (REMOTEOS_MCP_REF, as on Linux)"
    assert "{40}" in code, "the resolved ref is not checked to be a full commit sha"


def test_the_installed_commit_is_recorded_next_to_config_json() -> None:
    """#60's updater reads which commit a box runs from installed-ref.txt (no secret in it)."""
    writes = [line for _, line in _flow_lines() if "installed-ref.txt" in line]
    assert any("$sourceSha" in line and "Set-Content" in line for line in writes), writes
    assert not any("$AuthKey" in line for line in writes), writes


def test_the_elevated_relaunch_runs_the_same_commit() -> None:
    """The elevated process does not inherit the caller's environment: the relaunch fetches the
    installer of the resolved commit and hands that commit over inside its own command."""
    code = "\n".join(line for _, line in _code_lines())
    assert '$InstallerUrl = "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/$sourceSha/install.ps1"' in code
    assert ("Invoke-RemoteOSElevatedRelaunch -ScriptPath $PSCommandPath -InstallerUrl $InstallerUrl"
            " -SourceRef $sourceSha") in code
