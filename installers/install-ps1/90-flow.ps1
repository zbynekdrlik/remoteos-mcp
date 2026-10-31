Write-Host ""
Write-Host "  RemoteOS MCP Installer" -ForegroundColor Cyan
Write-Host "  Remote Windows control for Claude Code" -ForegroundColor Gray
Write-Host ""

# --- The elevation gate (#57): before anything on the box changes ---
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$interactive = Test-RemoteOSInteractiveSession -SshConnection $env:SSH_CONNECTION -SshClient $env:SSH_CLIENT `
    -SessionId (Get-Process -Id $PID).SessionId -InputRedirected ([Console]::IsInputRedirected)
# A piped run in a person's console pauses before it exits ($PSCommandPath is empty for irm | iex)
$pauseOnExit = $interactive -and -not $PSCommandPath -and -not $relaunched
$elevation = Get-RemoteOSElevationAction -IsAdmin $isAdmin -Interactive $interactive -ExplicitKey ([bool]$explicitKey -or [bool]$enrollUrl) `
    -Relaunched $relaunched
if ($elevation -eq 'fail') {
    Write-Host "  [X] The RemoteOS MCP installer needs an elevated (Administrator) PowerShell -- nothing was changed on this box" -ForegroundColor Red
    if ($relaunched) {
        Write-Host "      The elevated relaunch is still not elevated (is UAC turned off for a standard user?):" -ForegroundColor Yellow
        Write-Host "      run the installer from an administrator account." -ForegroundColor Yellow
    } elseif ($enrollUrl) {
        Write-Host "      The enrollment URL does not cross the UAC elevation: open PowerShell as Administrator" -ForegroundColor Yellow
        Write-Host "      (right-click > Run as Administrator) and run the printed command there." -ForegroundColor Yellow
    } elseif ($interactive) {
        Write-Host "      REMOTEOS_MCP_AUTH_KEY cannot cross the UAC elevation: open PowerShell as Administrator" -ForegroundColor Yellow
        Write-Host "      (right-click > Run as Administrator), set REMOTEOS_MCP_AUTH_KEY there and run the installer again." -ForegroundColor Yellow
    } else {
        Write-Host "      This run is not at a desktop (SSH, session 0, or no console input), so it cannot answer" -ForegroundColor Yellow
        Write-Host "      a UAC prompt: run it as an account with administrator rights." -ForegroundColor Yellow
    }
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}

if (-not (Test-RemoteOSRing $Ring)) {
    Write-Host "  [X] REMOTEOS_MCP_RING must be stable or canary -- nothing was changed on this box" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}

if ($enrollUrl -and -not (Test-RemoteOSEnrollUrl $enrollUrl)) {
    Write-Host "  [X] REMOTEOS_ENROLL is not an enrollment URL (https://<host>/<token>/) -- nothing was changed on this box" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}
if ($enrollUrl -and -not (Test-RemoteOSSshPublicKey $enrollSshKey)) {
    Write-Host "  [X] REMOTEOS_ENROLL_SSH_KEY must be one ssh public key line -- nothing was changed on this box" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}

# --- The source commit (#12): resolved once, before anything changes ---
if (-not (Test-RemoteOSRefName $Ref)) {
    Write-Host "  [X] REMOTEOS_MCP_REF is not a plain branch, tag or commit name -- nothing was changed on this box" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}
# Windows PowerShell 5.1 on an older .NET may not offer TLS 1.2, which GitHub requires
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$sourceSha = $null
$resolveError = "no commit sha in the answer"
try {
    $sourceSha = [string](Invoke-RestMethod -Uri "https://api.github.com/repos/zbynekdrlik/remoteos-mcp/commits/$Ref" -TimeoutSec 30 -ErrorAction Stop).sha
} catch {
    $resolveError = "$_"
}
if ($sourceSha -cnotmatch '^[0-9a-f]{40}\z') {
    Write-Host "  [X] Could not resolve '$Ref' to a commit of zbynekdrlik/remoteos-mcp ($resolveError) -- nothing was changed on this box" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}
Write-Host "  Source: $Ref = commit $sourceSha" -ForegroundColor Gray
# This installer at that commit: the elevated relaunch of a piped run fetches it (#57)
$InstallerUrl = "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/$sourceSha/install.ps1"

