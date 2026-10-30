"""install.ps1, static checks on the Linux job (#12): the key never goes onto a command line
and is never printed. The behaviour (the generated .bat, the key precedence including
remoteos.toml, the ACL) is tested in real PowerShell 5.1: tests_windows/test_install_ps1_key.py.
"""

from __future__ import annotations

import re

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
                  "winget install", "curl.exe", "& $installerPath", "Stop-RemoteOSServer",
                  "Install-RemoteOSWheelhouse")


def _function_names() -> set[str]:
    return {line.split()[1].split("(")[0] for _, line in _code_lines() if line.startswith("function ")}


def _function_code(name: str) -> list[str]:
    """The code lines of install.ps1's top-level function `name`, its closing brace included."""
    lines = INSTALLER_WINDOWS.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith((f"function {name}(", f"function {name} ")))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return [line.strip() for line in lines[start:end + 1] if line.strip() and not line.strip().startswith("#")]


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
        # After the stop a failure rolls back first (#64), then exits 1
        assert any(line.startswith("Exit-RemoteOSAfterFailure -Reason") and line.endswith("@rollback")
                   for line in block), f"the catch after {marker} does not fail the run: {block}"


def test_an_ssh_run_starts_the_server_through_the_scheduled_task() -> None:
    """A server started from session 0 (SSH) dies with the SSH session (resolume-pp, #57
    amendment). Off the desktop the installer runs the task instead and proves the listener is
    the task's process; only an interactive run starts the server itself."""
    # The start and the proof are functions since #64: the rollback starts and proves the old
    # version the same way
    start = _function_code("Start-RemoteOSServer")
    assert 'schtasks /Run /TN "RemoteOSMCP"' in "\n".join(start)
    direct = [i for i, line in enumerate(start) if line.startswith("Start-Process") and "wscript.exe" in line]
    assert len(direct) == 1, direct
    branch = [line for line in start[:direct[0]] if line.startswith("if (")][-1]
    assert branch.startswith("if ($StartDirect)"), f"the direct start is not inside the StartDirect branch: {branch}"
    flow = _flow_lines()
    code = "\n".join(line for _, line in flow)
    assert not any("wscript.exe" in line and line.startswith("Start-Process") for _, line in flow)
    # Review round 2: directly only from the desktop user's own desktop run; another account (an
    # admin's password typed at the UAC prompt) goes through the task as well
    assert "$startDirect = $interactive -and ($env:USERNAME -ieq $desktopUser)" in code
    assert "Start-RemoteOSServer -ConfigDir $ConfigDir -StartDirect $startDirect" in code
    assert "Test-RemoteOSServerHealth -Port $Port -Version $preflight.Version -StartDirect $startDirect" in code
    health = _function_code("Test-RemoteOSServerHealth")
    session = next(i for i, line in enumerate(health) if "Get-RemoteOSListenerSessionId" in line)
    direct_ok = next(i for i, line in enumerate(health) if line.startswith("if ($StartDirect) {"))
    assert direct_ok < session, "the task's server is not proven to run in a desktop session"


def test_another_account_s_python_is_refused_before_anything_changes() -> None:
    """Review round 3: elevated as another account, a per-user Python of that account (found, or
    installed now) is unreachable for the desktop user's task: stop before any change."""
    flow = _flow_lines()
    check = [n for n, line in flow if line.startswith("if (-not (Test-RemoteOSPythonReachable")]
    assert len(check) == 1, check
    assert any(line == "$otherAccount = $env:USERNAME -ine $desktopUser" for _, line in flow)
    changes = [n for n, line in flow if any(change in line for change in _STATE_CHANGES)]
    assert check[0] < min(changes), (check[0], min(changes))


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
    # strings removed first ("... exit code ..." in a message is not an exit); then a return or
    # exit as a statement anywhere on the line, also inside `{ ... }` (review round 3)
    unquoted = [(n, re.sub(r'"[^"]*"|\'[^\']*\'', '""', line)) for n, line in after_gate]
    bare = [(n, line) for n, line in unquoted if re.search(r"(^|[;{]\s*)(return|exit)\b", line)]
    assert bare == [], f"exits that bypass Exit-RemoteOSInstaller: {bare}"
    content = INSTALLER_WINDOWS.read_text()
    for message in ("[X] could not download remoteos-mcp", "[X] Could not install Python"):
        start = content.index(message)
        block = content[start:content.index("\n}", start)]
        assert "Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit" in block, (message, block)


