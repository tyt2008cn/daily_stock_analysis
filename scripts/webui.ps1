<#
.SYNOPSIS
    Control script for the DSA WebUI + daily-schedule daemon.

.DESCRIPTION
    Runs the app detached as a long-lived background process:

        python main.py --serve        (with SCHEDULE_ENABLED=true)

    With SCHEDULE_ENABLED=true, main.py takes the
    "Web/API runtime scheduler" branch: the WebUI and the daily analysis both
    live in ONE process, and no one-off analysis is triggered on startup.

    Notes / gotchas handled here:
      * On Windows, .venv\Scripts\python.exe is a launcher shim that spawns the
        real interpreter as a CHILD process. Killing only the launcher leaves the
        server alive, so stop uses a process-TREE kill (taskkill /T).
      * The process is started detached, so closing the terminal does not stop it.
      * A PID file plus a port-owner probe means stop/status also work for an
        instance that was started manually outside this script.
      * Safety: before killing a port owner we verify its command line actually
        references main.py, so an unrelated process holding the port is never killed.

.PARAMETER Action
    start | stop | restart | status | logs

.PARAMETER Port
    Override the listen port. Defaults to WEBUI_PORT in .env, else 8000.

.EXAMPLE
    .\scripts\webui.ps1 start
    .\scripts\webui.ps1 status
    .\scripts\webui.ps1 stop
    .\scripts\webui.ps1 logs
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'help')]
    [string]$Action = 'status',

    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$LogsDir = Join-Path $Root 'logs'
$PidFile = Join-Path $LogsDir 'webui.pid'
$OutLog = Join-Path $LogsDir 'webui.out.log'
$ErrLog = Join-Path $LogsDir 'webui.err.log'
$ControlLog = Join-Path $LogsDir 'webui.control.log'
$EnvFile = Join-Path $Root '.env'
$HealthTimeoutSec = 60

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
function Get-DotEnvValue {
    param([string]$Key)
    if (-not (Test-Path $EnvFile)) { return $null }
    $m = Select-String -Path $EnvFile -Pattern "^$Key=(.*)$" -ErrorAction SilentlyContinue |
        Select-Object -Last 1
    if ($null -eq $m) { return $null }
    return $m.Matches[0].Groups[1].Value.Trim()
}

function Resolve-Python {
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python not found. Expected .venv\Scripts\python.exe under $Root"
}

function Resolve-Port {
    if ($Port -gt 0) { return $Port }
    $p = Get-DotEnvValue 'WEBUI_PORT'
    if ($p -and $p -match '^\d+$') { return [int]$p }
    return 8000
}

function Resolve-Host {
    $h = Get-DotEnvValue 'WEBUI_HOST'
    if ($h) { return $h }
    return '127.0.0.1'
}

function Get-PortOwnerPid {
    param([int]$Port)
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $conn) { return $null }
    return [int]$conn.OwningProcess
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $p) { return '' }
    return [string]$p.CommandLine
}

function Test-IsOurApp {
    param([int]$ProcessId)
    $cmd = Get-ProcessCommandLine -ProcessId $ProcessId
    return ($cmd -match 'main\.py')
}

function Get-HealthUrl {
    param([int]$Port, [string]$HostName)
    return "http://${HostName}:${Port}/api/health"
}

