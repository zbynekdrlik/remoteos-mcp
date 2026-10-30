# --- The per-box updater (#65): the ring and the daily task --------------------------------------

function Test-RemoteOSRing([string]$Ring) {
    # Empty = keep what the box has
    return [bool](-not $Ring -or $Ring -ceq 'stable' -or $Ring -ceq 'canary')
}

function Set-RemoteOSRing([string]$ConfigDir, [string]$Ring) {
    # updater.json: an explicit ring wins, else what the box has stays, else stable. No BOM: Python reads it.
    $path = Join-Path $ConfigDir 'updater.json'
    if (-not $Ring -and (Test-Path -LiteralPath $path)) { return }
    if (-not $Ring) { $Ring = 'stable' }
    [System.IO.File]::WriteAllText($path, "{`"ring`": `"$Ring`"}`n", (New-Object System.Text.UTF8Encoding($false)))
}

function Get-RemoteOSUpdaterAction([string]$PythonPath, [string]$ConfigDir) {
    # pythonw.exe: a console window every day would steal the focus on a live desktop -> @{Execute; Argument}
    $execute = $PythonPath
    $windowless = Join-Path (Split-Path -Parent $PythonPath) 'pythonw.exe'
    if (Test-Path -LiteralPath $windowless) { $execute = $windowless }
    return [pscustomobject]@{ Execute = $execute
                              Argument = "-m remoteos.updater --log-file `"$(Join-Path $ConfigDir 'updater.log')`"" }
}

function Register-RemoteOSUpdaterTask([string]$ConfigDir, [string]$PythonPath, [string]$User) {
    # Daily at a random time; StartWhenAvailable catches up a date missed while the box was off. The desktop
    # user, highest, like RemoteOSMCP-Upgrade (the updater starts that task). An unchanged task stays as it
    # is, with its time. -> $true when it was (re)registered
    $wanted = Get-RemoteOSUpdaterAction -PythonPath $PythonPath -ConfigDir $ConfigDir
    $existing = Get-ScheduledTask -TaskName "RemoteOSMCP-Updater" -ErrorAction SilentlyContinue
    if ($existing) {
        $current = @($existing.Actions)[0]
        if ($current.Execute -ceq $wanted.Execute -and $current.Arguments -ceq $wanted.Argument) { return $false }
    }
    $action = New-ScheduledTaskAction -Execute $wanted.Execute -Argument $wanted.Argument
    $trigger = New-ScheduledTaskTrigger -Daily -At '03:00' -RandomDelay (New-TimeSpan -Hours 6)
    $principal = New-ScheduledTaskPrincipal -UserId (Get-RemoteOSAccountName $User) -RunLevel Highest -LogonType Interactive
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    $task = @{ TaskName = "RemoteOSMCP-Updater"; Action = $action; Trigger = $trigger; Principal = $principal; Settings = $settings; Force = $true }
    Register-ScheduledTask @task | Out-Null
    return $true
}

