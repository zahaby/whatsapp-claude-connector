# One-time Cloudflare Tunnel setup. Needs a domain on your Cloudflare account.
#   windows\setup-tunnel.ps1 wa.yourdomain.com
# Outbound-only: no router ports are opened, the PC's IP is never exposed.
param([Parameter(Mandatory = $true)][string]$Hostname)
. "$PSScriptRoot\common.ps1"
$name = 'wa-connector'
$cfDir = Join-Path $HOME '.cloudflared'
# The connector's TLS certificate, which cloudflared will pin (created if missing).
$tlsDir = Get-EnvValue 'MCP_TLS_DIR'
if ($tlsDir -and -not $env:MCP_TLS_DIR) { $env:MCP_TLS_DIR = $tlsDir }   # environment wins, like the server
$caPool = (& $Py (Join-Path $Repo 'connector\tls.py') ensure)
if ($LASTEXITCODE -ne 0 -or -not $caPool) { Fail 'could not prepare the TLS certificate' }
$caPool = ([string]$caPool).Trim()
Protect-File (Join-Path (Split-Path -Parent $caPool) 'connector.key')
New-Item -ItemType Directory -Force -Path $cfDir | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $cfDir 'cert.pem'))) {
    if ((Invoke-Native 'cloudflared' @('tunnel', 'login')) -ne 0) { Fail 'cloudflared login failed' }   # browser login, owner does this
}
if ((Invoke-Native 'cloudflared' @('tunnel', 'info', $name)) -ne 0) {
    if ((Invoke-Native 'cloudflared' @('tunnel', 'create', $name)) -ne 0) { Fail 'tunnel create failed' }
}
# Options must come BEFORE the tunnel name: cloudflared stops reading flags
# at the first plain argument ("accepts exactly one argument" otherwise).
$ErrorActionPreference = 'Continue'
$json = (& cloudflared tunnel info -o json $name 2>$null) -join "`n"
$ErrorActionPreference = 'Stop'
$tunnelId = $null
try { $tunnelId = [string](($json | ConvertFrom-Json).id) } catch { }
if (-not $tunnelId -or $tunnelId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
    Fail "could not read the tunnel id (cloudflared tunnel info -o json $name)"
}
if ((Invoke-Native 'cloudflared' @('tunnel', 'route', 'dns', $name, $Hostname)) -ne 0) { Fail 'DNS route failed' }
$tpl = [IO.File]::ReadAllText((Join-Path $Repo 'config\cloudflared.yml'))
$out = $tpl.Replace('__TUNNEL_ID__', $tunnelId).Replace('__HOME__', $HOME).Replace('__HOSTNAME__', $Hostname)
$out = $out.Replace('__PORT__', [string](Get-Port)).Replace('/.cloudflared/', '\.cloudflared\')
$out = $out.Replace('__CA_POOL__', $caPool.Replace("'", "''"))   # inside YAML single quotes
[IO.File]::WriteAllText($TunnelConfig, $out, $Utf8NoBom)
Set-EnvValue 'MCP_PUBLIC_HOSTNAME' $Hostname
Write-Output "Tunnel ready: https://$Hostname  ->  127.0.0.1:$(Get-Port)"
