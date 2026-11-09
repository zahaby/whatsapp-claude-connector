# Shared paths and helpers. Dot-sourced by the other scripts:  . "$PSScriptRoot\common.ps1"
# Windows PowerShell 5.1 compatible (no ternary, no ??, no &&).
#
# Every helper that changes a service THROWS on failure, and nothing reports
# success unless the change was observed on the running connector.
#
# Services: bridge, connector, tunnel. Each runs under a hidden supervisor
# (supervise.ps1) that restarts it if it crashes, like launchd KeepAlive on
# macOS. A shortcut in the user's Startup folder starts them at login; no
# administrator rights are needed.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Text between Python and PowerShell is UTF-8 both ways. Without this, Python
# writes in the Windows ANSI code page and PowerShell reads it in the console's
# DOS code page, so non-Latin chat names (e.g. Arabic) come out as garbage.
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

function Get-Setting([string]$Name, [string]$Default) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($v) { return $v }
    return $Default
}

$Repo           = Split-Path -Parent $PSScriptRoot
$Upstream       = Get-Setting 'UPSTREAM' (Join-Path $HOME 'whatsapp-mcp')
$UpstreamCommit = Get-Setting 'UPSTREAM_COMMIT' '7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88'  # audited revision
# WhatsApp rejects old client libraries ("Client outdated (405)"), so the
# bridge is built against this newer whatsmeow instead of upstream's pin.
# Bump it (and re-run install.ps1) if WhatsApp starts refusing it again.
$WhatsmeowVersion = Get-Setting 'WHATSMEOW_VERSION' 'v0.0.0-20261102152715-54038813c47f'
$EnvFile        = Get-Setting 'WA_ENV_FILE' (Join-Path $Repo 'connector\.env')
$Py             = Get-Setting 'WA_PYTHON' (Join-Path $Repo 'connector\.venv\Scripts\python.exe')
$Verify         = Join-Path $Repo 'connector\verify.py'
$ServerPy       = Join-Path $Repo 'connector\server.py'
$Logs           = Get-Setting 'WA_LOG_DIR' (Join-Path $env:LOCALAPPDATA 'wa-connector\logs')
$StartupDir     = Get-Setting 'WA_STARTUP_DIR' ([Environment]::GetFolderPath('Startup'))
$BridgeDir      = Join-Path $Upstream 'whatsapp-bridge'
$BridgeExe      = Join-Path $BridgeDir 'whatsapp-bridge.exe'
$TunnelConfig   = Join-Path $HOME '.cloudflared\wa-connector.yml'
$Services       = @((Get-Setting 'WA_SERVICES' 'bridge,connector,tunnel').Split(',') | Where-Object { $_ })
$Supervisor     = Join-Path $PSScriptRoot 'supervise.ps1'
$WinPS          = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$env:WA_CONFIG  = $EnvFile   # verify.py / server.py read the same file

function Fail([string]$Message) {
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}

# ---- native commands ----------------------------------------------------------
# Windows PowerShell 5.1 turns a native program's stderr into error records,
# which $ErrorActionPreference='Stop' would make fatal. Run natives with
# 'Continue', pass their output through, and return the exit code.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { [Console]::Error.WriteLine($_.Exception.Message) }
            else { [Console]::Out.WriteLine([string]$_) }
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
}

# ---- .env ---------------------------------------------------------------------
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)   # a BOM would corrupt the first key

function Get-EnvValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return '' }
    foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
        if ($line.StartsWith("$Key=")) { return $line.Substring($Key.Length + 1) }
    }
    return ''
}

function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = New-Object System.Collections.Generic.List[string]
    $found = $false
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
            if ((-not $found) -and $line.StartsWith("$Key=")) { $lines.Add("$Key=$Value"); $found = $true }
            else { $lines.Add($line) }
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    [IO.File]::WriteAllLines($EnvFile, $lines, $Utf8NoBom)
    Protect-File $EnvFile
}