if ($elevation -eq 'relaunch') {
    Write-Host "  Not elevated: the install continues in an elevated PowerShell window - confirm the UAC prompt." -ForegroundColor Yellow
    $elevatedExit = Invoke-RemoteOSElevatedRelaunch -ScriptPath $PSCommandPath -InstallerUrl $InstallerUrl -SourceRef $sourceSha -Ring $Ring
    if ($null -eq $elevatedExit) { Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit }
    Write-Host "  The elevated install finished with exit code $elevatedExit." -ForegroundColor Gray
    Exit-RemoteOSInstaller -Code $elevatedExit -Pause $pauseOnExit
}

# --- One run at a time (#65), from here to the end of this process ---
$installLock = Enter-RemoteOSInstallLock
if (-not $installLock) {
    Write-Host "  [X] Another run of this installer is going on -- nothing was changed on this box" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
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
Write-Host "  [1/7] Checking Python..." -ForegroundColor White
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if (Test-RemoteOSPythonVersion "$ver") {
            $python = $cmd
            Write-Host "        Found $ver" -ForegroundColor Green
            break
        }
    } catch {}
}

# Enrollment (#26): a fresh box gets Python for all users, from an installer that waits, or the run ends
if ($enrollUrl -and -not $python) {
    Write-Host "        Python 3.11+ not found. Installing Python 3.12 for all users..." -ForegroundColor Yellow
    try {
        $python = Install-RemoteOSMachinePython (Join-Path $env:ProgramFiles 'Python312')
    } catch {
        Write-Host "        [X] Could not install Python: $_ -- nothing else was changed on this box" -ForegroundColor Red
        Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
    }
    Write-Host "        Python installed: $python" -ForegroundColor Green
}

# Elevated as another account: the desktop user's task must reach this Python (checked before any change)
$otherAccount = $env:USERNAME -ine $desktopUser
$foundPythonPath = $null
if ($python) { $foundPythonPath = (Get-Command $python -ErrorAction SilentlyContinue).Source }
if (-not (Test-RemoteOSPythonReachable -PythonPath $foundPythonPath -RunProfile $env:USERPROFILE -OtherAccount $otherAccount)) {
    Write-Host "  [X] This run is elevated as $env:USERNAME, but the server runs as $desktopUser, who cannot run" -ForegroundColor Red
    Write-Host "      this account's Python ($foundPythonPath) -- nothing was changed on this box" -ForegroundColor Red
    Write-Host "      Install Python 3.11+ for all users (python.org: 'Install for all users'), or run the installer" -ForegroundColor Yellow
    Write-Host "      as $desktopUser with administrator rights." -ForegroundColor Yellow
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}

if (-not $python) {
    Write-Host "        Python 3.11+ not found. Installing..." -ForegroundColor Yellow

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
        Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
    }
}
# What runs as $python must be 3.11+ too: winget / python.org may leave an older Python first on PATH
try { $installedVersion = "$(& $python --version 2>&1)" } catch { $installedVersion = "$_" }
if (-not (Test-RemoteOSPythonVersion "$installedVersion")) {
    Write-Host "        [X] Python 3.11+ is needed, but '$python' is '$installedVersion' -- put Python 3.11+ first on PATH and run the installer again" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}

# --- The auth key: resolved before anything changes (an invalid explicit key is refused first) ---
$tomlPath = "$desktopProfile\.config\remoteos\remoteos.toml"
$oldConfigDir = "$desktopProfile\.winremote-mcp"
$foundKeys = Get-RemoteOSExistingKeys -ConfigDir $ConfigDir -TomlPath $tomlPath -OldConfigDir $oldConfigDir
try {
    $resolvedKey = Resolve-RemoteOSAuthKey -Explicit $explicitKey -Found $foundKeys
} catch {
    Write-Host "  [X] $_ - nothing was changed on this PC" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}
$AuthKey = $resolvedKey.Key

