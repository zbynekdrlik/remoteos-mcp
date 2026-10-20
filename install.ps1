# RemoteOS MCP - One-line installer
# Usage: irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/install.ps1 | iex

$ErrorActionPreference = "Stop"
$Port = 8092
# The explicit key is read once and dropped from the environment BEFORE the first child process
# (query user, python --version, winget, curl.exe): a child inherits $env: (#12 review round 9).
$explicitKey = $env:REMOTEOS_MCP_AUTH_KEY
Remove-Item Env:REMOTEOS_MCP_AUTH_KEY -ErrorAction SilentlyContinue

# --- The auth key (#12): never on a command line, never printed ---------------------------------
# The key precedence is the Linux/macOS one: an explicit REMOTEOS_MCP_AUTH_KEY, else the key the
# running start script hands the server, else config.json, else remoteos.toml's [server] auth_key
# (the server's own config: a config.json/toml split can never flip the key), else the old
# winremote config, else a new key. start-remoteos.bat sets REMOTEOS_AUTH_KEY, which remoteos
# reads, instead of passing --auth-key on the server's command line.

function Test-RemoteOSKey([string]$Key) {
    # 1-128 ASCII letters/digits; \z, not $: $ also matches before a trailing newline
    return [bool]($Key -and $Key.Length -le 128 -and $Key -cmatch '^[A-Za-z0-9]+\z')
}

function New-RemoteOSKey {
    # 32 characters drawn uniformly from [A-Za-z0-9] with a CSPRNG (bytes >= 248 are dropped:
    # 248 = 4 * 62, so no character is more likely than another)
    $alphabet = [char[]]((48..57) + (65..90) + (97..122))
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $byte = New-Object byte[] 1
    $key = New-Object System.Text.StringBuilder
    try {
        while ($key.Length -lt 32) {
            $rng.GetBytes($byte)
            if ($byte[0] -lt 248) { [void]$key.Append($alphabet[$byte[0] % 62]) }
        }
    } finally {
        $rng.Dispose()
    }
    return $key.ToString()
}

function Get-RemoteOSKeyFromStartScript([string]$Path) {
    # start-remoteos.bat: `set "REMOTEOS_AUTH_KEY=<key>"` (this installer), or the legacy
    # `... -m remoteos ... --auth-key "<key>"`
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*set\s+"REMOTEOS_AUTH_KEY=([^"]+)"') { return $Matches[1] }  # not the clearing line
        if ($line -match '\s--auth-key\s+"?([^"\s]+)"?') { return $Matches[1] }
    }
    return $null
}

function Get-RemoteOSKeyFromJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return [string](Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json).auth_key } catch { return $null }
}

function Get-RemoteOSKeyFromToml([string]$Path) {
    # remoteos.toml's [server] auth_key (the table remoteos's config.py reads it from)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $table = ''
    foreach ($line in Get-Content -LiteralPath $Path) {
        $t = $line.Trim()
        if ($t -match '^\[\s*([^\]]+?)\s*\]') { $table = $Matches[1]; continue }
        if ($table -eq 'server' -and $t -match '^auth_key\s*=\s*["'']([^"'']*)["'']') { return $Matches[1] }
    }
    return $null
}

function Get-RemoteOSExistingKeys([string]$ConfigDir, [string]$TomlPath, [string]$OldConfigDir) {
    # Every place this box may already keep a key, in precedence order: {source: key or $null}
    $found = [ordered]@{}
    $found["the running start script ($ConfigDir\start-remoteos.bat)"] = Get-RemoteOSKeyFromStartScript (Join-Path $ConfigDir 'start-remoteos.bat')
    $found["$ConfigDir\config.json"] = Get-RemoteOSKeyFromJson (Join-Path $ConfigDir 'config.json')
    $found[$TomlPath] = Get-RemoteOSKeyFromToml $TomlPath
    $found["the old winremote config ($OldConfigDir\config.json)"] = Get-RemoteOSKeyFromJson (Join-Path $OldConfigDir 'config.json')
    return $found
}

function Resolve-RemoteOSAuthKey([string]$Explicit, $Found) {
    # -> @{Key; Source}: Source names where the key came from ($null = new), never the value
    if ($Explicit) {
        if (-not (Test-RemoteOSKey $Explicit)) {
            throw "REMOTEOS_MCP_AUTH_KEY must be 1-128 letters/digits [A-Za-z0-9]"
        }
        return [pscustomobject]@{ Key = $Explicit; Source = 'REMOTEOS_MCP_AUTH_KEY' }
    }
    # The first key the PC has decides; one this installer cannot carry is refused, never
    # replaced by a new key (that would silently cut the clients off).
    foreach ($source in $Found.Keys) {
        $key = $Found[$source]
        if (-not $key) { continue }
        if (-not (Test-RemoteOSKey $key)) {
            throw "the key in $source is not 1-128 letters/digits, so it cannot be carried over; pass the key the clients use as REMOTEOS_MCP_AUTH_KEY"
        }
        return [pscustomobject]@{ Key = $key; Source = $source }
    }
    return [pscustomobject]@{ Key = (New-RemoteOSKey); Source = $null }
}

