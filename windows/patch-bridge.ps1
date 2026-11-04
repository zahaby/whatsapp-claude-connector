# Patch the upstream bridge in $Upstream (already checked out at the pinned
# commit) and move it to a WhatsApp library version WhatsApp still accepts.
# Every patch is verified; any mismatch stops with "Patch failed".
# Called by install.ps1, and by CI, which then really builds the result.
. "$PSScriptRoot\common.ps1"

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
Write-Output "==> Current WhatsApp library (whatsmeow $WhatsmeowVersion); WhatsApp refuses outdated clients"
# Newer whatsmeow takes a context.Context first in these five calls.
$calls = @(
    @('client.Download(downloader)', 'client.Download(context.Background(), downloader)'),
    @('sqlstore.New("sqlite3",', 'sqlstore.New(context.Background(), "sqlite3",'),
    @('container.GetFirstDevice()', 'container.GetFirstDevice(context.Background())'),
    @('client.GetGroupInfo(jid)', 'client.GetGroupInfo(context.Background(), jid)'),
    @('client.Store.Contacts.GetContact(jid)', 'client.Store.Contacts.GetContact(context.Background(), jid)'))
foreach ($c in $calls) {
    $src = $src.Replace($c[0], $c[1])
    if (-not $src.Contains($c[1])) { Fail "Patch failed: upstream changed ($($c[1])), review main.go" }
}
[IO.File]::WriteAllText($main, $src, $Utf8NoBom)
Push-Location $BridgeDir
try {
    if ((Invoke-Native 'go' @('get', "go.mau.fi/whatsmeow@$WhatsmeowVersion")) -ne 0) { Fail "could not fetch whatsmeow $WhatsmeowVersion" }
    if ((Invoke-Native 'go' @('mod', 'tidy')) -ne 0) { Fail 'go mod tidy failed' }
} finally { Pop-Location }

