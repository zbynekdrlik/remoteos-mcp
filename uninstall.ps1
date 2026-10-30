# RemoteOS MCP - Uninstaller
# Usage: in an elevated PowerShell, irm https://raw.githubusercontent.com/zbynekdrlik/remoteos-mcp/main/uninstall.ps1 | iex

$ErrorActionPreference = "SilentlyContinue"

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

function Get-RemoteOSDesktopProfile([string[]]$Sessions, [string]$UserName, [string]$FallbackProfile, [string]$ProfilesRoot) {
    # The profile the installer used: the console user's, not the one running this (an SSH or UAC admin)
    $user = $UserName
    foreach ($line in $Sessions) {
        if ($line -match "console" -and $line -match "Active") {
            $user = ($line.Trim() -split "\s+")[0].TrimStart(">")
            break
        }
    }
    $candidate = Join-Path $ProfilesRoot $user
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $FallbackProfile
}

Write-Host ""
Write-Host "  RemoteOS MCP Uninstaller" -ForegroundColor Cyan
Write-Host ""

# --- The elevation gate: the tasks, the firewall rule and ProgramData need it, so a run without it changes nothing ---
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "  [X] The RemoteOS MCP uninstaller needs an elevated (Administrator) PowerShell -- nothing was changed on this box" -ForegroundColor Red
    if (-not $PSCommandPath -and -not [Console]::IsInputRedirected) { try { Read-Host "  Press Enter to close" | Out-Null } catch {} }
    exit 1
}

$desktopProfile = Get-RemoteOSDesktopProfile -Sessions @(query user 2>&1 | ForEach-Object { "$_" }) -UserName $env:USERNAME `
    -FallbackProfile $env:USERPROFILE -ProfilesRoot "C:\Users"
$ConfigDir = "$desktopProfile\.remoteos-mcp"
$DataDir = Join-Path $env:ProgramData 'remoteos-mcp'
Write-Host "  Profile: $desktopProfile" -ForegroundColor Gray

# Stop running server: its restart loop, the launcher and python (Win32_Process: 5.1's Get-Process has no CommandLine)
Write-Host "  [1/4] Stopping server..." -ForegroundColor White
schtasks /End /TN "RemoteOSMCP" 2>&1 | Out-Null
Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and ($_.CommandLine -match "-m remoteos" -or $_.CommandLine -match "start-remoteos")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "        Done" -ForegroundColor Green

# Remove scheduled tasks
Write-Host "  [2/4] Removing scheduled tasks..." -ForegroundColor White
foreach ($task in @("RemoteOSMCP", "RemoteOSMCP-Upgrade", "RemoteOSMCP-Updater", "RemoteOSMCP-Rollback")) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host "        Done" -ForegroundColor Green

# Remove firewall rule
Write-Host "  [3/4] Removing firewall rule..." -ForegroundColor White
Remove-NetFirewallRule -DisplayName "RemoteOS MCP" -ErrorAction SilentlyContinue
Write-Host "        Done" -ForegroundColor Green

# Remove config and the wheelhouses
Write-Host "  [4/4] Removing config..." -ForegroundColor White
foreach ($folder in @($ConfigDir, $DataDir)) {
    try { Remove-RemoteOSTree $folder } catch { Write-Host "        [!] Could not remove ${folder}: $_" -ForegroundColor Yellow }
}
Write-Host "        Done" -ForegroundColor Green

# Also clean up legacy winremote-mcp if present
Unregister-ScheduledTask -TaskName "WinRemoteMCP" -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName "WinRemote MCP" -ErrorAction SilentlyContinue
try { Remove-RemoteOSTree "$desktopProfile\.winremote-mcp" } catch {}

# Uninstall pip package
Write-Host ""
$uninstallPip = Read-Host "  Also uninstall remoteos-mcp pip package? (y/N)"
if ($uninstallPip -eq "y") {
    python -m pip uninstall remoteos-mcp -y 2>&1 | Out-Null
    python -m pip uninstall winremote-mcp -y 2>&1 | Out-Null
    Write-Host "  Package removed" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Uninstall complete." -ForegroundColor Cyan
Write-Host "  Don't forget to remove the MCP connection from Claude Code:" -ForegroundColor Gray
Write-Host "  claude mcp remove win-$($env:COMPUTERNAME.ToLower())" -ForegroundColor Gray
Write-Host ""
