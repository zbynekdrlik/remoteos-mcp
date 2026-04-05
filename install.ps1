# RemoteOS MCP - One-line installer
# Usage: irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/install.ps1 | iex

$ErrorActionPreference = "Stop"
$Port = 8090

Write-Host ""
Write-Host "  RemoteOS MCP Installer" -ForegroundColor Cyan
Write-Host "  Remote Windows control for Claude Code" -ForegroundColor Gray
Write-Host ""

# --- Check admin ---
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "  [!] Not running as admin - firewall rule and auto-start may fail" -ForegroundColor Yellow
    Write-Host "  [!] Recommend: Right-click PowerShell > Run as Administrator" -ForegroundColor Yellow
    Write-Host ""
}

# --- Detect logged-in desktop user (may differ from SSH user) ---
$desktopUser = $null
try {
    $sessions = query user 2>&1
    foreach ($line in $sessions) {
        if ($line -match "console" -and $line -match "Active") {
            $desktopUser = ($line.Trim() -split "\s+")[0].TrimStart(">")
            break
        }
    }
} catch {}
if (-not $desktopUser) { $desktopUser = $env:USERNAME }

$desktopProfile = "C:\Users\$desktopUser"
if (-not (Test-Path $desktopProfile)) { $desktopProfile = $env:USERPROFILE }
$ConfigDir = "$desktopProfile\.remoteos-mcp"

Write-Host "  Target user: $desktopUser ($desktopProfile)" -ForegroundColor Gray
Write-Host ""

# --- Find or install Python ---
Write-Host "  [1/6] Checking Python..." -ForegroundColor White
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) {
            $python = $cmd
            Write-Host "        Found $ver" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $python) {
    Write-Host "        Python 3.10+ not found. Installing..." -ForegroundColor Yellow

    # Try winget first
    $installed = $false
    try {
        $wv = winget --version 2>&1
        if ($wv -match "v\d") {
            winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            $python = "python"
            $installed = $true
            Write-Host "        Python installed (winget)" -ForegroundColor Green
        }
    } catch {}

    # Fallback: direct download
    if (-not $installed) {
        Write-Host "        winget not available, downloading directly..." -ForegroundColor Yellow
        $installerPath = "$env:TEMP\python-3.12.10-amd64.exe"
        try {
            curl.exe -Lo $installerPath "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
            & $installerPath /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
            Start-Sleep -Seconds 5
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            # Find the freshly installed python
            foreach ($cmd in @("python", "python3")) {
                try {
                    $ver = & $cmd --version 2>&1
                    if ($ver -match "Python 3") { $python = $cmd; break }
                } catch {}
            }
            # Check common install paths
            if (-not $python) {
                $tryPaths = @(
                    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                    "C:\Users\$desktopUser\AppData\Local\Programs\Python\Python312\python.exe",
                    "C:\Python312\python.exe"
                )
                foreach ($p in $tryPaths) {
                    if (Test-Path $p) { $python = $p; break }
                }
            }
            if ($python) {
                Write-Host "        Python installed (direct download)" -ForegroundColor Green
            }
            Remove-Item $installerPath -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Host "        [X] Direct download failed: $_" -ForegroundColor Red
        }
    }

    if (-not $python) {
        Write-Host "        [X] Could not install Python. Install manually from python.org" -ForegroundColor Red
        return
    }
}

# --- Install remoteos-mcp ---
Write-Host "  [2/6] Installing remoteos-mcp..." -ForegroundColor White
# pip warnings on stderr must not abort the script
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
# Uninstall old winremote-mcp if present
$oldPkg = & $python -m pip show winremote-mcp 2>&1 | Out-String
if ($oldPkg -match "Version:") {
    Write-Host "        Removing old winremote-mcp..." -ForegroundColor Yellow
    & $python -m pip uninstall winremote-mcp -y 2>&1 | Out-Null
}
# Clean up old config and scheduled task
$oldConfigDir = "$desktopProfile\.winremote-mcp"
if (Test-Path $oldConfigDir) {
    Remove-Item -Recurse -Force $oldConfigDir -ErrorAction SilentlyContinue
    Write-Host "        Cleaned up old .winremote-mcp config" -ForegroundColor Gray
}
Unregister-ScheduledTask -TaskName "WinRemoteMCP" -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName "WinRemote MCP" -ErrorAction SilentlyContinue
& $python -m pip install --no-cache-dir "https://github.com/zbynekdrlik/remoteos-mcp/archive/master.zip" 2>&1 | Out-Null
$pipShow = & $python -m pip show remoteos-mcp 2>&1 | Out-String
$ErrorActionPreference = $prevEAP
if ($pipShow -match "Version: (.+)") {
    Write-Host "        Installed v$($Matches[1].Trim())" -ForegroundColor Green
} else {
    Write-Host "        [X] pip install failed" -ForegroundColor Red
    Write-Host "        Try manually: $python -m pip install https://github.com/zbynekdrlik/remoteos-mcp/archive/master.zip" -ForegroundColor Yellow
    return
}

