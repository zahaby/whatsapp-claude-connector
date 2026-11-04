# One-time Cloudflare Tunnel setup. Needs a domain on your Cloudflare account.
#   windows\setup-tunnel.ps1 wa.yourdomain.com
# Outbound-only: no router ports are opened, the PC's IP is never exposed.
param([Parameter(Mandatory = $true)][string]$Hostname)
. "$PSScriptRoot\common.ps1"
$name = 'wa-connector'
$cfDir = Join-Path $HOME '.cloudflared'
if (-not (Test-Path -LiteralPath (Join-Path $cfDir 'cert.pem'))) {
    if ((Invoke-Native 'cloudflared' @('tunnel', 'login')) -ne 0) { Fail 'cloudflared login failed' }   # browser login, owner does this
}
if ((Invoke-Native 'cloudflared' @('tunnel', 'info', $name)) -ne 0) {
    if ((Invoke-Native 'cloudflared' @('tunnel', 'create', $name)) -ne 0) { Fail 'tunnel create failed' }
}
$info = (& cloudflared tunnel info $name -o json) | ConvertFrom-Json
if ((Invoke-Native 'cloudflared' @('tunnel', 'route', 'dns', $name, $Hostname)) -ne 0) { Fail 'DNS route failed' }
$tpl = [IO.File]::ReadAllText((Join-Path $Repo 'config\cloudflared.yml'))
$out = $tpl.Replace('__TUNNEL_ID__', $info.id).Replace('__HOME__', $HOME).Replace('__HOSTNAME__', $Hostname)
$out = $out.Replace('__PORT__', [string](Get-Port)).Replace('/.cloudflared/', '\.cloudflared\')
[IO.File]::WriteAllText($TunnelConfig, $out, $Utf8NoBom)
Set-EnvValue 'MCP_PUBLIC_HOSTNAME' $Hostname
Write-Output "Tunnel ready: https://$Hostname  ->  127.0.0.1:$(Get-Port)"