function Test-Health {
    param([int]$Port, [string]$HostName)
    try {
        $r = Invoke-WebRequest -Uri (Get-HealthUrl -Port $Port -HostName $HostName) `
            -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-TrackedPid {
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($raw -and $raw -match '^\d+$') { return [int]$raw }
    return $null
}

function Test-PidAlive {
    param($ProcessId)
    if ($null -eq $ProcessId) { return $false }
    return ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue))
}

function Write-Banner {
    param([string]$Text)
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

# Append an audit line for every start/stop attempt.
# This exists so the logon scheduled task (which cannot easily redirect output
# without fragile nested quoting) still leaves a diagnosable trace.
function Write-ControlLog {
    param([string]$Message)
    try {
        if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }
        $line = '{0} | pid={1} | {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $Message
        Add-Content -Path $ControlLog -Value $line -Encoding utf8
    } catch {
        # logging must never break control flow
    }
}

# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
function Invoke-Status {
    $listenPort = Resolve-Port
    $listenHost = Resolve-Host
    Write-Banner 'DSA WebUI status'

    $tracked = Get-TrackedPid
    $trackedAlive = Test-PidAlive $tracked
    $owner = Get-PortOwnerPid -Port $listenPort
    $healthy = Test-Health -Port $listenPort -HostName $listenHost

    Write-Host ("  Repo          : {0}" -f $Root)
    Write-Host ("  Endpoint      : {0}" -f (Get-HealthUrl -Port $listenPort -HostName $listenHost))

    if ($tracked) {
        $mark = if ($trackedAlive) { 'running' } else { 'STALE (process gone)' }
        Write-Host ("  PID file      : {0} -> {1}" -f $tracked, $mark)
    } else {
        Write-Host '  PID file      : none'
    }

    if ($owner) {
        $isOurs = Test-IsOurApp -ProcessId $owner
        $who = if ($isOurs) { 'DSA (main.py)' } else { 'NOT DSA - a different process' }
        Write-Host ("  Port {0,-8}: held by PID {1} ({2})" -f $listenPort, $owner, $who)
    } else {
        Write-Host ("  Port {0,-8}: free" -f $listenPort)
    }

    if ($healthy) {
        Write-Host '  Health        : OK' -ForegroundColor Green
        Write-Host ''
        Write-Host ("  WebUI         : http://{0}:{1}" -f $listenHost, $listenPort) -ForegroundColor Green
    } elseif ($owner) {
        Write-Host '  Health        : no response yet (starting up, or hung)' -ForegroundColor Yellow
    } else {
        Write-Host '  Health        : stopped' -ForegroundColor DarkGray
    }
    Write-Host ''
}

function Invoke-Start {
    $listenPort = Resolve-Port
    $listenHost = Resolve-Host

    if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }

    Write-Banner "Starting DSA WebUI on $listenHost`:$listenPort"
    Write-ControlLog "start requested (port=$listenPort)"

    $owner = Get-PortOwnerPid -Port $listenPort
    if ($owner) {
        $isOurs = Test-IsOurApp -ProcessId $owner
        if ($isOurs) {
            Write-Host ("  Already running: PID {0} is serving port {1}." -f $owner, $listenPort) -ForegroundColor Yellow
            Write-Host '  Use "restart" to replace it.' -ForegroundColor Yellow
            Write-ControlLog "start skipped: already running (listener pid=$owner)"
        } else {
            Write-Host ("  Port {0} is held by PID {1}, which is NOT this app:" -f $listenPort, $owner) -ForegroundColor Red
            Write-Host ("    {0}" -f (Get-ProcessCommandLine -ProcessId $owner)) -ForegroundColor DarkGray
            Write-Host '  Refusing to start. Free the port first.' -ForegroundColor Red
            Write-ControlLog "start REFUSED: port $listenPort held by non-DSA pid=$owner"
        }
        Write-Host ''
        return 1
    }

    $tracked = Get-TrackedPid
    if ($tracked -and -not (Test-PidAlive $tracked)) {
        Write-Host ("  Removing stale PID file ({0})." -f $tracked) -ForegroundColor DarkGray
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }

    # --serve + SCHEDULE_ENABLED=true => "Web/API runtime scheduler":
    # WebUI and the daily job share one process and no one-off analysis runs on boot.
    $scheduleEnabled = Get-DotEnvValue 'SCHEDULE_ENABLED'
    if ($scheduleEnabled -ne 'true') {
        Write-Host '  WARNING: SCHEDULE_ENABLED is not true.' -ForegroundColor Yellow
        Write-Host '           --serve will then run a ONE-OFF analysis on every start' -ForegroundColor Yellow
        Write-Host '           instead of the persistent runtime scheduler.' -ForegroundColor Yellow
        Write-Host ''
    }

    $python = Resolve-Python
    # Always pass --port/--host explicitly so the script and the app can never
    # drift apart (the app would otherwise read WEBUI_PORT from .env and ignore
    # the -Port override used for the health check).
    $appArgs = @('main.py', '--serve', '--port', "$listenPort", '--host', $listenHost)
    Write-Host ("  Python        : {0}" -f $python)
    Write-Host ("  Command       : {0}" -f ($appArgs -join ' '))
    Write-Host ("  Stdout log    : {0}" -f $OutLog)
    Write-Host ("  Stderr log    : {0}" -f $ErrLog)

    # Force UTF-8 in the child. Without this, Windows uses the legacy code page
    # (CP936 here) and every Chinese log line lands in the redirected file as
    # mojibake. docs/full-guide.md recommends the same for manual runs.
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $proc = Start-Process -FilePath $python `
        -ArgumentList $appArgs `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru

    Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
    Write-Host ("  Launched      : launcher PID {0}" -f $proc.Id)

    Write-Host '  Waiting for health check ...' -NoNewline
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    $ok = $false
    $currentOwner = $null
    while ((Get-Date) -lt $deadline) {
        if (Test-Health -Port $listenPort -HostName $listenHost) { $ok = $true; break }
        Start-Sleep -Milliseconds 800
        Write-Host '.' -NoNewline
    }
    Write-Host ''

    if ($ok) {
        $currentOwner = Get-PortOwnerPid -Port $listenPort
        Write-Host ("  Health        : OK (listening PID {0})" -f $currentOwner) -ForegroundColor Green
        Write-Host ("  WebUI         : http://{0}:{1}" -f $listenHost, $listenPort) -ForegroundColor Green
        Write-ControlLog "start OK: launcher=$($proc.Id) listener=$currentOwner port=$listenPort"
    } else {
        Write-Host '  Health        : FAILED to respond in time' -ForegroundColor Red
        Write-ControlLog "start FAILED: health check timeout (port=$listenPort launcher=$($proc.Id))"
        Write-Host '  Last stderr lines:' -ForegroundColor DarkGray
        if (Test-Path $ErrLog) {
            Get-Content $ErrLog -Tail 15 | ForEach-Object { Write-Host ("    {0}" -f $_) -ForegroundColor DarkGray }
        }
        Write-Host '  Last stdout lines:' -ForegroundColor DarkGray
        if (Test-Path $OutLog) {
            Get-Content $OutLog -Tail 15 | ForEach-Object { Write-Host ("    {0}" -f $_) -ForegroundColor DarkGray }
        }
        return 1
    }
    Write-Host ''
    return 0
}

function Invoke-Stop {
    $listenPort = Resolve-Port
    Write-Banner "Stopping DSA WebUI"
    Write-ControlLog "stop requested (port=$listenPort)"

    $targets = @()

    $tracked = Get-TrackedPid
    if ($tracked -and (Test-PidAlive $tracked)) { $targets += $tracked }

    $owner = Get-PortOwnerPid -Port $listenPort
    if ($owner) {
        if (-not (Test-IsOurApp -ProcessId $owner)) {
            Write-Host ("  Port {0} is held by PID {1}, which is NOT this app - refusing to kill it." -f $listenPort, $owner) -ForegroundColor Red
            Write-Host ("    {0}" -f (Get-ProcessCommandLine -ProcessId $owner)) -ForegroundColor DarkGray
            Write-Host ''
            Write-ControlLog "stop REFUSED: port $listenPort held by non-DSA pid=$owner"
            return 1
        }
        if ($targets -notcontains $owner) { $targets += $owner }
    }

    if ($targets.Count -eq 0) {
        Write-Host '  Not running.' -ForegroundColor DarkGray
        Write-ControlLog 'stop: not running'
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host ''
        return 0
    }

    foreach ($t in $targets) {
        Write-Host ("  Killing process tree of PID {0} ..." -f $t)
        # /T is required: the venv python.exe launcher spawns the real interpreter.
        & taskkill.exe /PID $t /T /F 2>&1 | ForEach-Object { Write-Host ("    {0}" -f $_) -ForegroundColor DarkGray }
    }

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-PortOwnerPid -Port $listenPort)) { break }
        Start-Sleep -Milliseconds 500
    }

    if (Get-PortOwnerPid -Port $listenPort) {
        Write-Host ("  Port {0} still held after kill." -f $listenPort) -ForegroundColor Red
        Write-ControlLog "stop FAILED: port $listenPort still held"
        return 1
    }

    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host '  Stopped; port released.' -ForegroundColor Green
    Write-ControlLog "stop OK: killed tree(s) $($targets -join ',') port=$listenPort"
    Write-Host ''
    return 0
}

