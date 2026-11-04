# Stop everything. Remote access is off until windows\start.ps1.
. "$PSScriptRoot\common.ps1"
foreach ($s in $Services) { Stop-WaService $s }
Write-Output "Stopped: $($Services -join ', ')"
