
# The flow, last: a truncated download defines functions but runs nothing.
$enrollRef = $env:REMOTEOS_MCP_REF
if (-not $enrollRef) { $enrollRef = 'main' }
$enrollExit = Invoke-RemoteOSEnroll -Ref $enrollRef
# irm | iex: `exit` would close the person's console before they read it, so only a file run exits
if ($PSCommandPath) { exit $enrollExit }
$global:LASTEXITCODE = $enrollExit
