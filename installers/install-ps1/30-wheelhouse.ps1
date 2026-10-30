# --- Download first, install offline, roll back (#64) ------------------------------------------
# Every wheel goes into <config>\wheelhouse\<sha> while the old server runs, the install is offline, and a
# failure after the stop reinstalls the previous commit's wheelhouse (.claude/rules/installers.md).

function Test-RemoteOSPythonVersion([string]$VersionText) {
    # "Python 3.<minor>" with minor >= 11: Pillow 13, the fixed release of its CVEs, needs 3.11 (#39)
    if ($VersionText -match 'Python 3\.(\d+)') { return [int]$Matches[1] -ge 11 }
    return $false
}

function Invoke-RemoteOSNative([string]$Python, [string[]]$Arguments) {
    # -> @{Exit; Output}, never discarded (#12); Continue: 5.1 turns native stderr under 2>&1 into a throw
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Python @Arguments 2>&1 | ForEach-Object { "$_" } | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    return [pscustomobject]@{ Exit = $code; Output = $output }
}

function Invoke-RemoteOSPip([string]$Python, [string[]]$Arguments) {
    return Invoke-RemoteOSNative $Python (@('-m', 'pip', '--disable-pip-version-check') + $Arguments)
}

function Get-RemoteOSInstalledVersion([string]$Python) {
    # The installed remoteos-mcp version or $null, from a marked line (a stderr warning is not it)
    $r = Invoke-RemoteOSNative $Python @('-c', "import importlib.metadata as m; print('REMOTEOS_VER=' + m.version('remoteos-mcp'))")
    if ($r.Exit -ne 0) { return $null }
    foreach ($line in ($r.Output -split "`r?`n")) {
        if ($line -match '^REMOTEOS_VER=(\S+)\s*$') { return $Matches[1] }
    }
    return $null
}

function Test-RemoteOSWheelhouseInstallable([string]$Python, [string]$Wheelhouse, [string]$Version) {
    # Can THIS Python install the wheelhouse offline (wheels are per interpreter)? -> @{Ok; Output}
    $r = Invoke-RemoteOSPip $Python @('install', '--dry-run', '--ignore-installed', '--no-index', '--find-links', $Wheelhouse,
        '-c', (Join-Path $Wheelhouse 'constraints.txt'), "remoteos-mcp==$Version")
    return [pscustomobject]@{ Ok = ($r.Exit -eq 0); Output = $r.Output }
}

