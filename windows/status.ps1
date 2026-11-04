. "$PSScriptRoot\common.ps1"
foreach ($s in $Services) {
    if (Test-ServiceRunning $s) { Write-Output "running  $s" } else { Write-Output "STOPPED  $s" }
}
if (Test-Health) { Write-Output 'local  health: ok' } else { Write-Output 'local  health: DOWN' }
$h = Get-EnvValue 'MCP_PUBLIC_HOSTNAME'
if ($h) {
    try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "https://$h/healthz"; Write-Output "public health: $($r.Content)" }
    catch { Write-Output 'public health: DOWN' }
}
if ((Get-EnvValue 'WHATSAPP_ALLOW_SEND') -eq 'true') { Write-Output 'sending: ENABLED' } else { Write-Output 'sending: disabled' }
$bl = Join-Path $Logs 'bridge.log'
if (Test-Path -LiteralPath $bl) { Write-Output "bridge last log line: $(Get-Content -LiteralPath $bl -Tail 1)" }
