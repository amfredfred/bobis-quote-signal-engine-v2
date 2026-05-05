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
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath (Join-Path $EngineDir "nssm") -Force
    Remove-Item $zip
}

# ── Remove existing service (robust) ──────────────────────────────────────────
function Remove-ExistingService {
    $exists = sc.exe query $ServiceName 2>$null

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Stopping and removing $ServiceName..."

        # Stop service
        & $NssmExe stop $ServiceName confirm 2>$null | Out-Null
        Start-Sleep -Seconds 2

        # Remove service
        & $NssmExe remove $ServiceName confirm 2>$null | Out-Null
        Start-Sleep -Seconds 2

        # Fallback hard delete (handles NSSM failures)
        sc.exe delete $ServiceName 2>$null | Out-Null

        Write-Host "$ServiceName removed."
    } else {
        Write-Host "$ServiceName not installed."
    }

    # Kill orphan processes (critical)
    Get-Process | Where-Object {
        $_.Path -like "*signal-engine*" -or $_.Path -like "*mt5*" -or $_.ProcessName -like "nssm*"
    } | Stop-Process -Force -ErrorAction SilentlyContinue
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
if ($Action -eq "uninstall") {
    Remove-ExistingService
    exit 0
}

# ── Validate install ──────────────────────────────────────────────────────────
if (-not (Test-Path $AppExe)) {
    Write-Error "Executable not found: $AppExe`nRun: .venv\Scripts\pip install -e ."
    exit 1
}

Remove-ExistingService

# ── Install ───────────────────────────────────────────────────────────────────
Write-Host "Installing $ServiceName..."
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

& $NssmExe install $ServiceName $AppExe

# Working directory
& $NssmExe set $ServiceName AppDirectory $EngineDir

# Logging
& $NssmExe set $ServiceName AppStdout "$LogDir\stdout.log"
& $NssmExe set $ServiceName AppStderr "$LogDir\stderr.log"
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 10485760

# Graceful shutdown (prevents orphan python processes)
& $NssmExe set $ServiceName AppStopMethodConsole 15000
& $NssmExe set $ServiceName AppStopMethodWindow  15000
& $NssmExe set $ServiceName AppStopMethodThreads 15000

# Controlled restart behavior (avoid infinite loops)
& $NssmExe set $ServiceName AppThrottle 5000
& $NssmExe set $ServiceName AppExit Default Restart

# Metadata
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName DisplayName "BobiFX Signal Engine"
& $NssmExe set $ServiceName Description "Real-time forex signal engine (HTF zones + WebSocket broadcast)"

# Bounded Windows recovery (NOT infinite)
sc.exe failure $ServiceName reset= 300 actions= restart/5000/restart/15000/""/0 | Out-Null

# ── Start ─────────────────────────────────────────────────────────────────────
Write-Host "Starting $ServiceName..."
& $NssmExe start $ServiceName

Start-Sleep -Seconds 3
& $NssmExe status $ServiceName

Write-Host ""
Write-Host "Logs:"
Write-Host "  Get-Content '$LogDir\stderr.log' -Tail 50 -Wait"