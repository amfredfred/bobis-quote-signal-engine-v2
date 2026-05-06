# install_service.ps1 — Run as Administrator
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_service.ps1
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 uninstall

param(
    [ValidateSet("install","uninstall")]
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"

$ServiceName = "BobiFXSignalEngineV2"
$EngineDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppExe      = Join-Path $EngineDir ".venv\Scripts\signal-engine.exe"
$NssmExe     = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
$LogDir      = Join-Path $EngineDir "logs"

# ── Ensure NSSM exists ────────────────────────────────────────────────────────
if (-not (Test-Path $NssmExe)) {
    Write-Host "Downloading NSSM..."
    $zip = Join-Path $EngineDir "nssm.zip"

    Invoke-WebRequest `
        -Uri "https://nssm.cc/release/nssm-2.24.zip" `
        -OutFile $zip

    Expand-Archive $zip -DestinationPath (Join-Path $EngineDir "nssm") -Force
    Remove-Item $zip -Force
}

# ── SAFE SERVICE STOP ────────────────────────────────────────────────────────
function Stop-ServiceSafe {
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue

    if ($svc -and $svc.Status -eq "Running") {
        Write-Host "Stopping service..."
        & $NssmExe stop $ServiceName confirm | Out-Null
    }

    # wait until fully stopped
    for ($i = 0; $i -lt 10; $i++) {
        $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
        if (-not $svc -or $svc.Status -eq "Stopped") {
            return
        }
        Start-Sleep 1
    }
}

# ── SAFE REMOVE ───────────────────────────────────────────────────────────────
function Remove-ServiceSafe {
    Stop-ServiceSafe

    Write-Host "Removing service..."

    & $NssmExe remove $ServiceName confirm 2>$null | Out-Null
    sc.exe delete $ServiceName 2>$null | Out-Null

    Start-Sleep 2
}

# ── PROCESS CLEANUP (SAFE SCOPED) ────────────────────────────────────────────
function Cleanup-Orphans {
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and (
            $_.Path -like "*signal-engine*" -or
            $_.ProcessName -eq "nssm"
        )
    } | Stop-Process -Force -ErrorAction SilentlyContinue
}

# ── UNINSTALL ────────────────────────────────────────────────────────────────
if ($Action -eq "uninstall") {
    Remove-ServiceSafe
    Cleanup-Orphans
    Write-Host "Uninstall complete."
    exit 0
}

# ── VALIDATION ───────────────────────────────────────────────────────────────
if (-not (Test-Path $AppExe)) {
    Write-Error "Executable not found: $AppExe"
    Write-Error "Run: .venv\Scripts\pip install -e ."
    exit 1
}

Remove-ServiceSafe

# ── INSTALL ──────────────────────────────────────────────────────────────────
Write-Host "Installing service..."
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

& $NssmExe install $ServiceName $AppExe

# Working directory
& $NssmExe set $ServiceName AppDirectory $EngineDir

# Logging
& $NssmExe set $ServiceName AppStdout (Join-Path $LogDir "stdout.log")
& $NssmExe set $ServiceName AppStderr (Join-Path $LogDir "stderr.log")
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 10485760

# Stop behavior
& $NssmExe set $ServiceName AppStopMethodConsole 15000
& $NssmExe set $ServiceName AppStopMethodWindow  15000
& $NssmExe set $ServiceName AppStopMethodThreads 15000

# Restart behavior
& $NssmExe set $ServiceName AppThrottle 5000
& $NssmExe set $ServiceName AppExit Default Restart

# Windows service config
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName DisplayName "BobiFX Signal Engine"
& $NssmExe set $ServiceName Description "Real-time forex signal engine (HTF zones + WebSocket broadcast)"

# Recovery policy (bounded, not infinite loop)
sc.exe failure $ServiceName reset= 300 actions= restart/5000/restart/15000/""/0 | Out-Null

# ── START ────────────────────────────────────────────────────────────────────
Write-Host "Starting service..."

& $NssmExe start $ServiceName

Start-Sleep 3

& $NssmExe status $ServiceName

Write-Host ""
Write-Host "Logs:"
Write-Host "  Get-Content '$LogDir\stderr.log' -Tail 50 -Wait"