# Owner-only access: exactly one rule, Full Control for the current user,
# inheritance off and nothing copied from the folder. Then READ IT BACK and
# refuse to continue unless that is precisely what the file now has.
function Protect-File([string]$Path) {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User   # SID, immune to name formats
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
    [System.IO.File]::SetAccessControl($Path, $acl)   # writes the DACL only

    $actual = [System.IO.File]::GetAccessControl($Path)
    $rules = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    $ok = $actual.AreAccessRulesProtected -and $rules.Count -eq 1 -and
          $rules[0].IdentityReference.Value -eq $me.Value -and -not $rules[0].IsInherited -and
          $rules[0].AccessControlType -eq 'Allow'
    if (-not $ok) {
        $desc = ($rules | ForEach-Object { "$($_.IdentityReference) $($_.AccessControlType) $($_.FileSystemRights) inherited=$($_.IsInherited)" }) -join '; '
        throw "could not restrict ${Path} to the current user only (now: $desc)"
    }
}

function New-Token {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')  # 43 chars
}

function Set-NewToken { Set-EnvValue 'MCP_AUTH_TOKEN' (New-Token) }

function Get-Port {
    $p = Get-EnvValue 'MCP_PORT'
    if ($p) { return [int]$p }
    return 8765
}

# ---- services (supervised processes) -----------------------------------------
function Get-Needle([string]$Svc) {
    # A string that appears in the command line of the service's own process
    # (and of the cmd.exe that redirects its output), and of nothing else.
    switch ($Svc) {
        'connector' { return $ServerPy }
        'bridge'    { return $BridgeExe }
        'tunnel'    { return $TunnelConfig }
    }
    throw "unknown service $Svc"
}

function Get-AllProcesses { return @(Get-CimInstance -ClassName Win32_Process) }

function Get-SupervisorProcesses([string]$Svc) {
    return @(Get-AllProcesses | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains($Supervisor) -and $_.CommandLine.Contains("-Kind $Svc")
    })
}

function Get-ServiceProcesses([string]$Svc) {
    $needle = Get-Needle $Svc
    return @(Get-AllProcesses | Where-Object {
        $cl = $_.CommandLine
        $cl -and $cl.Contains($needle) -and -not $cl.Contains($Supervisor) -and
            # Never match Claude Desktop's own local connector (--transport stdio).
            (($Svc -ne 'connector') -or $cl.Contains('--transport http'))
    })
}

function Test-ServiceRunning([string]$Svc) { return @(Get-SupervisorProcesses $Svc).Count -gt 0 }

function Wait-Until([scriptblock]$Condition, [double]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return [bool](& $Condition)
}

function Get-RestartTimeout { return [double](Get-Setting 'WA_RESTART_TIMEOUT' '20') }

# Test-only fault injection, used by connector/tests/test_scripts_windows.py
# to prove failures are reported. Inert unless WA_TEST_FAULT is set.
function Invoke-TestFault([string]$Point) {
    if ($env:WA_TEST_FAULT -eq $Point) { throw "simulated failure ($Point)" }
}

function Stop-WaService([string]$Svc) {
    Invoke-TestFault 'stop-fail'
    # Supervisor first, so it cannot restart the service we are stopping.
    foreach ($p in (Get-SupervisorProcesses $Svc)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($p in (Get-ServiceProcesses $Svc))    { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    $gone = Wait-Until { (@(Get-SupervisorProcesses $Svc).Count + @(Get-ServiceProcesses $Svc).Count) -eq 0 } 10
    if (-not $gone) { throw "could not stop $Svc (processes still running)" }
}

function Start-WaService([string]$Svc) {
    if (Test-ServiceRunning $Svc) { return }
    if ($env:WA_TEST_FAULT -eq 'start-noop') { return }   # test: "started" but nothing runs
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    $psArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Supervisor`" -Kind $Svc"
    Start-Process -FilePath $WinPS -ArgumentList $psArgs -WindowStyle Hidden | Out-Null
    if (-not (Wait-Until { Test-ServiceRunning $Svc } 10)) { throw "could not start $Svc" }
}

# One health check over TLS that trusts only the connector's own certificate
# (verify.py health), so a program squatting on the port cannot pass it.
function Test-Health {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Py $Verify health 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    finally { $ErrorActionPreference = $old }
}

# Restart the connector and wait until a NEW process serves. Stop first and
# wait for the port to go quiet, so the old process cannot answer the checks.
function Restart-Connector {
    Stop-WaService 'connector'
    if (-not (Wait-Until { -not (Test-Health) } 10)) { throw 'old connector is still answering after stop' }
    Start-WaService 'connector'
    if ((Invoke-Native $Py @($Verify, 'wait-healthy', '--timeout', [string](Get-RestartTimeout))) -ne 0) {
        throw 'restarted connector is not healthy'
    }
}