# --- The download (#64): every wheel the install needs, while the old server still runs ---
Write-Host "  [2/7] Downloading remoteos-mcp and its dependencies (the running server is not touched)..." -ForegroundColor White
$dataDir = Get-RemoteOSDataDir
$wheelhouseRoot = Join-Path $dataDir 'wheelhouse'
$legacyWheelhouseRoot = Join-Path $ConfigDir 'wheelhouse'
# The folder first: any account may create a folder in ProgramData, and its owner could swap what is inside
$protectError = Protect-RemoteOSWheelhouseRoot -Root $dataDir -User $desktopUser
if (-not $protectError) { $protectError = Protect-RemoteOSWheelhouseRoot -Root $wheelhouseRoot -User $desktopUser }
if ($protectError) {
    Write-Host "  [X] $protectError -- the running server and package were not touched" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}
$preflight = New-RemoteOSWheelhouse -Python $python -Root $wheelhouseRoot -Sha $sourceSha `
    -SourceUrl "https://github.com/zbynekdrlik/remoteos-mcp/archive/$sourceSha.zip" `
    -ConstraintsUrl "https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/$sourceSha/constraints.txt"
if (-not $preflight.Ok) {
    Write-Host "  [X] could not download remoteos-mcp $sourceSha and its dependencies ($($preflight.Error)) -- nothing was changed on this box" -ForegroundColor Red
    if ($preflight.Output) { Write-Host $preflight.Output }
    Exit-RemoteOSInstaller -Code 1 -Pause $pauseOnExit
}
if ($preflight.Reused) {
    Write-Host "        v$($preflight.Version): wheelhouse kept from an earlier run ($($preflight.Path))" -ForegroundColor Green
} else {
    Write-Host "        v$($preflight.Version): downloaded into $($preflight.Path)" -ForegroundColor Green
}
# What a failure after the stop goes back to, read before anything changes
$rollbackPoint = Get-RemoteOSRollbackPoint -ConfigDir $ConfigDir -Root $wheelhouseRoot -Python $python
if (-not $rollbackPoint) {
    Write-Host "        Fetching the running version again for the rollback..." -ForegroundColor Gray
    $rebuilt = New-RemoteOSRollbackWheelhouse -ConfigDir $ConfigDir -Root $wheelhouseRoot -Python $python
    if ($rebuilt -and $rebuilt.Ok) {
        $rollbackPoint = Get-RemoteOSRollbackPoint -ConfigDir $ConfigDir -Root $wheelhouseRoot -Python $python
    } elseif ($rebuilt) {
        Write-Host "        [!] It could not be fetched ($($rebuilt.Error))" -ForegroundColor Yellow
    }
}
if ($rollbackPoint) {
    Write-Host "        Rollback: v$($rollbackPoint.Version) (commit $($rollbackPoint.Ref)), from its kept wheelhouse" -ForegroundColor Gray
} else {
    Write-Host "        [!] No previous wheelhouse (the first install with wheelhouses): a failure after the stop cannot roll back" -ForegroundColor Yellow
}
$startDirect = $interactive -and ($env:USERNAME -ieq $desktopUser)
# Every failure after the stop goes through Exit-RemoteOSAfterFailure @rollback
$rollback = @{
    Point       = $rollbackPoint
    Python      = $python
    Pause       = $pauseOnExit
    StopServer  = { Stop-RemoteOSServer -Port $Port }
    StartServer = { Start-RemoteOSServer -ConfigDir $ConfigDir -StartDirect $startDirect }
    ProveHealth = { param($Version) Test-RemoteOSServerHealth -Port $Port -Version $Version -StartDirect $startDirect }
    Disarm      = { Stop-RemoteOSRollbackTrap -ConfigDir $ConfigDir }
}

