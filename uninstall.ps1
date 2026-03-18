# WinRemote MCP - Uninstaller
# Usage: irm https://raw.githubusercontent.com/zbynekdrlik/winremote-setup/main/uninstall.ps1 | iex

$ErrorActionPreference = "SilentlyContinue"
$ConfigDir = "$env:USERPROFILE\.winremote-mcp"

Write-Host ""
Write-Host "  WinRemote MCP Uninstaller" -ForegroundColor Cyan
Write-Host ""

# Stop running server
Write-Host "  [1/4] Stopping server..." -ForegroundColor White
Get-Process -Name "python*" | Where-Object {
    $_.CommandLine -match "winremote_mcp"
} | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "        Done" -ForegroundColor Green

# Remove scheduled task
Write-Host "  [2/4] Removing scheduled task..." -ForegroundColor White
Unregister-ScheduledTask -TaskName "WinRemoteMCP" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "        Done" -ForegroundColor Green

# Remove firewall rule
Write-Host "  [3/4] Removing firewall rule..." -ForegroundColor White
Remove-NetFirewallRule -DisplayName "WinRemote MCP" -ErrorAction SilentlyContinue
Write-Host "        Done" -ForegroundColor Green

# Remove config
Write-Host "  [4/4] Removing config..." -ForegroundColor White
if (Test-Path $ConfigDir) {
    Remove-Item -Recurse -Force $ConfigDir
}
Write-Host "        Done" -ForegroundColor Green

# Uninstall pip package
Write-Host ""
$uninstallPip = Read-Host "  Also uninstall winremote-mcp pip package? (y/N)"
if ($uninstallPip -eq "y") {
    python -m pip uninstall winremote-mcp -y 2>&1 | Out-Null
    Write-Host "  Package removed" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Uninstall complete." -ForegroundColor Cyan
Write-Host "  Don't forget to remove the MCP connection from Claude Code:" -ForegroundColor Gray
Write-Host "  claude mcp remove win-$($env:COMPUTERNAME.ToLower())" -ForegroundColor Gray
Write-Host ""
