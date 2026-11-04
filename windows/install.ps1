# One-time install on Windows. Safe to re-run. No administrator rights needed.
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
. "$PSScriptRoot\common.ps1"

Write-Output '==> Checking tools'
$missing = @()
foreach ($t in @(
        @{ n = 'git';         hint = 'winget install --id Git.Git -e' },
        @{ n = 'go';          hint = 'winget install --id GoLang.Go -e' },
        @{ n = 'gcc';         hint = 'winget install --id BrechtSanders.WinLibs.POSIX.UCRT -e   (C compiler for go-sqlite3)' },
        @{ n = 'uv';          hint = 'winget install --id astral-sh.uv -e' },
        @{ n = 'cloudflared'; hint = 'winget install --id Cloudflare.cloudflared -e' })) {
    if (-not (Get-Command $t.n -ErrorAction SilentlyContinue)) { $missing += "  $($t.n):  $($t.hint)" }
}
if ($missing.Count) { Fail ("Missing tools. Install them, open a NEW PowerShell window, and re-run:`n" + ($missing -join "`n")) }

Write-Output "==> Upstream lharries/whatsapp-mcp at $Upstream (pinned $UpstreamCommit)"
if (-not (Test-Path -LiteralPath (Join-Path $Upstream '.git'))) {
    if ((Invoke-Native 'git' @('clone', 'https://github.com/lharries/whatsapp-mcp.git', $Upstream)) -ne 0) { Fail 'git clone failed' }
}
if ((Invoke-Native 'git' @('-C', $Upstream, 'fetch', '-q', 'origin')) -ne 0) { Fail 'git fetch failed' }
if ((Invoke-Native 'git' @('-C', $Upstream, 'checkout', '-q', $UpstreamCommit)) -ne 0) { Fail 'git checkout failed' }

$main = Join-Path $BridgeDir 'main.go'
$src = [IO.File]::ReadAllText($main)

Write-Output '==> Hardening bridge: REST API (incl. unauthenticated /api/send) binds to 127.0.0.1 only'
$src = $src.Replace('serverAddr := fmt.Sprintf(":%d", port)', 'serverAddr := fmt.Sprintf("127.0.0.1:%d", port)')
if (-not $src.Contains('fmt.Sprintf("127.0.0.1:%d", port)')) { Fail 'Patch failed: upstream changed, review main.go' }

Write-Output '==> Privacy: stop the bridge printing message text to the terminal/log'
$src = [regex]::Replace($src,
    'fmt\.Printf\("\[%s\] %s %s: (\[%s: %s\] )?%s\\n", timestamp, direction, sender(, mediaType, filename)?, content\)',
    'fmt.Printf("[%s] %s message stored\n", timestamp, direction)')
if ([regex]::IsMatch($src, 'fmt\.Printf\(.*sender.*content\)')) { Fail 'Patch failed: bridge still prints message content, review main.go' }
[IO.File]::WriteAllText($main, $src, $Utf8NoBom)

Write-Output '==> Building bridge (CGO, needs gcc)'
$env:CGO_ENABLED = '1'
Push-Location $BridgeDir
try {
    if ((Invoke-Native 'go' @('build', '-o', 'whatsapp-bridge.exe', '.')) -ne 0) { Fail 'go build failed (is gcc on PATH?)' }
} finally { Pop-Location }

Write-Output '==> Connector virtualenv'
Push-Location (Join-Path $Repo 'connector')
try {
    if ((Invoke-Native 'uv' @('sync', '-q')) -ne 0) { Fail 'uv sync failed' }
} finally { Pop-Location }

if (-not (Test-Path -LiteralPath $EnvFile)) {
    $example = [IO.File]::ReadAllLines((Join-Path $Repo 'connector\.env.example'))
    [IO.File]::WriteAllLines($EnvFile, $example, $Utf8NoBom)
    Set-EnvValue 'WA_DB_PATH' (Join-Path $BridgeDir 'store\messages.db')
}
Protect-File $EnvFile
if (-not (Get-EnvValue 'MCP_AUTH_TOKEN')) { Set-NewToken; Write-Output 'Generated connector token.' }

Write-Output '==> Loopback TLS certificate (cloudflared and the scripts pin it)'
$tlsDir = Get-EnvValue 'MCP_TLS_DIR'
if ($tlsDir -and -not $env:MCP_TLS_DIR) { $env:MCP_TLS_DIR = $tlsDir }   # environment wins, like the server
$cert = (& $Py (Join-Path $Repo 'connector\tls.py') ensure)
if ($LASTEXITCODE -ne 0 -or -not $cert) { Fail 'could not create the TLS certificate' }
Protect-File (Join-Path (Split-Path -Parent ([string]$cert).Trim()) 'connector.key')

Write-Output '==> Start at login (shortcut in your Startup folder)'
New-Item -ItemType Directory -Force -Path $Logs, $StartupDir | Out-Null
$lnk = Join-Path $StartupDir 'wa-connector.lnk'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $WinPS
$sc.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot 'start.ps1')`" -Quiet"
$sc.WorkingDirectory = $Repo
$sc.WindowStyle = 7   # minimised
$sc.Description = 'WhatsApp-Claude connector'
$sc.Save()

$desktopCfg = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
$entry = @{ mcpServers = @{ 'whatsapp-family' = @{ command = $Py; args = @($ServerPy, '--transport', 'stdio') } } } |
         ConvertTo-Json -Depth 5
Write-Output @"

Install done. Next steps (see README, Windows section):
  1. Link WhatsApp (you scan the QR):    windows\link-whatsapp.ps1
  2. Claude Desktop: merge this into $desktopCfg
$entry
  3. Tunnel (once): windows\setup-tunnel.ps1 <hostname>, then windows\start.ps1
"@
