# install_service.ps1 - Run as Administrator
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_service.ps1
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 uninstall
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 -VenvName .venv

param(
    [ValidateSet("install","uninstall")]
    [string]$Action = "install",

    [string]$VenvName = "venv"
)

$ErrorActionPreference = "Stop"

$ServiceName = "BobiFXSignalEngineV2"
$EngineDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir     = Join-Path $EngineDir $VenvName
$PythonExe   = Join-Path $VenvDir "Scripts\python.exe"
$AppExe      = Join-Path $VenvDir "Scripts\signal-engine.exe"
$NssmExe     = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
$LogDir      = Join-Path $EngineDir "logs"

function Ensure-Nssm {
    if (Test-Path -LiteralPath $NssmExe) {
        return
    }

    $zip = Join-Path $EngineDir "nssm.zip"

    if (-not (Test-Path -LiteralPath $zip)) {
        Write-Host "Downloading NSSM..."
        Invoke-WebRequest `
            -Uri "https://nssm.cc/release/nssm-2.24.zip" `
            -OutFile $zip
    }

    Expand-Archive $zip -DestinationPath (Join-Path $EngineDir "nssm") -Force

    if (-not (Test-Path -LiteralPath $NssmExe)) {
        Write-Error "NSSM executable not found after extraction: $NssmExe"
        exit 1
    }
}

function Stop-ServiceSafe {
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue

    if ($svc -and $svc.Status -eq "Running") {
        Write-Host "Stopping service..."
        & $NssmExe stop $ServiceName confirm | Out-Null
    }

    for ($i = 0; $i -lt 15; $i++) {
        $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
        if (-not $svc -or $svc.Status -eq "Stopped") {
            return
        }
        Start-Sleep 1
    }
}

function Remove-ServiceSafe {
    Stop-ServiceSafe

    if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
        Write-Host "Removing service..."
        & $NssmExe remove $ServiceName confirm 2>$null | Out-Null
        sc.exe delete $ServiceName 2>$null | Out-Null
        Start-Sleep 2
    }
}

function Cleanup-Orphans {
    $escapedEngineDir = [regex]::Escape($EngineDir)
    $escapedServiceName = [regex]::Escape($ServiceName)

    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        (
            $_.CommandLine -match $escapedEngineDir -or
            $_.CommandLine -match $escapedServiceName
        )
    } | ForEach-Object {
        Write-Host "Stopping orphan process PID $($_.ProcessId): $($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Validate-InstallInputs {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Error "Virtual environment Python not found: $PythonExe"
        Write-Error "Create it with: py -3.12 -m venv $VenvName"
        exit 1
    }

    if (-not (Test-Path -LiteralPath $AppExe)) {
        Write-Error "Executable not found: $AppExe"
        Write-Error "Run: $VenvName\Scripts\python.exe -m pip install -e ."
        exit 1
    }
}

Ensure-Nssm

if ($Action -eq "uninstall") {
    Remove-ServiceSafe
    Cleanup-Orphans
    Write-Host "Uninstall complete."
    exit 0
}

Validate-InstallInputs
Remove-ServiceSafe

Write-Host "Installing service..."
Write-Host "  Service: $ServiceName"
Write-Host "  App:     $AppExe"
Write-Host "  CWD:     $EngineDir"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

& $NssmExe install $ServiceName $AppExe

& $NssmExe set $ServiceName AppDirectory $EngineDir
& $NssmExe set $ServiceName AppEnvironmentExtra "PYTHONNOUSERSITE=1" "PYTHONPATH=$EngineDir\src"

& $NssmExe set $ServiceName AppStdout (Join-Path $LogDir "stdout.log")
& $NssmExe set $ServiceName AppStderr (Join-Path $LogDir "stderr.log")
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 10485760

& $NssmExe set $ServiceName AppStopMethodConsole 15000
& $NssmExe set $ServiceName AppStopMethodWindow  15000
& $NssmExe set $ServiceName AppStopMethodThreads 15000

& $NssmExe set $ServiceName AppThrottle 5000
& $NssmExe set $ServiceName AppExit Default Restart

& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName DisplayName "BobiFX Signal Engine"
& $NssmExe set $ServiceName Description "Real-time forex signal engine (HTF zones + WebSocket broadcast)"

sc.exe failure $ServiceName reset= 300 actions= restart/5000/restart/15000/""/0 | Out-Null

Write-Host "Starting service..."
& $NssmExe start $ServiceName

Start-Sleep 3
& $NssmExe status $ServiceName

Write-Host ""
Write-Host "Logs:"
Write-Host "  Get-Content '$LogDir\stderr.log' -Tail 50 -Wait"
