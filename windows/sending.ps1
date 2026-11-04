# THE SEND SWITCH.   windows\sending.ps1 on | off | status
# on/off only report success after the RUNNING remote connector is observed
# exposing (or not exposing) the send tool.
param([ValidateSet('on', 'off', 'status')][string]$Mode = 'status')
. "$PSScriptRoot\common.ps1"

function Test-Sending([string]$Expect) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $Py $Verify sending --expect $Expect 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    finally { $ErrorActionPreference = $old }
}

if ($Mode -eq 'status') {
    Write-Output "configured: WHATSAPP_ALLOW_SEND=$(Get-EnvValue 'WHATSAPP_ALLOW_SEND')"
    if (Test-ServiceRunning 'connector') {
        if (Test-Sending 'on') { Write-Output 'running remote connector: sending ON' }
        elseif (Test-Sending 'off') { Write-Output 'running remote connector: sending OFF' }
        else { Write-Output 'running remote connector: UNKNOWN (not reachable)' }
    } else {
        Write-Output 'remote connector: not running'
    }
    exit 0
}

if ($Mode -eq 'on') { Set-EnvValue 'WHATSAPP_ALLOW_SEND' 'true' } else { Set-EnvValue 'WHATSAPP_ALLOW_SEND' 'false' }
$upper = $Mode.ToUpper()
Write-Output "configured: WHATSAPP_ALLOW_SEND=$(Get-EnvValue 'WHATSAPP_ALLOW_SEND')"
if (Test-ServiceRunning 'connector') {
    $ok = $true
    try {
        Restart-Connector
        if ((Invoke-Native $Py @($Verify, 'sending', '--expect', $Mode)) -ne 0) { $ok = $false }
    } catch {
        [Console]::Error.WriteLine("ERROR: $_")
        $ok = $false
    }
    if (-not $ok) {
        [Console]::Error.WriteLine("ERROR: the running remote connector was NOT confirmed as sending $upper.")
        if ($Mode -eq 'off') { [Console]::Error.WriteLine('       Sending may STILL BE ENABLED. Run windows\stop.ps1 to be safe.') }
        exit 1
    }
    Write-Output "Verified: running remote connector has sending $upper."
} else {
    Write-Output 'Remote connector not running; the setting applies on windows\start.ps1.'
}
Write-Output 'Claude Desktop (local): quit and reopen it to apply. Claude app: start a new chat.'