# --- Install remoteos-mcp, offline from the wheelhouse ---
Write-Host "  [3/7] Installing remoteos-mcp v$($preflight.Version) (offline)..." -ForegroundColor White
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
    try { Remove-RemoteOSTree $oldConfigDir } catch { Write-Host "        [!] Could not remove ${oldConfigDir}: $_" -ForegroundColor Yellow }
    Write-Host "        Cleaned up old .winremote-mcp config" -ForegroundColor Gray
}
Unregister-ScheduledTask -TaskName "WinRemoteMCP" -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName "WinRemote MCP" -ErrorAction SilentlyContinue
$ErrorActionPreference = $prevEAP
# A run killed from here on (an SSH drop, a crash) is rolled back from outside (#65)
if ($rollbackPoint) {
    $trapPython = (Get-Command $python -ErrorAction SilentlyContinue).Source
    if (-not $trapPython) { $trapPython = $python }
    $trapError = Start-RemoteOSRollbackTrap -Point $rollbackPoint -Python $trapPython -ConfigDir $ConfigDir -DataDir $dataDir -Port $Port -User $desktopUser
    if ($trapError) {
        Write-Host "        [!] No rollback trap ($trapError): a run killed from here on leaves no server" -ForegroundColor Yellow
    }
}
Write-Host "        Stopping running server..." -ForegroundColor Gray
Stop-RemoteOSServer -Port $Port
$install = Install-RemoteOSWheelhouse -Python $python -Wheelhouse $preflight.Path -Version $preflight.Version
if (-not $install.Ok) {
    Exit-RemoteOSAfterFailure -Reason $install.Error -Output $install.Output @rollback
}
Write-Host "        Installed v$($preflight.Version)" -ForegroundColor Green

