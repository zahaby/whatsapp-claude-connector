# VIDEO / VOICE-NOTE TRANSCRIPTION (local, on this PC).
#   windows\media-setup.ps1 on      install the speech model (~1.5 GB once), switch on (verified)
#   windows\media-setup.ps1 off     switch off (verified); nothing is uninstalled
#   windows\media-setup.ps1 status
# Links and YouTube captions work without this.
param([ValidateSet('on', 'off', 'status')][string]$Action = 'status')
. "$PSScriptRoot\common.ps1"

function Invoke-ApplyAndVerify([string]$Want) {
    $upper = $Want.ToUpper()
    if (Test-ServiceRunning 'connector') {
        $ok = $true
        try {
            Restart-Connector
            if ((Invoke-Native $Py @($Verify, 'media', '--expect', $Want)) -ne 0) { $ok = $false }
        } catch {
            [Console]::Error.WriteLine("ERROR: $_")
            $ok = $false
        }
        if (-not $ok) {
            [Console]::Error.WriteLine("ERROR: the running connector was NOT confirmed with transcription $upper.")
            exit 1
        }
        Write-Output "Verified: the running connector has transcription $upper."
    } else {
        Write-Output 'Connector not running; the setting applies on windows\start.ps1.'
    }
}

switch ($Action) {
    'on' {
        Write-Output '==> Installing the transcription packages'
        Push-Location (Join-Path $Repo 'connector')
        try {
            if ((Invoke-Native 'uv' @('sync', '-q', '--extra', 'media')) -ne 0) { Fail 'could not install the transcription packages' }
        } finally { Pop-Location }
        if (-not (Get-EnvValue 'WA_WHISPER_MODEL')) { Set-EnvValue 'WA_WHISPER_MODEL' 'medium' }
        $model = Get-EnvValue 'WA_WHISPER_MODEL'
        if ((Invoke-Native $Py @((Join-Path $Repo 'connector\media.py'), 'download-model', $model)) -ne 0) {
            Fail 'could not download the speech model'
        }
        Set-EnvValue 'WA_MEDIA' 'true'
        Invoke-ApplyAndVerify 'on'
    }
    'off' {
        Set-EnvValue 'WA_MEDIA' 'false'
        Invoke-ApplyAndVerify 'off'
    }
    'status' {
        Write-Output "configured: WA_MEDIA=$(Get-EnvValue 'WA_MEDIA') model=$(Get-EnvValue 'WA_WHISPER_MODEL')"
        if (Test-ServiceRunning 'connector') {
            $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            & $Py $Verify media --expect on 2>$null | Out-Null
            $on = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = $old
            if ($on) { Write-Output 'running connector: transcription ON' } else { Write-Output 'running connector: transcription OFF' }
        } else {
            Write-Output 'connector: not running'
        }
    }
}