function New-RemoteOSStartScript([string]$PythonPath, [int]$Port, [string]$AuthKey) {
    # The start script: an auto-restart loop with a crash guard. The key is in the server's
    # environment (`set "REMOTEOS_AUTH_KEY=..."`), never on its command line, and set only around
    # the server line: every other program cmd.exe starts (netstat, findstr, timeout) inherits it.
    if (-not (Test-RemoteOSKey $AuthKey)) { throw "refusing to write an invalid auth key into the start script" }
    return @"
@echo off
title RemoteOS MCP (port $Port)
REM remoteos reads its auth key from REMOTEOS_AUTH_KEY, set only for the server itself.
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
set "REMOTEOS_AUTH_KEY=$AuthKey"
"$PythonPath" -m remoteos --transport streamable-http --enable-all --host 0.0.0.0 --port $Port
set EXIT_CODE=%errorlevel%
set "REMOTEOS_AUTH_KEY="
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
"@
}

function Set-RemoteOSOwnerOnlyAcl([string]$Path, [string]$User) {
    # A protected ACL (nothing inherited) for a file holding the key: the desktop user the
    # server runs as, SYSTEM and Administrators, nobody else.
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $identities = @(
        (New-Object System.Security.Principal.NTAccount($User)),
        (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')),
        (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544'))
    )
    foreach ($identity in $identities) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow')))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

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

# --- The auth key: resolved before anything changes (the old winremote config is deleted
# below, and an invalid explicit key must be refused first) ---
$tomlPath = "$desktopProfile\.config\remoteos\remoteos.toml"
$oldConfigDir = "$desktopProfile\.winremote-mcp"
$foundKeys = Get-RemoteOSExistingKeys -ConfigDir $ConfigDir -TomlPath $tomlPath -OldConfigDir $oldConfigDir
try {
    $resolvedKey = Resolve-RemoteOSAuthKey -Explicit $explicitKey -Found $foundKeys
} catch {
    Write-Host "  [X] $_ - nothing was changed on this PC" -ForegroundColor Red
    exit 1
}
$AuthKey = $resolvedKey.Key

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
if (Test-Path $oldConfigDir) {
    Remove-Item -Recurse -Force $oldConfigDir -ErrorAction SilentlyContinue
    Write-Host "        Cleaned up old .winremote-mcp config" -ForegroundColor Gray
}
Unregister-ScheduledTask -TaskName "WinRemoteMCP" -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName "WinRemote MCP" -ErrorAction SilentlyContinue
# Stop running server BEFORE pip install — pip --force-reinstall on Windows
# cannot delete files held open by the running python process, which corrupts
# packages (e.g. fastmcp's __init__.py gets deleted, leaving a namespace package).
Write-Host "        Stopping running server..." -ForegroundColor Gray
schtasks /End /TN "RemoteOSMCP" 2>&1 | Out-Null
# Kill by window title (catches python/cmd with the batch title)
Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -match "RemoteOS|WinRemote"
} | Stop-Process -Force -ErrorAction SilentlyContinue
# Kill ALL processes related to the running server: the python server process,
# the CMD batch restart loop (start-remoteos.bat), and the wscript VBS launcher.
# The CMD batch has a restart loop that will re-launch python within 10s if
# only python is killed.  Exclude the current process AND its parent — the
# installer's own source code (and the SSH cmd.exe wrapper carrying it) contain
# these patterns as string literals and would match themselves.
$myPid = $PID
$parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId = $myPid" -ErrorAction SilentlyContinue).ParentProcessId
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $myPid -and $_.ProcessId -ne $parentPid -and
    ($_.CommandLine -match "-m remoteos" -or $_.CommandLine -match "start-remoteos" -or
     $_.CommandLine -match "-m winremote" -or $_.CommandLine -match "start-winremote")
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
# Kill server by port (fallback — catches anything on our port)
$portPid = (netstat -ano | Select-String "0.0.0.0:$Port.*LISTENING" | ForEach-Object {
    ($_.ToString().Trim() -split "\s+")[-1]
}) | Select-Object -First 1
if ($portPid) {
    Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
$env:PIP_CONSTRAINT = "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/constraints.txt"
# Pre-uninstall all three packages so no stale RECORD can delete files the new
# install writes.  Root cause (#12 wheel evidence): monolithic fastmcp 2.x
# RECORD claims fastmcp/__init__.py — same file fastmcp-slim 4.x writes.
# pip --force-reinstall can install slim (writes it) then uninstall old 2.x
# (deletes it).  Explicit pre-uninstall + clean install avoids this entirely.
& $python -m pip uninstall -y remoteos-mcp fastmcp fastmcp-slim 2>&1 | Out-Null
# Keep the full pip output: on failure it is printed whole (#12 — a discarded
# or truncated pip error hides the real cause).
$pipInstallOut = & $python -m pip install --no-cache-dir "https://github.com/zbynekdrlik/remoteos-mcp/archive/main.zip" 2>&1 | Out-String
$pipInstallExit = $LASTEXITCODE
$pipShow = & $python -m pip show remoteos-mcp 2>&1 | Out-String
$ErrorActionPreference = $prevEAP
# Success = pip exited 0 AND the package is there (an old dist left behind by a
# failed install must not read as "Installed v<old>").
if ($pipInstallExit -eq 0 -and $pipShow -match "Version: (.+)") {
    Write-Host "        Installed v$($Matches[1].Trim())" -ForegroundColor Green
} else {
    Write-Host "        [X] pip install failed. Full pip output:" -ForegroundColor Red
    Write-Host $pipInstallOut
    Write-Host "        Try manually: $python -m pip install https://github.com/zbynekdrlik/remoteos-mcp/archive/main.zip" -ForegroundColor Yellow
    return
}
# Sanity check: verify remoteos and fastmcp can be imported (fail hard, never repair)
$importCheck = & $python -c "import remoteos, fastmcp; from fastmcp import FastMCP" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "        [X] Import check failed — remoteos or fastmcp broken after install" -ForegroundColor Red
    Write-Host "        $importCheck" -ForegroundColor Red
    exit 1
}

# --- The auth key (resolved before the install, above) ---
Write-Host "  [3/6] Configuring auth key..." -ForegroundColor White
if ($resolvedKey.Source) {
    Write-Host "        Auth key: kept, from $($resolvedKey.Source)" -ForegroundColor Green
} else {
    Write-Host "        Auth key: a NEW auth key was generated - the client's .mcp.json entry for this PC needs it" -ForegroundColor Yellow
}
$tomlKey = $foundKeys[$tomlPath]
if ((Test-RemoteOSKey $tomlKey) -and $tomlKey -cne $AuthKey) {
    Write-Host "        [!] $tomlPath has another auth_key; the installed key wins (REMOTEOS_AUTH_KEY)" -ForegroundColor Yellow
}

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

# Start batch script (auto-restart loop and crash guard; the key in its environment)
New-RemoteOSStartScript -PythonPath $pythonPath -Port $Port -AuthKey $AuthKey | Set-Content "$ConfigDir\start-remoteos.bat"

# Both files hold the key: the desktop user, SYSTEM and Administrators only
foreach ($secretFile in @("$ConfigDir\config.json", "$ConfigDir\start-remoteos.bat")) {
    try {
        Set-RemoteOSOwnerOnlyAcl -Path $secretFile -User $desktopUser
    } catch {
        Write-Host "        [!] Could not restrict the ACL of ${secretFile}: $_ (it keeps the user profile's ACL)" -ForegroundColor Yellow
    }
}

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
    # Set the network to Private (the rule below allows every profile, but a Public network may
    # carry other blocking rules)
    Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object {
        $_.NetworkCategory -eq "Public"
    } | Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
    # Remove old rule if exists
    Remove-NetFirewallRule -DisplayName "RemoteOS MCP" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "RemoteOS MCP" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -Profile Any | Out-Null
    Write-Host "        Firewall rule added (port $Port, all profiles - reliable across network drift)" -ForegroundColor Green
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

# --- Post-install: verify service reports the correct version ---
Write-Host "  Verifying installed version matches running service..." -ForegroundColor Cyan
$PKG_VER = & $python -c "import importlib.metadata as m; print(m.version('remoteos-mcp'))" 2>&1 | Out-String
$PKG_VER = $PKG_VER.Trim()
$healthOk = $false
for ($attempt = 1; $attempt -le 6; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5 -ErrorAction Stop
        $healthVer = $health.version
        if ($healthVer -eq $PKG_VER) {
            $healthOk = $true
            Write-Host "        Health check OK: v${healthVer}" -ForegroundColor Green
            break
        } elseif ($healthVer) {
            Write-Host "  [X] Installed ${PKG_VER} but service reports ${healthVer}" -ForegroundColor Red
            exit 1
        }
    } catch {}
    Start-Sleep -Seconds 5
}
if (-not $healthOk) {
    Write-Host "  [X] Health endpoint (http://127.0.0.1:${Port}/health) did not respond within 30s" -ForegroundColor Red
    exit 1
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
Write-Host "  Auth key:  in $ConfigDir\config.json and start-remoteos.bat (owner-only) - never printed." -ForegroundColor White
Write-Host "             Read it on this PC: Get-Content $ConfigDir\config.json" -ForegroundColor White
if (-not $resolvedKey.Source) {
    Write-Host "             It is a NEW key: update the client's .mcp.json entry for this PC." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  On your Linux machine, run:" -ForegroundColor White
Write-Host ""
Write-Host "  claude mcp add --transport http win-$hostName ``" -ForegroundColor Green
Write-Host "    http://${localIP}:${Port}/mcp ``" -ForegroundColor Green
Write-Host "    --header ""Authorization: Bearer <the auth key>""" -ForegroundColor Green
Write-Host ""
Write-Host "  Then restart Claude Code." -ForegroundColor Gray
Write-Host ""
Write-Host "  To uninstall later:" -ForegroundColor Gray
Write-Host "  irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall.ps1 | iex" -ForegroundColor Gray
Write-Host ""
