# Keeps one service running, restarting it if it exits (like launchd KeepAlive).
# Started hidden by Start-WaService; stopped by killing this process first.
#   supervise.ps1 -Kind bridge|connector|tunnel
param([Parameter(Mandatory = $true)][ValidateSet('bridge', 'connector', 'tunnel')][string]$Kind)
. "$PSScriptRoot\common.ps1"
$ErrorActionPreference = 'Continue'   # a failing child must never end the loop

New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$log = Join-Path $Logs "$Kind.log"
$delay = [int](Get-Setting 'WA_RESTART_DELAY' '10')

switch ($Kind) {
    'connector' {
        $workdir = Join-Path $Repo 'connector'
        $exe = $Py
        $argline = "`"$ServerPy`" --transport http --log-file `"$(Join-Path $Logs 'connector.log')`""
        $log = Join-Path $Logs 'connector-console.log'
    }
    'bridge' {
        $workdir = $BridgeDir   # the bridge uses the relative path store/
        $exe = $BridgeExe
        $argline = ''
    }
    'tunnel' {
        $workdir = $HOME
        $cf = Get-Command cloudflared -ErrorAction SilentlyContinue
        if ($cf) { $exe = $cf.Path } else { $exe = $null }
        $argline = "tunnel --config `"$TunnelConfig`" run"
    }
}

while ($true) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
        Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) supervisor: $Kind executable not found ($exe); retrying in ${delay}s"
    } else {
        # cmd.exe does the output redirection (append), which Windows
        # PowerShell 5.1 cannot do cleanly for native programs.
        $inner = "`"`"$exe`" $argline >> `"$log`" 2>&1`""
        $p = Start-Process -FilePath 'cmd.exe' -ArgumentList "/d /s /c $inner" -WorkingDirectory $workdir `
             -NoNewWindow -Wait -PassThru
        Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) supervisor: $Kind exited with code $($p.ExitCode); restarting in ${delay}s"
    }
    Start-Sleep -Seconds $delay
}
