# --- SelfUpgrade (#71): the elevated task the server starts, for the desktop user -----------------
# The command is fixed here, at install time, and goes over as -EncodedCommand (no quoting, no file the
# user could change). The ref comes through upgrade-ref.txt next to config.json and is checked again.

function Get-RemoteOSUpgradeCommand([string]$ConfigDir) {
    # REMOTEOS_MCP_RELAUNCHED: the task IS the elevated run. Nobody is at this hidden window, so the
    # installer must neither relaunch for UAC nor wait for Enter. No key anywhere: it reads the box's.
    return @(
        '$ErrorActionPreference = "Stop"',
        ("`$configDir = '" + $ConfigDir.Replace("'", "''") + "'"),
        'Start-Transcript -LiteralPath (Join-Path $configDir "upgrade.log") -Force | Out-Null',
        '$ref = "main"',
        '$refFile = Join-Path $configDir "upgrade-ref.txt"',
        'if (Test-Path -LiteralPath $refFile) {',
        '    $ref = ([string](Get-Content -LiteralPath $refFile -Raw)).Trim()',
        '    Remove-Item -LiteralPath $refFile -Force',
        '}',
        'if ($ref.Length -gt 100 -or $ref -cnotmatch "^[A-Za-z0-9][A-Za-z0-9._/-]*\z" -or $ref -match "\.\.") {',
        '    Write-Host "  [X] upgrade-ref.txt holds no plain branch, tag or commit name -- nothing was changed on this box"',
        '    exit 1',
        '}',
        '$env:REMOTEOS_MCP_REF = $ref',
        '$env:REMOTEOS_MCP_RELAUNCHED = "1"',
        '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12',
        'Invoke-RestMethod -Uri "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/$ref/install.ps1" -TimeoutSec 60 | Invoke-Expression',
        'Stop-Transcript | Out-Null'
    ) -join "`n"
}

function Get-RemoteOSUpgradeTaskArguments([string]$ConfigDir) {
    $command = Get-RemoteOSUpgradeCommand -ConfigDir $ConfigDir
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
    return "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encoded"
}

function Register-RemoteOSUpgradeTask([string]$ConfigDir, [string]$User) {
    # On demand: no trigger, schtasks /Run. Like RemoteOSMCP: the desktop user, highest level. An unchanged
    # task stays as it is: a SelfUpgrade run is inside it. -> $true when it was (re)registered
    $arguments = Get-RemoteOSUpgradeTaskArguments -ConfigDir $ConfigDir
    $existing = Get-ScheduledTask -TaskName "RemoteOSMCP-Upgrade" -ErrorAction SilentlyContinue
    if ($existing -and @($existing.Actions)[0].Arguments -ceq $arguments) { return $false }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $principal = New-ScheduledTaskPrincipal -UserId $User -RunLevel Highest -LogonType Interactive
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $task = @{ TaskName = "RemoteOSMCP-Upgrade"; Action = $action; Principal = $principal; Settings = $settings; Force = $true }
    Register-ScheduledTask @task | Out-Null
    return $true
}

