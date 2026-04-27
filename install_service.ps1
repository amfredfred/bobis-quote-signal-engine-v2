# install_service.ps1 - Run as Administrator
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_service.ps1           # install
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 uninstall # uninstall

param(
    [string]$Action = "install"
)

$ServiceName = "BobiFXSignalEngineV2"
$EngineDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptExe   = Join-Path $EngineDir ".venv\Scripts\signal-engine.exe"
$NssmExe     = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
$LogDir      = Join-Path $EngineDir "logs"

function Remove-ExistingService {
    $status = & $NssmExe status $ServiceName 2>$null
    if ($status) {
        Write-Host "Stopping and removing $ServiceName..."
        & $NssmExe stop   $ServiceName confirm 2>$null | Out-Null
        & $NssmExe remove $ServiceName confirm 2>$null | Out-Null
        Start-Sleep -Seconds 2
        Write-Host "$ServiceName removed."
    } else {
        Write-Host "$ServiceName is not installed -- nothing to remove."
    }
}

if ($Action -eq "uninstall") {
    Remove-ExistingService
    exit 0
}

if ($Action -ne "install") {
    Write-Error "Unknown action '$Action'. Use 'install' or 'uninstall'."
    exit 1
}

if (-not (Test-Path $ScriptExe)) {
    Write-Error "signal-engine.exe not found: $ScriptExe`nRun: .venv\Scripts\pip install -e . (from $EngineDir)"
    exit 1
}

if (-not (Test-Path $NssmExe)) {
    Write-Host "Downloading NSSM..."
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile "$EngineDir\nssm.zip"
    Expand-Archive "$EngineDir\nssm.zip" -DestinationPath "$EngineDir\nssm" -Force
    Remove-Item "$EngineDir\nssm.zip"
}

Remove-ExistingService

Write-Host "Installing $ServiceName..."
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

& $NssmExe install $ServiceName $ScriptExe
& $NssmExe set $ServiceName AppDirectory   $EngineDir
& $NssmExe set $ServiceName AppStdout      "$LogDir\service_stdout.log"
& $NssmExe set $ServiceName AppStderr      "$LogDir\service_stderr.log"
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 10485760
& $NssmExe set $ServiceName Start          SERVICE_AUTO_START
& $NssmExe set $ServiceName DisplayName    "BobiFX Signal Engine"
& $NssmExe set $ServiceName Description    "Real-time forex signal engine -- HTF zone detection, WebSocket broadcast."

sc.exe failure $ServiceName reset= 60 actions= restart/5000/restart/10000/restart/30000 | Out-Null

Write-Host "Starting $ServiceName..."
& $NssmExe start $ServiceName
Start-Sleep -Seconds 4
& $NssmExe status $ServiceName

Write-Host ""
Write-Host "Logs: Get-Content '$LogDir\service_stderr.log' -Tail 50 -Wait"