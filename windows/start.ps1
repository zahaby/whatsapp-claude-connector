# Start (or restart) bridge, connector and tunnel. Fails if any service fails.
#   windows\start.ps1          (-Quiet: used by the login shortcut)
param([switch]$Quiet)
. "$PSScriptRoot\common.ps1"
foreach ($s in $Services) { Stop-WaService $s; Start-WaService $s }
if ($Services -contains 'connector') {
    if ((Invoke-Native $Py @($Verify, 'wait-healthy', '--timeout', [string](Get-RestartTimeout))) -ne 0) {
        Fail "connector did not become healthy; see $Logs\connector.log"
    }
}
if (-not $Quiet) { & "$PSScriptRoot\status.ps1" }
