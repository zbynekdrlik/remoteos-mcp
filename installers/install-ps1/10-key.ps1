# --- The auth key (#12): never on a command line, never printed ---------------------------------
# Precedence: an explicit REMOTEOS_MCP_AUTH_KEY, the running start script's, config.json, remoteos.toml's
# [server] auth_key, the old winremote config, else new. The server reads REMOTEOS_AUTH_KEY, never --auth-key.

function Test-RemoteOSKey([string]$Key) {
    # 1-128 ASCII letters/digits; \z, not $: $ also matches before a trailing newline
    return [bool]($Key -and $Key.Length -le 128 -and $Key -cmatch '^[A-Za-z0-9]+\z')
}

function New-RemoteOSKey {
    # 32 characters uniformly from [A-Za-z0-9] by a CSPRNG (bytes >= 248 = 4 * 62 are dropped: no bias)
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
    # start-remoteos.bat: `set "REMOTEOS_AUTH_KEY=<key>"`, or the legacy `-m remoteos ... --auth-key "<key>"`
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
    # The first key decides; one this installer cannot carry is refused, never replaced (clients would break)
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
    # An auto-restart loop with a crash guard; the key is set only around the server line (#12 round 10)
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
    # A protected ACL for a file holding the key: the desktop user, SYSTEM and Administrators only
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

