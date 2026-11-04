# Generate a new 256-bit token and PROVE the running connector switched to it.
#   windows\rotate-token.ps1              rotate + restart + verify
#   windows\rotate-token.ps1 -NoRestart   only write a token
param([switch]$NoRestart)
. "$PSScriptRoot\common.ps1"
$old = Get-EnvValue 'MCP_AUTH_TOKEN'
Set-NewToken

if ($NoRestart) {
    Write-Output "New token written to $EnvFile (connector not restarted)."
} elseif (Test-ServiceRunning 'connector') {
    $ok = $true
    try {
        Restart-Connector
        $env:WA_OLD_TOKEN = $old
        if ((Invoke-Native $Py @($Verify, 'token')) -ne 0) { $ok = $false }
    } catch {
        [Console]::Error.WriteLine("ERROR: $_")
        $ok = $false
    } finally {
        Remove-Item Env:\WA_OLD_TOKEN -ErrorAction SilentlyContinue
    }
    if (-not $ok) {
        [Console]::Error.WriteLine('ERROR: rotation NOT verified. The OLD connector URL may STILL WORK.')
        [Console]::Error.WriteLine("       Check windows\status.ps1 and $Logs\connector.log, then re-run this script.")
        exit 1
    }
    Write-Output 'Verified: the running connector accepts the new token and rejects the old one.'
} else {
    Write-Output 'Connector is not running, so no URL works right now.'
    Write-Output 'The new token takes effect on windows\start.ps1.'
}

if (Get-EnvValue 'MCP_PUBLIC_HOSTNAME') {
    Write-Output 'Update the connector URL in claude.ai (Settings > Connectors): windows\connector-url.ps1'
} else {
    Write-Output 'No public hostname yet; run windows\setup-tunnel.ps1 when ready.'
}