function Test-RemoteOSWheelhouseRootTrusted([string]$Root) {
    # Owned by Administrators or SYSTEM, a protected ACL, and no write right for anyone else
    $sid = [System.Security.Principal.SecurityIdentifier]
    $admins = @('S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Root
    if ($admins -notcontains $acl.GetOwner($sid).Value -or -not $acl.AreAccessRulesProtected) { return $false }
    $writeMask = [int64](0x116 + 0x10000 + 0x40 + 0x40000 + 0x80000) -bor 0x10000000 -bor 0x40000000
    foreach ($rule in $acl.GetAccessRules($true, $true, $sid)) {
        if ($rule.AccessControlType -ne 'Allow' -or $admins -contains $rule.IdentityReference.Value) { continue }
        if (([int64]$rule.FileSystemRights -band $writeMask) -ne 0) { return $false }
    }
    return $true
}

function Test-RemoteOSLink([string]$Path) {
    # Fail closed: a path that exists but cannot be read counts as a link
    try { $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch [System.Management.Automation.ItemNotFoundException] { return $false }
    catch { return $true }
    return [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

function Remove-RemoteOSTree([string]$Path) {
    # Never through a planted link (5.1's Remove-Item -Recurse enters a junction): a link goes alone, a
    # directory entry by entry (.NET Framework's recursive Delete throws on a junction inside)
    if (Test-RemoteOSLink $Path) { [System.IO.Directory]::Delete($Path); return }
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($entry in ([System.IO.DirectoryInfo]$Path).GetFileSystemInfos()) {
        if ($entry -is [System.IO.DirectoryInfo]) { Remove-RemoteOSTree $entry.FullName }
        else { $entry.Attributes = 'Normal'; $entry.Delete() }
    }
    [System.IO.Directory]::Delete($Path)
}

function Protect-RemoteOSWheelhouseRoot([string]$Root, [string]$User) {
    # The wheelhouses feed an ELEVATED pip: owned by Administrators, SYSTEM + Administrators full, the user
    # read. A link, or a root made by someone else, is removed first. -> $null or an error
    try {
        $parent = Split-Path -Parent $Root
        if (Test-RemoteOSLink $parent) { return "$parent is a link: refusing to write through it" }
        if ((Test-RemoteOSLink $Root) -or ((Test-Path -LiteralPath $Root) -and -not (Test-RemoteOSWheelhouseRootTrusted $Root))) {
            Write-Host "        [!] $Root was a link or writable for another account: replaced (old wheelhouses removed)" -ForegroundColor Yellow
            Remove-RemoteOSTree $Root
        }
        if (-not (Test-Path -LiteralPath $Root)) { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        $none = [System.Security.AccessControl.PropagationFlags]::None
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')))
        $acl.SetAccessRuleProtection($true, $false)
        $grants = @(
            @((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')), 'FullControl'),
            @((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')), 'FullControl'),
            @((New-Object System.Security.Principal.NTAccount($User)), 'ReadAndExecute')
        )
        foreach ($grant in $grants) {
            $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($grant[0], $grant[1], $inherit, $none, 'Allow')))
        }
        Set-Acl -LiteralPath $Root -AclObject $acl
        if (-not (Test-RemoteOSWheelhouseRootTrusted $Root)) { return "$Root is still writable for another account after its ACL was set" }
        return $null
    } catch {
        return "could not protect ${Root} (remove it by hand if this repeats): $_"
    }
}

function Get-RemoteOSWheelhouseVersion([string]$Path) {
    # A complete wheelhouse holds the commit's constraints.txt and ONE remoteos_mcp wheel -> its version
    if (-not (Test-Path -LiteralPath (Join-Path $Path 'constraints.txt'))) { return $null }
    $wheels = @(Get-ChildItem -LiteralPath $Path -Filter 'remoteos_mcp-*.whl' -File -ErrorAction SilentlyContinue)
    if ($wheels.Count -eq 1 -and $wheels[0].Name -match '^remoteos_mcp-([^-]+)-') { return $Matches[1] }
    return $null
}

function New-RemoteOSWheelhouse([string]$Python, [string]$Root, [string]$Sha, [string]$SourceUrl, [string]$ConstraintsUrl) {
    # The preflight: constraints.txt, pip download, pip wheel of every source archive (offline pip cannot
    # build them), in <sha>.partial renamed when complete. A kept one this Python installs is reused, one
    # it cannot is built again. -> @{Ok; Path; Version; Reused; Error; Output}
    $final = Join-Path $Root $Sha
    $partial = Join-Path $Root "$Sha.partial"
    $result = [pscustomobject]@{ Ok = $false; Path = $final; Version = (Get-RemoteOSWheelhouseVersion $final)
                                 Reused = $false; Error = $null; Output = '' }
    if ($result.Version -and (Test-RemoteOSWheelhouseInstallable $Python $final $result.Version).Ok) {
        $result.Ok = $true; $result.Reused = $true; return $result
    }
    $result.Version = $null
    try {
        Remove-RemoteOSTree $partial
        New-Item -ItemType Directory -Path $partial -Force | Out-Null
        $constraints = Join-Path $partial 'constraints.txt'
        # Through Python's urllib (proxy and certificates as pip), 3 tries
        $fetch = "import sys, urllib.request; d = urllib.request.urlopen(sys.argv[1], timeout=60).read(); open(sys.argv[2], 'wb').write(d)"
        for ($try = 1; $try -le 3; $try++) {
            $get = Invoke-RemoteOSNative $Python @('-c', $fetch, $ConstraintsUrl, $constraints)
            if ($get.Exit -eq 0) { break }
            if ($try -lt 3) { Start-Sleep -Seconds (5 * $try) }
        }
        if ($get.Exit -ne 0) { $result.Error = "could not fetch $ConstraintsUrl"; $result.Output = $get.Output; return $result }
        $download = Invoke-RemoteOSPip $Python @('download', '--timeout', '60', '--retries', '10', '-c', $constraints, '-d', $partial, $SourceUrl)
        if ($download.Exit -ne 0) {
            $result.Error = "pip download failed (exit $($download.Exit))"; $result.Output = $download.Output; return $result
        }
        $sources = @(Get-ChildItem -LiteralPath $partial -File | Where-Object { $_.Name -notlike '*.whl' -and $_.Name -ne 'constraints.txt' })
        if ($sources.Count -gt 0) {
            $build = Invoke-RemoteOSPip $Python (@('wheel', '--no-deps', '--timeout', '60', '--retries', '10', '-c', $constraints, '-w', $partial) +
                @($sources | ForEach-Object { $_.FullName }))
            if ($build.Exit -ne 0) {
                $result.Error = "pip wheel failed (exit $($build.Exit))"; $result.Output = $build.Output; return $result
            }
            foreach ($source in $sources) { Remove-Item -LiteralPath $source.FullName -Force }
        }
        $version = Get-RemoteOSWheelhouseVersion $partial
        if (-not $version) { $result.Error = "the download holds no single remoteos-mcp wheel"; return $result }
        $check = Test-RemoteOSWheelhouseInstallable $Python $partial $version
        if (-not $check.Ok) { $result.Error = "the download does not install offline on this Python"; $result.Output = $check.Output; return $result }
        Remove-RemoteOSTree $final
        Move-Item -LiteralPath $partial -Destination $final
        $result.Version = $version
        $result.Ok = $true
        return $result
    } catch {
        $result.Error = "$_"
        return $result
    } finally {
        if (-not $result.Ok -and (Test-Path -LiteralPath $partial)) {
            try { Remove-RemoteOSTree $partial } catch { Write-Host "        [!] Could not remove ${partial}: $_" -ForegroundColor Yellow }
        }
    }
}

function Get-RemoteOSRollbackPoint([string]$ConfigDir, [string]$Python) {
    # installed-ref.txt's commit (proven) + its kept wheelhouse, installable here -> @{Ref; Path; Version} or $null
    $refFile = Join-Path $ConfigDir 'installed-ref.txt'
    if (-not (Test-Path -LiteralPath $refFile)) { return $null }
    $ref = ([string](Get-Content -LiteralPath $refFile -Raw)).Trim()
    if ($ref -cnotmatch '^[0-9a-f]{40}\z') { return $null }
    $path = Join-Path (Join-Path $ConfigDir 'wheelhouse') $ref
    $version = Get-RemoteOSWheelhouseVersion $path
    if (-not $version) { return $null }
    $check = Test-RemoteOSWheelhouseInstallable $Python $path $version
    if (-not $check.Ok) {
        Write-Host "        [!] The kept wheelhouse of v$version ($path) does not install on this Python:" -ForegroundColor Yellow
        Write-Host $check.Output
        return $null
    }
    return [pscustomobject]@{ Ref = $ref; Path = $path; Version = $version }
}

function Install-RemoteOSWheelhouse([string]$Python, [string]$Wheelhouse, [string]$Version) {
    # Offline; the pre-uninstall stays (#12), never --force-reinstall; version + import check -> @{Ok; Error; Output}
    $uninstall = Invoke-RemoteOSPip $Python @('uninstall', '-y', 'remoteos-mcp', 'fastmcp', 'fastmcp-slim')
    if ($uninstall.Exit -ne 0) {
        return [pscustomobject]@{ Ok = $false; Error = "the pre-uninstall failed (exit $($uninstall.Exit))"; Output = $uninstall.Output }
    }
    $install = Invoke-RemoteOSPip $Python @('install', '--no-index', '--find-links', $Wheelhouse, '-c', (Join-Path $Wheelhouse 'constraints.txt'), "remoteos-mcp==$Version")
    $installed = Get-RemoteOSInstalledVersion $Python
    if ($install.Exit -ne 0 -or $installed -ne $Version) {
        return [pscustomobject]@{ Ok = $false; Output = $install.Output
                                  Error = "pip install of v$Version from $Wheelhouse failed (exit $($install.Exit), installed: $installed)" }
    }
    $import = Invoke-RemoteOSNative $Python @('-c', 'import remoteos, fastmcp; from fastmcp import FastMCP')
    if ($import.Exit -ne 0) {
        return [pscustomobject]@{ Ok = $false; Error = "Import check failed - remoteos or fastmcp broken after install"; Output = $import.Output }
    }
    return [pscustomobject]@{ Ok = $true; Error = $null; Output = $install.Output }
}

function Remove-RemoteOSStaleWheelhouses([string]$Root, [string[]]$Keep) {
    # Keep current + previous, remove other <sha> names only; nothing if the root became a link -> removed names
    $removed = @()
    if ((Test-RemoteOSLink $Root) -or (Test-RemoteOSLink (Split-Path -Parent $Root))) { return $removed }
    foreach ($dir in @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue)) {
        if ($Keep -contains $dir.Name -or $dir.Name -cnotmatch '^[0-9a-f]{40}(\.partial)?\z') { continue }
        try {
            Remove-RemoteOSTree $dir.FullName
            $removed += $dir.Name
        } catch {
            Write-Host "        [!] Could not remove the old wheelhouse $($dir.FullName): $_" -ForegroundColor Yellow
        }
    }
    return $removed
}

function Stop-RemoteOSServer([int]$Port) {
    # Before pip touches the files: Windows cannot delete files the running python holds open
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        schtasks /End /TN "RemoteOSMCP" 2>&1 | Out-Null
        # never this process: a SelfUpgrade run is in the RemoteOSMCP-Upgrade task
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Id -ne $PID -and $_.MainWindowTitle -match "RemoteOS|WinRemote"
        } | Stop-Process -Force -ErrorAction SilentlyContinue
        # python, the CMD restart loop (it re-launches python) and the wscript launcher; never this process or
        # its parent (the installer's source and the SSH cmd.exe wrapper hold these patterns)
        $myPid = $PID
        $parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId = $myPid" -ErrorAction SilentlyContinue).ParentProcessId
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessId -ne $myPid -and $_.ProcessId -ne $parentPid -and
            ($_.CommandLine -match "-m remoteos" -or $_.CommandLine -match "start-remoteos" -or
             $_.CommandLine -match "-m winremote" -or $_.CommandLine -match "start-winremote")
        } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        $portPid = (netstat -ano | Select-String "0.0.0.0:$Port.*LISTENING" | ForEach-Object {
            ($_.ToString().Trim() -split "\s+")[-1]
        }) | Select-Object -First 1
        if ($portPid) { Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    Start-Sleep -Seconds 3
}

function Start-RemoteOSServer([string]$ConfigDir, [bool]$StartDirect) {
    # Directly only from the desktop user's own desktop run (#57), else through the task -> $null or an error
    if ($StartDirect) {
        Start-Process -FilePath "wscript.exe" -ArgumentList "`"$ConfigDir\start-remoteos.vbs`""
        return $null
    }
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        schtasks /End /TN "RemoteOSMCP" 2>&1 | Out-Null
        $taskRunOut = schtasks /Run /TN "RemoteOSMCP" 2>&1 | Out-String
        $taskRunExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($taskRunExit -ne 0) { return "schtasks /Run /TN RemoteOSMCP failed (exit $taskRunExit): $($taskRunOut.Trim())" }
    return $null
}

function Test-RemoteOSServerHealth([int]$Port, [string]$Version, [bool]$StartDirect, [int]$Attempts = 6, [int]$DelaySeconds = 5) {
    # /health = $Version; through the task the listener must be in a desktop session (#57) -> @{Ok; Message}
    $answered = $false
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        Start-Sleep -Seconds $DelaySeconds
        try {
            $healthVer = [string](Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5 -ErrorAction Stop).version
        } catch {
            continue
        }
        if ($healthVer -and $healthVer -ne $Version) {
            return [pscustomobject]@{ Ok = $false; Message = "Installed $Version but service reports $healthVer" }
        }
        if ($healthVer -ne $Version) { continue }
        if ($StartDirect) { return [pscustomobject]@{ Ok = $true; Message = "v$healthVer" } }
        $answered = $true
        $serverSession = Get-RemoteOSListenerSessionId -Port $Port
        if ($serverSession -eq 0) {
            return [pscustomobject]@{ Ok = $false; Message = "The server on port $Port runs in session 0 (no desktop): it would die with this session" }
        }
        if ($null -ne $serverSession) {
            return [pscustomobject]@{ Ok = $true; Message = "v$healthVer (the task's server, session $serverSession)" }
        }
    }
    if ($answered) {
        return [pscustomobject]@{ Ok = $false; Message = "The server answers v$Version, but the session of the process on port $Port could not be read" }
    }
    return [pscustomobject]@{ Ok = $false; Message = "Health endpoint (http://127.0.0.1:$Port/health) did not respond within $($Attempts * $DelaySeconds)s" }
}

function Invoke-RemoteOSRollback($Point, [string]$Python, [scriptblock]$StopServer, [scriptblock]$StartServer,
                                 [scriptblock]$ProveHealth) {
    # Offline back to the previous proven install: stop, reinstall, start, prove the OLD version -> @{Ok; ...}
    $null = & $StopServer
    $reinstall = Install-RemoteOSWheelhouse -Python $Python -Wheelhouse $Point.Path -Version $Point.Version
    if (-not $reinstall.Ok) { return $reinstall }
    $startError = & $StartServer
    if ($startError) { return [pscustomobject]@{ Ok = $false; Error = "$startError"; Output = '' } }
    $proof = & $ProveHealth $Point.Version
    if (-not $proof.Ok) { return [pscustomobject]@{ Ok = $false; Error = $proof.Message; Output = '' } }
    return [pscustomobject]@{ Ok = $true; Error = $null; Output = '' }
}

function Exit-RemoteOSAfterFailure([string]$Reason, [string]$Output, $Point, [string]$Python, [scriptblock]$StopServer,
                                   [scriptblock]$StartServer, [scriptblock]$ProveHealth, [bool]$Pause) {
    # Every failure after the stop: roll back when a wheelhouse is kept, then exit 1. None kept (the first
    # install with wheelhouses): the box stays as the failure left it, as before #64, and it says so.
    Write-Host "  [X] $Reason" -ForegroundColor Red
    if ($Output) { Write-Host $Output }
    if (-not $Point) {
        Write-Host "  [X] $Reason -- no previous wheelhouse to roll back to (the first install with wheelhouses): nothing was rolled back" -ForegroundColor Red
        Exit-RemoteOSInstaller -Code 1 -Pause $Pause
    }
    Write-Host "      Rolling back to v$($Point.Version) (commit $($Point.Ref)), offline from its wheelhouse..." -ForegroundColor Yellow
    $back = Invoke-RemoteOSRollback -Point $Point -Python $Python -StopServer $StopServer -StartServer $StartServer `
        -ProveHealth $ProveHealth
    if ($back.Ok) {
        Write-Host "  [X] $Reason -- rolled back to $($Point.Version) (its health check passed)" -ForegroundColor Red
    } else {
        Write-Host "  [X] $Reason -- and the rollback to $($Point.Version) failed too: $($back.Error)" -ForegroundColor Red
        if ($back.Output) { Write-Host $back.Output }
    }
    Exit-RemoteOSInstaller -Code 1 -Pause $Pause
}