# --- The auth key (resolved before the install, above) ---
Write-Host "  [4/7] Configuring auth key..." -ForegroundColor White
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
Write-Host "  [5/7] Creating config..." -ForegroundColor White
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
Write-Host "  [6/7] Setting up auto-start..." -ForegroundColor White
try {
    $taskAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$ConfigDir\start-remoteos.vbs`""
    # Trigger at logon + every 5 minutes (task won't start a second instance if already running)
    $triggerLogon = New-ScheduledTaskTrigger -AtLogon
    $triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId (Get-RemoteOSAccountName $desktopUser) -RunLevel Highest -LogonType Interactive
    $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "RemoteOSMCP" -Action $taskAction -Trigger @($triggerLogon, $triggerRepeat) -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
    Write-Host "        Task 'RemoteOSMCP' registered for $desktopUser (at logon + every 5 min)" -ForegroundColor Green
} catch {
    # The run is elevated here (the gate above), so this is a real failure, not a missing right
    Exit-RemoteOSAfterFailure -Reason "Could not register the scheduled task 'RemoteOSMCP': $_" -Output '' @rollback
}
try {
    if (Register-RemoteOSUpgradeTask -ConfigDir $ConfigDir -User $desktopUser) {
        Write-Host "        Task 'RemoteOSMCP-Upgrade' registered for $desktopUser (the SelfUpgrade tool starts it)" -ForegroundColor Green
    }
} catch {
    Exit-RemoteOSAfterFailure -Reason "Could not register the scheduled task 'RemoteOSMCP-Upgrade': $_" -Output '' @rollback
}
try {
    if (Register-RemoteOSUpdaterTask -ConfigDir $ConfigDir -PythonPath $pythonPath -User $desktopUser) {
        Write-Host "        Task 'RemoteOSMCP-Updater' registered for $desktopUser (daily)" -ForegroundColor Green
    }
    Set-RemoteOSRing -ConfigDir $ConfigDir -Ring $Ring
} catch {
    Exit-RemoteOSAfterFailure -Reason "Could not set up the updater: $_" -Output '' @rollback
}

# --- Firewall + Network profile ---
Write-Host "  [7/7] Configuring firewall..." -ForegroundColor White
try {
    # Set the network to Private (a Public network may carry other blocking rules)
    Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object {
        $_.NetworkCategory -eq "Public"
    } | Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
    # The new rule first, then the old ones: a failure (and its rollback) keeps the LAN reachable
    $oldRules = @(Get-NetFirewallRule -DisplayName "RemoteOS MCP" -ErrorAction SilentlyContinue)
    New-NetFirewallRule -DisplayName "RemoteOS MCP" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -Profile Any | Out-Null
    if ($oldRules.Count -gt 0) { $oldRules | Remove-NetFirewallRule -ErrorAction SilentlyContinue }
    Write-Host "        Firewall rule added (port $Port, all profiles - reliable across network drift)" -ForegroundColor Green
} catch {
    # Elevated here too: a failure is real
    Exit-RemoteOSAfterFailure -Reason "Could not add the firewall rule for TCP port ${Port}: $_" -Output '' @rollback
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
    try { Remove-RemoteOSTree $currentUserConfig } catch { Write-Host "  [!] Could not remove ${currentUserConfig}: $_" -ForegroundColor Yellow }
    Write-Host "  Cleaned up old config from $env:USERNAME" -ForegroundColor Gray
}

# --- Start server now (hidden): directly or through the task (Start-RemoteOSServer) ---
if ($startDirect) {
    Write-Host "  Starting server..." -ForegroundColor Cyan
} else {
    Write-Host "  Starting server through the scheduled task, as $desktopUser in the desktop session..." -ForegroundColor Cyan
}
$startError = Start-RemoteOSServer -ConfigDir $ConfigDir -StartDirect $startDirect
if ($startError) {
    Exit-RemoteOSAfterFailure -Reason $startError -Output '' @rollback
}

# --- Post-install: verify service reports the correct version ---
Write-Host "  Verifying installed version matches running service..." -ForegroundColor Cyan
$health = Test-RemoteOSServerHealth -Port $Port -Version $preflight.Version -StartDirect $startDirect
if (-not $health.Ok) {
    if (-not $startDirect) {
        Write-Host "      The task runs the server in ${desktopUser}'s desktop session: that user must be logged in," -ForegroundColor Yellow
        Write-Host "      and must be able to run $pythonPath (a per-user Python of another account cannot be)." -ForegroundColor Yellow
    }
    Exit-RemoteOSAfterFailure -Reason $health.Message -Output '' @rollback
}
Write-Host "        Health check OK: $($health.Message)" -ForegroundColor Green
Stop-RemoteOSRollbackTrap -ConfigDir $ConfigDir

# The commit this box runs, once proven (#12; the updater and the next rollback read it). No secret.
$sourceSha | Set-Content "$ConfigDir\installed-ref.txt"
# Only now: the current and the previous wheelhouse stay, older ones go (#64)
$pruned = @(Remove-RemoteOSStaleWheelhouses -Root $wheelhouseRoot -Keep @($sourceSha, $rollbackPoint.Ref))
if ($pruned.Count -gt 0) { Write-Host "        Removed old wheelhouses: $($pruned -join ', ')" -ForegroundColor Gray }
if (Test-Path -LiteralPath $legacyWheelhouseRoot) {
    try { Remove-RemoteOSTree $legacyWheelhouseRoot } catch { Write-Host "        [!] Could not remove ${legacyWheelhouseRoot}: $_" -ForegroundColor Yellow }
}

# --- Enrollment (#26): after the proof, so a failure leaves the proven install up ---
# The registration first: SSH is the break-glass path, and a box dev1 reaches over MCP can have it repaired
if ($enrollUrl) {
    Write-Host "  Enrolling this box..." -ForegroundColor Cyan
    try {
        $enrollAddresses = @(Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            ForEach-Object { $_.IPAddress })
        $enrollBody = Get-RemoteOSEnrollmentBody -HostName $hostName -Addresses $enrollAddresses -ServerPort $Port `
            -Version $preflight.Version -Account $desktopUser -AuthKey $AuthKey
    } catch {
        Exit-RemoteOSEnrollmentFailure -Reason "Could not build the registration: $_" -Pause $pauseOnExit
    }
    $enrollError = Send-RemoteOSEnrollment -Url $enrollUrl -Body $enrollBody
    Remove-Variable enrollBody
    if ($enrollError) {
        Exit-RemoteOSEnrollmentFailure -Reason "The registration got no answer from dev1 ($enrollError): check on dev1 whether the box is enrolled" -Pause $pauseOnExit
    }
    Write-Host "        Enrolled: dev1 received this box's registration" -ForegroundColor Green
    try {
        $sshKeyPath = Enable-RemoteOSSsh -Key $enrollSshKey -User $desktopUser -UserProfile $desktopProfile
    } catch {
        Exit-RemoteOSEnrollmentFailure -Reason "The box is enrolled, but SSH could not be set up: $_" -Pause $pauseOnExit
    }
    Write-Host "        OpenSSH server: enabled, key in $sshKeyPath" -ForegroundColor Green
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
Write-Host "  Commit:    $sourceSha ($Ref; in $ConfigDir\installed-ref.txt)" -ForegroundColor White
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
# irm | iex ends here with the console still open: the lock must not outlive the run
$installLock.ReleaseMutex()
$installLock.Dispose()