def _stop_line() -> int:
    stops = [n for n, line in _flow_lines() if line.startswith("Stop-RemoteOSServer")]
    assert len(stops) == 1, stops
    return stops[0]


def test_every_failure_after_the_stop_rolls_back_before_it_exits() -> None:
    """#64: a failure after the server was stopped (pip, the import check, the task, the firewall,
    the start, the health proof) reinstalls the previous wheelhouse before it exits 1."""
    after_stop = [line for n, line in _flow_lines() if n > _stop_line()]
    assert not any("Exit-RemoteOSInstaller" in line for line in after_stop), "an exit after the stop skips the rollback"
    failures = [line for line in after_stop if line.startswith("Exit-RemoteOSAfterFailure ")]
    assert len(failures) >= 5 and all(line.endswith("@rollback") for line in failures), failures
    body = _function_code("Exit-RemoteOSAfterFailure")
    assert sum(line == "Exit-RemoteOSInstaller -Code 1 -Pause $Pause" for line in body) == 2, body
    assert any("Invoke-RemoteOSRollback" in line for line in body)


# --- #12: the code and its constraints come from ONE commit ------------------------------------

def test_the_constraints_and_the_source_archive_come_from_one_resolved_commit() -> None:
    """install.ps1 fetched constraints.txt from raw .../main/ and installed archive/main.zip: two
    requests for "main", and raw caches /main/ for minutes, so a merge that changes the pins could
    pair new code with old constraints (#12). Now $Ref resolves to ONE sha that both URLs use."""
    flow = _flow_lines()
    code = "\n".join(line for _, line in flow)
    # #64: the preflight downloads both into the commit's wheelhouse; the install reads only that
    assert '-SourceUrl "https://github.com/zbynekdrlik/remoteos-mcp/archive/$sourceSha.zip"' in code
    assert ('-ConstraintsUrl "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/$sourceSha/constraints.txt"'
            in code)
    assert "$env:PIP_CONSTRAINT" not in code, "a URL constraint would make the offline install reach the network"
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


# --- #64: download first, install offline, roll back ------------------------------------------

def test_the_download_comes_before_the_stop_and_the_uninstall() -> None:
    """install.ps1 stopped the server and pre-uninstalled BEFORE its one online pip install: a
    read timeout on PyPI then left resolume-pp without MCP (~10 min, then ~1 h). Now every wheel
    is in the commit's wheelhouse before anything changes, and a failed download exits 1."""
    flow = _flow_lines()
    preflight = [n for n, line in flow if line.startswith("$preflight = New-RemoteOSWheelhouse -Python $python")]
    assert len(preflight) == 1, preflight
    # A Python install (winget, the python.org installer) may come first: the download needs a
    # Python, and installing one leaves the running server alone
    python_install = ("winget install", "curl.exe", "& $installerPath")
    changes = [n for n, line in flow
               if any(change in line for change in _STATE_CHANGES if change not in python_install)]
    assert preflight[0] < min(changes), (preflight[0], min(changes))
    block = _if_block("if (-not $preflight.Ok) {")
    assert any("-- nothing was changed on this box" in line for _, line in block), block
    assert block[-2][1] == "Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit", block
    read = "$rollbackPoint = Get-RemoteOSRollbackPoint -ConfigDir $ConfigDir -Root $wheelhouseRoot -Python $python"
    point = [n for n, line in flow if line == read]
    assert point and max(point) < _stop_line() and preflight[0] < point[0], (point, _stop_line())