function Invoke-Logs {
    Write-Banner 'Tailing logs (Ctrl+C to exit)'
    Write-Host ("  {0}" -f $OutLog) -ForegroundColor DarkGray
    Write-Host ("  {0}" -f $ErrLog) -ForegroundColor DarkGray
    Write-Host ''
    $existing = @()
    if (Test-Path $OutLog) { $existing += $OutLog }
    if (Test-Path $ErrLog) { $existing += $ErrLog }
    if ($existing.Count -eq 0) {
        Write-Host '  No log files yet.' -ForegroundColor Yellow
        return 0
    }
    Get-Content -Path $existing -Tail 40 -Wait
    return 0
}

function Show-Help {
    Write-Host ''
    Write-Host 'DSA WebUI control' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  .\scripts\webui.ps1 start     Start detached (WebUI + daily schedule)'
    Write-Host '  .\scripts\webui.ps1 stop      Stop (process-tree kill)'
    Write-Host '  .\scripts\webui.ps1 restart   Stop then start'
    Write-Host '  .\scripts\webui.ps1 status    Show PID / port / health'
    Write-Host '  .\scripts\webui.ps1 logs      Tail stdout+stderr logs'
    Write-Host ''
    Write-Host '  -Port <n>   Override listen port (default: WEBUI_PORT in .env, else 8000)'
    Write-Host ''
    return 0
}

switch ($Action) {
    'start' { exit (Invoke-Start) }
    'stop' { exit (Invoke-Stop) }
    'restart' { Invoke-Stop | Out-Null; exit (Invoke-Start) }
    'status' { Invoke-Status; exit 0 }
    'logs' { exit (Invoke-Logs) }
    default { exit (Show-Help) }
}
