# --- Enrollment (#26): only with REMOTEOS_ENROLL set; a plain run never calls these --------------

function Test-RemoteOSEnrollUrl([string]$Url) {
    # The one-time URL `remoteos-gateway enroll` prints: https, a host, one token segment
    return [bool]($Url -and $Url.Length -le 300 -and $Url -cmatch '^https://[a-z0-9.-]+/[A-Za-z0-9_-]+/\z')
}

function Test-RemoteOSSshPublicKey([string]$Key) {
    # One public key line: no options (command=...), no second line
    return [bool]($Key -and $Key.Length -le 2000 -and
        $Key -cmatch '^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+( [A-Za-z0-9@._-]+)?\z')
}

function Test-RemoteOSMachinePython([string]$Root) {
    # A whole install, not a half one: the interpreter and its DLL
    return (Test-Path -LiteralPath (Join-Path $Root 'python.exe')) -and (Test-Path -LiteralPath (Join-Path $Root 'python312.dll'))
}

function Install-RemoteOSMachinePython([string]$Root) {
    # Python 3.12 for all users, with installers that wait. -> the path of python.exe, or throws.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            $r = Invoke-RemoteOSNative 'winget' @('install', '--id', 'Python.Python.3.12', '--exact', '--scope', 'machine',
                '--silent', '--accept-package-agreements', '--accept-source-agreements')
            if ($r.Exit -eq 0 -and (Test-RemoteOSMachinePython $Root)) { return (Join-Path $Root 'python.exe') }
            Write-Host "        [!] winget did not install it (exit $($r.Exit)):" -ForegroundColor Yellow
            Write-Host $r.Output
        } catch {
            # The app alias does not start outside a desktop session
            Write-Host "        [!] winget could not run: $_" -ForegroundColor Yellow
        }
    }
    $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $installer -UseBasicParsing
    try {
        $process = Start-Process -FilePath $installer -Wait -PassThru `
            -ArgumentList @('/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_pip=1')
        if ($process.ExitCode -notin @(0, 3010)) { throw "the Python installer exited with $($process.ExitCode)" }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-RemoteOSMachinePython $Root)) { throw "no python312.dll in $Root after the Python installer" }
    return (Join-Path $Root 'python.exe')
}

function Add-RemoteOSAuthorizedKey([string]$Path, [string]$Key) {
    # One line, added once, no BOM (sshd reads a BOM as part of the first key); other lines are kept
    $folder = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    $text = ''
    if (Test-Path -LiteralPath $Path) { $text = [IO.File]::ReadAllText($Path) }
    if (@($text -split "`r?`n") -ccontains $Key) { return }
    if ($text -and -not $text.EndsWith("`n")) { $text += "`n" }
    [IO.File]::WriteAllText($Path, $text + $Key + "`n", (New-Object System.Text.UTF8Encoding($false)))
}

function Get-RemoteOSAuthorizedKeysPath([bool]$Admin, [string]$DataRoot, [string]$UserProfile) {
    # sshd_config's Match Group administrators reads the shared file for an administrator
    if ($Admin) { return (Join-Path $DataRoot 'ssh\administrators_authorized_keys') }
    return (Join-Path $UserProfile '.ssh\authorized_keys')
}

function Test-RemoteOSAccountAdmin([string]$User) {
    $sid = (Get-RemoteOSAccount $User).Value
    return [bool](@(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | Where-Object { "$($_.SID)" -ceq $sid }).Count)
}

function Enable-RemoteOSSsh([string]$Key, [string]$User, [string]$UserProfile) {
    # The OpenSSH server (Automatic, running, TCP 22) + dev1's key. Throws on a failure.
    if (-not (Get-Service -Name sshd -ErrorAction SilentlyContinue)) {
        $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
        if (-not $capability) { throw "Windows offers no OpenSSH.Server capability" }
        Add-WindowsCapability -Online -Name $capability.Name | Out-Null
    }
    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd
    if (-not (Get-NetFirewallRule -Name 'RemoteOS-SSH' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'RemoteOS-SSH' -DisplayName 'RemoteOS SSH' -Direction Inbound -LocalPort 22 -Protocol TCP -Action Allow -Profile Any | Out-Null
    }
    # This run is elevated (the gate): the same account is an administrator, however it became one
    $admin = ($env:USERNAME -ieq $User) -or (Test-RemoteOSAccountAdmin $User)
    $path = Get-RemoteOSAuthorizedKeysPath -Admin $admin -DataRoot $env:ProgramData -UserProfile $UserProfile
    Add-RemoteOSAuthorizedKey -Path $path -Key $Key
    if ($admin) {
        # sshd ignores this file unless only Administrators and SYSTEM reach it
        $r = Invoke-RemoteOSNative 'icacls' @($path, '/inheritance:r', '/grant', '*S-1-5-32-544:F', '/grant', '*S-1-5-18:F')
        if ($r.Exit -ne 0) { throw "icacls refused ${path}: $($r.Output)" }
    } else {
        Set-RemoteOSOwnerOnlyAcl -Path $path -User $User
    }
    return $path
}

function Get-RemoteOSEnrollmentBody([string]$HostName, [string[]]$Addresses, [int]$ServerPort, [string]$Version,
                                    [string]$Account, [string]$AuthKey) {
    # The registration. @() + [string[]]: a single address must stay a JSON list
    return ([ordered]@{
        hostname = $HostName; ips = @($Addresses); port = $ServerPort; version = $Version; os = 'windows'
        account = $Account; auth_key = $AuthKey
    } | ConvertTo-Json -Compress)
}

function Send-RemoteOSEnrollment([string]$Url, [string]$Body, [int]$Tries = 3, [int]$DelaySeconds = 5) {
    # POST in this process (no argv). A named user agent: Cloudflare answers 403 to a default one. Only
    # the receiver's own ok counts, never a redirect. -> $null, or the last error, which holds no body.
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
    $lastError = 'no attempt'
    for ($try = 1; $try -le $Tries; $try++) {
        try {
            $reply = Invoke-RestMethod -Method Post -Uri $Url -Body $bytes -ContentType 'application/json' `
                -UserAgent 'remoteos-enroll' -TimeoutSec 30 -MaximumRedirection 0 -ErrorAction Stop
            if ("$reply".Trim() -ceq 'ok') { return $null }
            $lastError = 'the receiver did not answer ok'
        } catch {
            $lastError = $_.Exception.Message
        }
        Write-Host "        [!] Registration attempt $try failed" -ForegroundColor Yellow
        if ($try -lt $Tries) { Start-Sleep -Seconds $DelaySeconds }
    }
    return $lastError
}

function Exit-RemoteOSEnrollmentFailure([string]$Reason, [bool]$Pause) {
    # Past the proof: the install stands and nothing rolls back
    Write-Host "  [X] $Reason -- the install stands" -ForegroundColor Red
    Exit-RemoteOSInstaller -Code 1 -Pause $Pause
}