def test_the_preflight_downloads_with_timeouts_and_builds_every_source_archive() -> None:
    body = "\n".join(_function_code("New-RemoteOSWheelhouse"))
    assert "@('download', '--timeout', '60', '--retries', '10', '-c', $constraints, '-d', $partial, $SourceUrl)" in body
    assert "@('wheel', '--no-deps', '--timeout', '60', '--retries', '10', '-c', $constraints, '-w', $partial)" in body
    assert "Move-Item -LiteralPath $partial -Destination $final" in body, "a wheelhouse is not whole-or-absent"


def test_the_install_is_offline_from_the_wheelhouse() -> None:
    body = _function_code("Install-RemoteOSWheelhouse")
    installs = [line for line in body if "@('install'" in line]
    assert installs == ["$install = Invoke-RemoteOSPip $Python @('install', '--no-index', '--find-links', $Wheelhouse,"
                        " '-c', (Join-Path $Wheelhouse 'constraints.txt'), \"remoteos-mcp==$Version\")"], installs
    uninstall = next(i for i, line in enumerate(body)
                     if "@('uninstall', '-y', 'remoteos-mcp', 'fastmcp', 'fastmcp-slim')" in line)
    assert uninstall < body.index(installs[0]), "the pre-uninstall (#12 RECORD clash) must come first"
    code = "\n".join(line for _, line in _code_lines())
    assert "--force-reinstall" not in code
    flow = "\n".join(line for _, line in _flow_lines())
    assert ("$install = Install-RemoteOSWheelhouse -Python $python -Wheelhouse $preflight.Path"
            " -Version $preflight.Version") in flow


def test_old_wheelhouses_are_pruned_only_after_the_proven_install() -> None:
    flow = _flow_lines()
    health = next(n for n, line in flow if line.startswith("$health = Test-RemoteOSServerHealth"))
    ref = next(n for n, line in flow if "installed-ref.txt" in line and "Set-Content" in line)
    prune = [n for n, line in flow
             if "Remove-RemoteOSStaleWheelhouses -Root $wheelhouseRoot -Keep @($sourceSha, $rollbackPoint.Ref)" in line]
    assert len(prune) == 1 and health < ref < prune[0], (health, ref, prune)


def test_python_3_11_or_newer_is_required() -> None:
    """#64 (decided on #39): Pillow 13, the fixed release of its CVEs, needs Python 3.11."""
    code = "\n".join(line for _, line in _code_lines())
    assert 'if (Test-RemoteOSPythonVersion "$ver") {' in code
    assert "-ge 10" not in code and "3.10+" not in code
    assert "-ge 11" in "\n".join(_function_code("Test-RemoteOSPythonVersion"))


# --- #64 review round 1 -----------------------------------------------------------------------

def test_the_wheelhouse_root_is_protected_before_the_download() -> None:
    """The wheelhouses feed an elevated pip from the desktop user's profile: the root gets a
    protected ACL (SYSTEM + Administrators, the user read-only) before anything is downloaded,
    and a root that cannot be protected stops the run with nothing changed."""
    flow = _flow_lines()
    call = "$protectError = Protect-RemoteOSWheelhouseRoot -Root $wheelhouseRoot -User $desktopUser }"
    protect = [n for n, line in flow if line.endswith(call)]
    preflight = next(n for n, line in flow if line.startswith("$preflight = New-RemoteOSWheelhouse"))
    assert len(protect) == 1 and protect[0] < preflight, (protect, preflight)
    block = _if_block("if ($protectError) {")
    # (an untrusted root may already be gone: "nothing was changed" would not be true, review round 2)
    assert any("-- the running server and package were not touched" in line for _, line in block), block
    assert block[-2][1] == "Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit", block


def test_an_inherited_url_constraint_never_reaches_the_offline_install() -> None:
    """A PIP_CONSTRAINT URL the old installer left in the same console would make pip reach the
    network even with --no-index."""
    lines = _code_lines()
    removed = [n for n, line in lines if line.startswith("Remove-Item Env:PIP_CONSTRAINT")]
    preflight = next(n for n, line in _flow_lines() if line.startswith("$preflight = New-RemoteOSWheelhouse"))
    assert len(removed) == 1 and removed[0] < preflight, (removed, preflight)


