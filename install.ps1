# WinRemote MCP - One-line installer
# Usage: irm https://raw.githubusercontent.com/zbynekdrlik/winremote-setup/main/install.ps1 | iex

$ErrorActionPreference = "Stop"
$Port = 8090
$ConfigDir = "$env:USERPROFILE\.winremote-mcp"

Write-Host ""
Write-Host "  WinRemote MCP Installer" -ForegroundColor Cyan
Write-Host "  Remote Windows control for Claude Code" -ForegroundColor Gray
Write-Host ""

# --- Check admin ---
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "  [!] Not running as admin - firewall rule and auto-start may fail" -ForegroundColor Yellow
    Write-Host "  [!] Recommend: Right-click PowerShell > Run as Administrator" -ForegroundColor Yellow
    Write-Host ""
}

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
    try {
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        $python = "python"
        Write-Host "        Python installed" -ForegroundColor Green
    } catch {
        Write-Host "        [X] Failed to install Python. Install manually from python.org" -ForegroundColor Red
        return
    }
}

# --- Install winremote-mcp ---
Write-Host "  [2/6] Installing winremote-mcp..." -ForegroundColor White
try {
    & $python -m pip install --upgrade --quiet winremote-mcp 2>&1 | Out-Null
    # Verify it installed
    $pipShow = & $python -m pip show winremote-mcp 2>&1
    if ($pipShow -match "Version: (.+)") {
        Write-Host "        Installed v$($Matches[1])" -ForegroundColor Green
    } else {
        Write-Host "        Installed" -ForegroundColor Green
    }
} catch {
    Write-Host "        [X] pip install failed: $_" -ForegroundColor Red
    return
}

# --- Generate auth key ---
Write-Host "  [3/6] Generating auth key..." -ForegroundColor White
$AuthKey = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

# --- Create config and start script ---
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

# Start batch script
@"
@echo off
title WinRemote MCP (port $Port)
echo.
echo  WinRemote MCP Server
echo  Port: $Port
echo  Press Ctrl+C to stop
echo.
"$pythonPath" -m winremote_mcp --host 0.0.0.0 --port $Port --auth-key "$AuthKey"
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Server failed to start. Check Python and winremote-mcp installation.
    pause
)
"@ | Set-Content "$ConfigDir\start-winremote.bat"

Write-Host "        Config: $ConfigDir\config.json" -ForegroundColor Gray
Write-Host "        Start:  $ConfigDir\start-winremote.bat" -ForegroundColor Gray

# --- Auto-start scheduled task ---
Write-Host "  [5/6] Setting up auto-start..." -ForegroundColor White
try {
    $taskAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$ConfigDir\start-winremote.bat`""
    $taskTrigger = New-ScheduledTaskTrigger -AtLogon
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive
    $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName "WinRemoteMCP" -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
    Write-Host "        Task 'WinRemoteMCP' registered (starts at logon)" -ForegroundColor Green
} catch {
    Write-Host "        [!] Could not create scheduled task (need admin)" -ForegroundColor Yellow
    Write-Host "        You can start manually: $ConfigDir\start-winremote.bat" -ForegroundColor Yellow
}

# --- Firewall ---
Write-Host "  [6/6] Configuring firewall..." -ForegroundColor White
try {
    # Remove old rule if exists
    Remove-NetFirewallRule -DisplayName "WinRemote MCP" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "WinRemote MCP" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -Profile Private,Domain | Out-Null
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

# --- Start server now ---
Write-Host ""
Write-Host "  Starting server..." -ForegroundColor Cyan
Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$ConfigDir\start-winremote.bat`"" -WindowStyle Normal
Start-Sleep -Seconds 2

# Test if it's running
$running = $false
try {
    $response = Invoke-WebRequest -Uri "http://localhost:$Port" -Method GET -TimeoutSec 3 -ErrorAction SilentlyContinue
    $running = $true
} catch {
    # Even a 404/405 means the server is up
    if ($_.Exception.Response) { $running = $true }
}

if ($running) {
    Write-Host "  Server is running!" -ForegroundColor Green
} else {
    Write-Host "  [!] Server may still be starting... check the window" -ForegroundColor Yellow
}

# --- Summary ---
Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "  SETUP COMPLETE" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Computer:  $hostName ($localIP)" -ForegroundColor White
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
Write-Host "  pip uninstall winremote-mcp" -ForegroundColor Gray
Write-Host "  Unregister-ScheduledTask -TaskName WinRemoteMCP -Confirm:`$false" -ForegroundColor Gray
Write-Host "  Remove-Item -Recurse $ConfigDir" -ForegroundColor Gray
Write-Host ""
