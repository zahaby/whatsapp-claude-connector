# DAILY GROUP SUMMARY: a separate, minimal connector an unattended scheduled
# task uses to read ONE group and post one summary a day into it.
#
#   windows\daily-summary.ps1 set "<group name>"   choose the group (exact chat name)
#   windows\daily-summary.ps1 on | off             switch the daily endpoint (verified)
#   windows\daily-summary.ps1 url [-Print]         copy its connector URL
#   windows\daily-summary.ps1 rotate               new secret for its URL (verified)
#   windows\daily-summary.ps1 status               group, switch, last post
#   windows\daily-summary.ps1 reset-limit          allow the next post now (for testing)
param(
    [ValidateSet('set', 'on', 'off', 'url', 'rotate', 'status', 'reset-limit')][string]$Action = 'status',
    [string]$Name,
    [switch]$Print
)
. "$PSScriptRoot\common.ps1"
$Daily = Join-Path $Repo 'connector\daily.py'

function Set-NewDailyToken { Set-EnvValue 'MCP_DAILY_TOKEN' (New-Token) }

function Invoke-ApplyAndVerify([string]$Want) {
    $upper = $Want.ToUpper()
    if (Test-ServiceRunning 'connector') {
        $ok = $true
        try {
            Restart-Connector
            if ((Invoke-Native $Py @($Verify, 'autopost', '--expect', $Want)) -ne 0) { $ok = $false }
        } catch {
            [Console]::Error.WriteLine("ERROR: $_")
            $ok = $false
        }
        if (-not $ok) {
            [Console]::Error.WriteLine("ERROR: the running connector was NOT confirmed with the daily summary $upper.")
            if ($Want -eq 'off') { [Console]::Error.WriteLine('       It may STILL BE ON. Run windows\stop.ps1 to be safe.') }
            exit 1
        }
        Write-Output "Verified: the running connector has the daily summary $upper."
    } else {
        Write-Output 'Connector not running; the setting applies on windows\start.ps1.'
    }
}

switch ($Action) {
    'set' {
        if (-not $Name) { Fail 'usage: windows\daily-summary.ps1 set "<group name>"' }
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $line = & $Py $Daily resolve $Name
        $code = $LASTEXITCODE
        $ErrorActionPreference = $old
        if ($code -ne 0 -or -not $line) { exit 1 }
        $parts = ([string]$line).Split("`t", 2)
        Set-EnvValue 'WA_AUTOPOST_CHAT' $parts[0]
        Write-Output "Daily summary group: $($parts[1])"
        if ((Get-EnvValue 'WHATSAPP_AUTOPOST') -eq 'true') { Invoke-ApplyAndVerify 'on' }
    }
    'on' {
        if (-not (Get-EnvValue 'WA_AUTOPOST_CHAT')) { Fail 'choose the group first: windows\daily-summary.ps1 set "<group name>"' }
        if (-not (Get-EnvValue 'MCP_DAILY_TOKEN')) { Set-NewDailyToken }
        Set-EnvValue 'WHATSAPP_AUTOPOST' 'true'
        Invoke-ApplyAndVerify 'on'
        Write-Output "Next: windows\daily-summary.ps1 url  (then add it in claude.ai as a separate connector, e.g. 'WhatsApp Daily')."
    }
    'off' {
        Set-EnvValue 'WHATSAPP_AUTOPOST' 'false'
        Invoke-ApplyAndVerify 'off'
    }
    'rotate' {
        Set-NewDailyToken
        if ((Get-EnvValue 'WHATSAPP_AUTOPOST') -eq 'true') { Invoke-ApplyAndVerify 'on' }
        Write-Output 'New daily URL created; the old one no longer works. Update it in claude.ai: windows\daily-summary.ps1 url'
    }
    'url' {
        $h = Get-EnvValue 'MCP_PUBLIC_HOSTNAME'; $t = Get-EnvValue 'MCP_DAILY_TOKEN'
        if (-not $h) { Fail 'MCP_PUBLIC_HOSTNAME not set; run windows\setup-tunnel.ps1' }
        if (-not $t) { Fail 'daily summary not set up yet: windows\daily-summary.ps1 set "<group>" then on' }
        $url = "https://$h/daily/$t"
        if ($Print) { Write-Output $url }
        else { Set-Clipboard -Value $url; Write-Output 'Daily-summary connector URL copied to clipboard (treat it like a password).' }
    }
    'status' {
        $null = Invoke-Native $Py @($Daily, 'status')
        if (Test-ServiceRunning 'connector') {
            $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            & $Py $Verify autopost --expect on 2>$null | Out-Null
            $on = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = $old
            if ($on) { Write-Output 'running connector: daily summary ON' } else { Write-Output 'running connector: daily summary OFF' }
        } else {
            Write-Output 'connector: not running'
        }
    }
    'reset-limit' { $null = Invoke-Native $Py @($Daily, 'reset-limit') }
}