def test_both_wheelhouses_are_proven_installable_before_the_stop() -> None:
    """A wheelhouse built for another interpreter failed only AFTER the stop, and so did a
    rollback from it: both get an offline dry run first."""
    check = _function_code("Test-RemoteOSWheelhouseInstallable")
    assert any("'--dry-run', '--ignore-installed', '--no-index', '--find-links', $Wheelhouse" in line for line in check)
    for name in ("New-RemoteOSWheelhouse", "Get-RemoteOSRollbackPoint"):
        assert any("Test-RemoteOSWheelhouseInstallable" in line for line in _function_code(name)), name


def test_an_installed_python_is_checked_for_3_11_too() -> None:
    """winget / the python.org installer could leave an older Python first on PATH."""
    flow = _flow_lines()
    install = next(n for n, line in flow if line.startswith("winget install Python.Python"))
    check = [n for n, line in flow if line == 'if (-not (Test-RemoteOSPythonVersion "$installedVersion")) {']
    assert len(check) == 1 and check[0] > install, (install, check)
    block = _if_block('if (-not (Test-RemoteOSPythonVersion "$installedVersion")) {')
    assert block[-2][1] == "Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit", block


def test_a_failing_firewall_rule_leaves_the_old_rule_in_place() -> None:
    """The old rule went first: a failing New-NetFirewallRule then rolled back to a server the
    LAN could no longer reach."""
    lines = INSTALLER_WINDOWS.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "[7/7] Configuring firewall" in line)
    block = [line.strip() for line in lines[start:start + 25]]
    new = next(i for i, line in enumerate(block) if line.startswith('New-NetFirewallRule -DisplayName "RemoteOS MCP"'))
    removes = [i for i, line in enumerate(block) if "Remove-NetFirewallRule" in line]
    assert removes and min(removes) > new, block


def test_the_installed_version_is_read_from_a_marked_line() -> None:
    """A late warning on the merged output must not read as the version (a needless rollback)."""
    body = "\n".join(_function_code("Get-RemoteOSInstalledVersion"))
    assert "REMOTEOS_VER=" in body


def test_an_elevated_delete_never_follows_a_link() -> None:
    """Review rounds 2-3: Windows PowerShell 5.1's Remove-Item -Recurse enters a junction, and the
    run is elevated. Every recursive delete in install.ps1 goes through Remove-RemoteOSTree (.NET,
    link-safe): the wheelhouse code AND the old .winremote-mcp / other-account config cleanups."""
    recursive = [(n, line) for n, line in _code_lines() if "Remove-Item" in line and "-Recurse" in line]
    assert recursive == [], f"an elevated Remove-Item -Recurse is left: {recursive}"
    for name in ("Protect-RemoteOSWheelhouseRoot", "New-RemoteOSWheelhouse", "Remove-RemoteOSStaleWheelhouses"):
        body = _function_code(name)
        assert not any("Remove-Item" in line and "-Recurse" in line for line in body), name
        assert any("Remove-RemoteOSTree" in line for line in body), name
    # .NET Framework's recursive Directory.Delete fails on a junction inside ("The parameter is
    # incorrect", CI on 12e8902): the tree is walked entry by entry, a link removed alone
    tree = "\n".join(_function_code("Remove-RemoteOSTree"))
    assert "[System.IO.Directory]::Delete($Path)" in tree and "Delete($Path, $true)" not in tree
    assert "Remove-RemoteOSTree $entry.FullName" in tree


def test_account_names_are_qualified_where_computer_and_user_share_a_name():
    # On ABLETON-FOH the account Ableton-FOH does not translate by its bare name (#57).
    code = "\n".join(line for _, line in _code_lines())
    assert "function Get-RemoteOSAccount" in code
    assert "NTAccount($User)" not in code
    assert "-UserId $User" not in code
    assert "-UserId $desktopUser" not in code
