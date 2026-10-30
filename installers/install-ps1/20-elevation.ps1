# --- The elevation gate (#57): an elevated run, or nothing changes -----------------------------
# A person at the desktop is relaunched elevated (one UAC click); automation (SSH, session 0) and an
# explicit REMOTEOS_MCP_AUTH_KEY (it never crosses the elevation) fail with nothing changed.

function Get-RemoteOSElevationAction([Parameter(Mandatory)][bool]$IsAdmin, [Parameter(Mandatory)][bool]$Interactive,
                                     [Parameter(Mandatory)][bool]$ExplicitKey, [Parameter(Mandatory)][bool]$Relaunched) {
    # Mandatory inputs: a dropped argument is never read as $false. -> 'proceed' | 'relaunch' | 'fail' (pure).
    # A relaunch that is still not elevated (UAC off, a standard user) fails, so relaunches never chain.
    if ($IsAdmin) { return 'proceed' }
    if ($Interactive -and -not $ExplicitKey -and -not $Relaunched) { return 'relaunch' }
    return 'fail'
}

function Test-RemoteOSInteractiveSession([string]$SshConnection, [string]$SshClient, [Parameter(Mandatory)][int]$SessionId,
                                         [Parameter(Mandatory)][bool]$InputRedirected) {
    # A person at the desktop: not SSH, not session 0, stdin the console (else nobody answers UAC)
    return [bool]((-not $SshConnection) -and (-not $SshClient) -and $SessionId -ne 0 -and -not $InputRedirected)
}

function Test-RemoteOSPythonReachable([string]$PythonPath, [string]$RunProfile, [bool]$OtherAccount) {
    # Can the desktop user's task run this Python? Not one in ANOTHER elevated account's profile (per-user,
    # the WindowsApps alias), nor none at all when this run would install one per-user for that account.
    if (-not $OtherAccount) { return $true }
    if (-not $PythonPath) { return $false }
    return -not $PythonPath.StartsWith($RunProfile.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Exit-RemoteOSInstaller([int]$Code, [bool]$Pause) {
    # exit $Code; with $Pause wait for Enter first (a Read-Host that cannot read must not keep the run alive)
    if ($Pause) { try { Read-Host "  Press Enter to close" | Out-Null } catch {} }
    exit $Code
}

function Get-RemoteOSListenerSessionId([int]$Port) {
    # The Windows session of the process listening on $Port, $null when nothing listens
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    return [int]$process.SessionId
}

function Test-RemoteOSRefName([string]$Ref) {
    # A plain git ref name (#12): it goes into a URL and into the relaunch's command
    return [bool]($Ref -and $Ref.Length -le 200 -and $Ref -cmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*\z' -and
        $Ref -notmatch '\.\.')
}

function Get-RemoteOSRelaunchCommand([string]$ScriptPath, [string]$InstallerUrl, [string]$SourceRef, [string]$Ring) {
    # The elevated window's script: the file again, or the resolved commit's installer for a piped run, with
    # $SourceRef in REMOTEOS_MCP_REF (no environment crosses). A CHILD powershell, then Enter to close.
    if ($ScriptPath) {
        $run = "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File '" + $ScriptPath.Replace("'", "''") + "'"
    } else {
        $inner = "irm '" + $InstallerUrl.Replace("'", "''") + "' | iex"
        $run = "& powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '" + $inner.Replace("'", "''") + "'"
    }
    return @(
        ("`$env:REMOTEOS_MCP_REF = '" + $SourceRef.Replace("'", "''") + "'"),
        '$env:REMOTEOS_MCP_RELAUNCHED = "1"',
        ("`$env:REMOTEOS_MCP_RING = '" + $Ring + "'"),  # checked by the flow; empty = keep the box's
        $run,
        '$code = $LASTEXITCODE',
        'Write-Host ""',
        'Read-Host "RemoteOS MCP installer finished (exit code $code) - press Enter to close this window" | Out-Null',
        'exit $code'
    ) -join "`n"
}

function Invoke-RemoteOSElevatedRelaunch([string]$ScriptPath, [string]$InstallerUrl, [string]$SourceRef, [string]$Ring) {
    # Elevated window (UAC) -> its exit code, or $null when cancelled/refused; -EncodedCommand: no quoting.
    $command = Get-RemoteOSRelaunchCommand -ScriptPath $ScriptPath -InstallerUrl $InstallerUrl -SourceRef $SourceRef -Ring $Ring
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
    # -PassThru + WaitForExit, not -Wait (5.1's -Wait also waits for the server the run leaves)
    try {
        $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -PassThru `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded)
    } catch {
        Write-Host "  [X] The elevation was cancelled or failed ($_) -- nothing was changed on this box" -ForegroundColor Red
        return $null
    }
    if (-not $process) {
        Write-Host "  [X] The elevated PowerShell did not start -- nothing was changed on this box" -ForegroundColor Red
        return $null
    }
    $null = $process.Handle  # keep the process handle: ExitCode needs it after the exit
    $process.WaitForExit()
    return [int]$process.ExitCode
}