# --- Generate or preserve auth key ---
Write-Host "  [3/6] Configuring auth key..." -ForegroundColor White
$ExistingKey = $null
if (Test-Path "$ConfigDir\config.json") {
    try {
        $existingConfig = Get-Content "$ConfigDir\config.json" -Raw | ConvertFrom-Json
        $ExistingKey = $existingConfig.auth_key
        Write-Host "        Reusing existing auth key" -ForegroundColor Green
    } catch {
        Write-Host "        Could not read existing config, generating new key" -ForegroundColor Yellow
    }
}
if (-not $ExistingKey) {
    $ExistingKey = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
    Write-Host "        Generated new auth key" -ForegroundColor Green
}
$AuthKey = $ExistingKey

# --- Create config and start scripts ---
Write-Host "  [4/6] Creating config..." -ForegroundColor White
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}

# Find the actual python executable path for the batch file
$pythonPath = (Get-Command $python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) { $pythonPath = $python }

# Config JSON
@{
    port     = $Port
    auth_key = $AuthKey
    host     = "0.0.0.0"
} | ConvertTo-Json | Set-Content "$ConfigDir\config.json"

# Start batch script (with auto-restart loop and crash guard)
@"
@echo off
title RemoteOS MCP (port $Port)
echo.
echo  RemoteOS MCP Server
echo  Port: $Port
echo.
set FAILURES=0
:loop
REM Check if port is already in use (another instance running)
netstat -ano | findstr "0.0.0.0:$Port.*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo  [%date% %time%] Port $Port already in use. Exiting to avoid duplicates.
    exit /b 0
)
echo  [%date% %time%] Starting server...
set START_TIME=%time%
"$pythonPath" -m remoteos --transport streamable-http --enable-all --host 0.0.0.0 --port $Port --auth-key "$AuthKey"
set EXIT_CODE=%errorlevel%
echo  [%date% %time%] Server exited (code %EXIT_CODE%).
REM Crash guard: if server ran less than 30 seconds, count as rapid failure
call :elapsed_check
if %RAPID%==1 (
    set /a FAILURES+=1
    echo  [%date% %time%] Rapid failure %FAILURES%/5.
) else (
    set FAILURES=0
)
if %FAILURES% geq 5 (
    echo  [%date% %time%] Too many rapid failures. Stopping restart loop.
    echo  [%date% %time%] Check logs and restart manually or wait for scheduled task.
    exit /b 1
)
echo  [%date% %time%] Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop

:elapsed_check
REM Simple check: if start hour:minute matches current, it was rapid
set RAPID=0
set CUR_MIN=%time:~3,2%
set START_MIN=%START_TIME:~3,2%
if "%CUR_MIN%"=="%START_MIN%" set RAPID=1
goto :eof
"@ | Set-Content "$ConfigDir\start-remoteos.bat"

# VBS launcher to run batch file hidden (no CMD window)
@"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """" & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\start-remoteos.bat""", 0, False
"@ | Set-Content "$ConfigDir\start-remoteos.vbs"

Write-Host "        Config: $ConfigDir\config.json" -ForegroundColor Gray
Write-Host "        Start:  $ConfigDir\start-remoteos.vbs (hidden)" -ForegroundColor Gray

