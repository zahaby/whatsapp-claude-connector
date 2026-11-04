# Copies the secret connector URL to the clipboard (default, so it never shows
# on a shared screen). -Print writes it to the terminal instead.
param([switch]$Print)
. "$PSScriptRoot\common.ps1"
$h = Get-EnvValue 'MCP_PUBLIC_HOSTNAME'
if (-not $h) { Fail 'MCP_PUBLIC_HOSTNAME not set; run windows\setup-tunnel.ps1' }
$url = "https://$h/mcp/$(Get-EnvValue 'MCP_AUTH_TOKEN')"
if ($Print) { Write-Output $url }
else { Set-Clipboard -Value $url; Write-Output 'Connector URL copied to clipboard (treat it like a password).' }
