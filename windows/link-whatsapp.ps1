# Run in a normal PowerShell window. The OWNER scans the QR code with their
# phone (WhatsApp > Settings > Linked devices > Link a device).
# Also used for re-linking. -Fresh wipes the local session first.
param([switch]$Fresh)
. "$PSScriptRoot\common.ps1"
Stop-WaService 'bridge'
if ($Fresh) {
    Write-Output 'Removing local WhatsApp session + message cache (your phone is unaffected).'
    Remove-Item -LiteralPath (Join-Path $BridgeDir 'store\whatsapp.db'), (Join-Path $BridgeDir 'store\messages.db') -ErrorAction SilentlyContinue
}
Set-Location $BridgeDir
Write-Output "When you see 'Connected to WhatsApp' and history has synced, press Ctrl-C."
Write-Output 'Then run windows\start.ps1 to run it in the background.'
$ErrorActionPreference = 'Continue'
& $BridgeExe