# --- Auto-start scheduled task ---
Write-Host "  [5/6] Setting up auto-start..." -ForegroundColor White
try {
    $taskAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$ConfigDir\start-remoteos.vbs`""
    # Trigger at logon + every 5 minutes (task won't start a second instance if already running)
    $triggerLogon = New-ScheduledTaskTrigger -AtLogon
    $triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $desktopUser -RunLevel Highest -LogonType Interactive
    $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "RemoteOSMCP" -Action $taskAction -Trigger @($triggerLogon, $triggerRepeat) -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
    Write-Host "        Task 'RemoteOSMCP' registered for $desktopUser (at logon + every 5 min)" -ForegroundColor Green
} catch {
    Write-Host "        [!] Could not create scheduled task (need admin)" -ForegroundColor Yellow
    Write-Host "        You can start manually: $ConfigDir\start-remoteos.vbs" -ForegroundColor Yellow
}

# --- Firewall + Network profile ---
Write-Host "  [6/6] Configuring firewall..." -ForegroundColor White
try {
    # Ensure network is Private (firewall rule only allows Private/Domain)
    Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object {
        $_.NetworkCategory -eq "Public"
    } | Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
    # Remove old rule if exists
    Remove-NetFirewallRule -DisplayName "RemoteOS MCP" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "RemoteOS MCP" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -Profile Private,Domain | Out-Null
    Write-Host "        Firewall rule added (port $Port, private/domain networks)" -ForegroundColor Green
} catch {
    Write-Host "        [!] Could not add firewall rule (need admin)" -ForegroundColor Yellow
    Write-Host "        Manually allow TCP port $Port inbound" -ForegroundColor Yellow
}

# --- Get LAN IP ---
$localIP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Sort-Object -Property InterfaceIndex |
    Select-Object -First 1).IPAddress

if (-not $localIP) { $localIP = "WINDOWS_IP" }
$hostName = $env:COMPUTERNAME.ToLower()

# --- Stop old server processes ---
Write-Host ""
Write-Host "  Stopping old server..." -ForegroundColor Cyan
# Kill by window title (catches python/cmd with the batch title, avoids killing unrelated python)
Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -match "RemoteOS|WinRemote"
} | Stop-Process -Force -ErrorAction SilentlyContinue
# Kill server python by port (only the one on our port, not other pythons)
$portPid = (netstat -ano | Select-String "0.0.0.0:$Port.*LISTENING" | ForEach-Object {
    ($_.ToString().Trim() -split "\s+")[-1]
}) | Select-Object -First 1
if ($portPid) {
    Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

# --- Clean up old config from other users (if installer was run under wrong user before) ---
$currentUserConfig = "$env:USERPROFILE\.remoteos-mcp"
if ($currentUserConfig -ne $ConfigDir -and (Test-Path $currentUserConfig)) {
    Remove-Item -Recurse -Force $currentUserConfig -ErrorAction SilentlyContinue
    Write-Host "  Cleaned up old config from $env:USERNAME" -ForegroundColor Gray
}

# --- Start server now (hidden) ---
Write-Host "  Starting server..." -ForegroundColor Cyan
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$ConfigDir\start-remoteos.vbs`""
Start-Sleep -Seconds 5

# Test if it's running
$running = $false
try {
    $response = Invoke-WebRequest -Uri "http://localhost:$Port" -Method GET -TimeoutSec 5 -ErrorAction SilentlyContinue
    $running = $true
} catch {
    # Even a 404/401 means the server is up
    if ($_.Exception.Response) { $running = $true }
}

if ($running) {
    Write-Host "  Server is running!" -ForegroundColor Green
} else {
    Write-Host "  [!] Server may still be starting... check Task Manager for python" -ForegroundColor Yellow
}

# --- Summary ---
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "  SETUP COMPLETE" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Computer:  $hostName ($localIP)" -ForegroundColor White
Write-Host "  User:      $desktopUser" -ForegroundColor White
Write-Host "  Port:      $Port" -ForegroundColor White
Write-Host "  Auth Key:  $AuthKey" -ForegroundColor Yellow
Write-Host ""
Write-Host "  On your Linux machine, run:" -ForegroundColor White
Write-Host ""
Write-Host "  claude mcp add --transport http win-$hostName ``" -ForegroundColor Green
Write-Host "    http://${localIP}:${Port}/mcp ``" -ForegroundColor Green
Write-Host "    --header ""Authorization: Bearer ${AuthKey}""" -ForegroundColor Green
Write-Host ""
Write-Host "  Then restart Claude Code." -ForegroundColor Gray
Write-Host ""
Write-Host "  To uninstall later:" -ForegroundColor Gray
Write-Host "  irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/master/uninstall.ps1 | iex" -ForegroundColor Gray
Write-Host ""
