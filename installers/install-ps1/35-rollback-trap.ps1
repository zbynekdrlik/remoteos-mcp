# --- The rollback trap (#65): a run killed after the stop must not leave a box with no server ------
# PowerShell has no EXIT trap for a killed process, and an SSH drop kills every child of the session. So
# a scheduled task watches the installer from outside: a marker next to config.json says "armed", and when
# the installer's process is gone while the marker is still there, the task rolls back, offline.

function Get-RemoteOSDataDir {
    # Machine-wide, not in a profile: the wheelhouses and the trap's script feed elevated runs
    return Join-Path $env:ProgramData 'remoteos-mcp'
}

function Get-RemoteOSQuoted([string]$Text) {
    return "'" + $Text.Replace("'", "''") + "'"
}

function Enter-RemoteOSInstallLock {
    # One run at a time (#65): the updater makes a second one routine, and two pips break a box. The mutex
    # dies with its holder. -> the held mutex (keep it in a variable), or $null when another run holds it
    $mutex = New-Object System.Threading.Mutex($false, 'Global\RemoteOSMCP-Install')
    try {
        if ($mutex.WaitOne(0)) { return ,$mutex }
    } catch [System.Threading.AbandonedMutexException] {
        return ,$mutex  # its holder was killed: ours now
    }
    $mutex.Dispose()
    return $null
}

function Get-RemoteOSRollbackTrapScript($Point, [string]$Python, [string]$ConfigDir, [int]$Port, [int]$InstallerPid) {
    # The whole watcher as one script; the installer's own functions go in as text, so the rollback is the
    # same code as the installer's (and the script needs no download). Pure.
    $functions = foreach ($name in @('Enter-RemoteOSInstallLock', 'Invoke-RemoteOSNative', 'Invoke-RemoteOSPip', 'Get-RemoteOSInstalledVersion',
                                     'Install-RemoteOSWheelhouse', 'Stop-RemoteOSServer', 'Start-RemoteOSServer',
                                     'Get-RemoteOSListenerSessionId', 'Test-RemoteOSServerHealth', 'Invoke-RemoteOSRollback')) {
        "function $name {`n$((Get-Command $name).Definition)`n}"
    }
    return (@(
        '$ErrorActionPreference = "Stop"',
        "`$ConfigDir = $(Get-RemoteOSQuoted $ConfigDir)",
        "`$Port = $Port",
        "`$installerPid = $InstallerPid",
        "`$python = $(Get-RemoteOSQuoted $Python)",
        "`$point = [pscustomobject]@{ Ref = $(Get-RemoteOSQuoted $Point.Ref); Path = $(Get-RemoteOSQuoted $Point.Path); Version = $(Get-RemoteOSQuoted $Point.Version) }",
        '$marker = Join-Path $ConfigDir "rollback-pending.txt"',
        '$log = Join-Path $ConfigDir "rollback.log"',
        'Set-Content -LiteralPath (Join-Path $ConfigDir "rollback-watching.txt") -Value $PID'  # the heartbeat
    ) + @($functions) + @(
        # The deadline: a piped run stopped with Ctrl+C leaves its powershell alive, and so does a reused pid.
        '$deadline = (Get-Date).AddMinutes(30)',
        'while ((Test-Path -LiteralPath $marker) -and (Get-Process -Id $installerPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }',
        'if (-not (Test-Path -LiteralPath $marker)) { exit 0 }',
        # Free once the installer is dead, so held = a new run owns the box. An installer still alive at the
        # deadline holds it itself (a Ctrl+C in a piped run keeps the console): its box still needs the rollback.
        '$lock = Enter-RemoteOSInstallLock',
        'if (-not $lock -and -not (Get-Process -Id $installerPid -ErrorAction SilentlyContinue)) {',
        '    Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) The installer (pid $installerPid) died armed, but a new run holds the lock: left to it"',
        '    exit 0',
        '}',
        'Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) The installer (pid $installerPid) died armed: rolling back to v$($point.Version)"',
        'try {',
        '    $back = Invoke-RemoteOSRollback -Point $point -Python $python -StopServer { Stop-RemoteOSServer -Port $Port } `',
        '        -StartServer { Start-RemoteOSServer -ConfigDir $ConfigDir -StartDirect $false } `',
        '        -ProveHealth { param($Version) Test-RemoteOSServerHealth -Port $Port -Version $Version -StartDirect $false }',
        '} catch {',
        '    $back = [pscustomobject]@{ Ok = $false; Error = "$_"; Output = "" }',  # nobody reads a hidden task's stderr
        '}',
        'Remove-Item -LiteralPath $marker -Force',
        'if ($back.Ok) { Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) Rolled back to v$($point.Version): its health check passed"; exit 0 }',
        'Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) The rollback failed: $($back.Error)`n$($back.Output)"',
        'exit 1'
    )) -join "`n"
}

function Wait-RemoteOSRollbackTrap([string]$ConfigDir, [int]$Seconds = 20) {
    # schtasks /Run only requests a start (with nobody logged on, nothing runs): the heartbeat proves it
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Path -LiteralPath (Join-Path $ConfigDir 'rollback-watching.txt')) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-RemoteOSRollbackTrap($Point, [string]$Python, [string]$ConfigDir, [string]$DataDir, [int]$Port, [string]$User) {
    # Before the stop. The script is in the Administrators-only data folder, so the desktop user's task
    # cannot be fed another one; the marker and the log are the task's own. -> $null or why there is no trap
    $marker = Join-Path $ConfigDir 'rollback-pending.txt'
    $prevEAP = $ErrorActionPreference
    try {
        $script = Join-Path $DataDir 'rollback-trap.ps1'
        Get-RemoteOSRollbackTrapScript -Point $Point -Python $Python -ConfigDir $ConfigDir -Port $Port -InstallerPid $PID |
            Set-Content -LiteralPath $script -Encoding UTF8
        Set-Content -LiteralPath $marker -Value $PID
        Remove-Item -LiteralPath (Join-Path $ConfigDir 'rollback-watching.txt') -Force -ErrorAction SilentlyContinue
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
        $principal = New-ScheduledTaskPrincipal -UserId (Get-RemoteOSAccountName $User) -RunLevel Highest -LogonType Interactive
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
        $task = @{ TaskName = "RemoteOSMCP-Rollback"; Action = $action; Principal = $principal; Settings = $settings; Force = $true }
        Register-ScheduledTask @task | Out-Null
        $ErrorActionPreference = 'Continue'
        schtasks /End /TN "RemoteOSMCP-Rollback" 2>&1 | Out-Null  # a watcher of a dead run: this run owns the box
        $out = schtasks /Run /TN "RemoteOSMCP-Rollback" 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw "schtasks /Run failed: $($out.Trim())" }
        if (-not (Wait-RemoteOSRollbackTrap -ConfigDir $ConfigDir)) { throw "the watcher did not start" }
        return $null
    } catch {
        Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
        return "$_"
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

function Stop-RemoteOSRollbackTrap([string]$ConfigDir) {
    # The watcher sees the marker gone and exits
    Remove-Item -LiteralPath (Join-Path $ConfigDir 'rollback-pending.txt') -Force -ErrorAction SilentlyContinue
}

