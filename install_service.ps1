# install_service.ps1
#
# Registers the Signal Engine as a Windows Task Scheduler task that runs
# under your user account at logon. This replaces the old NSSM service.
#
# WHY TASK SCHEDULER INSTEAD OF A SERVICE:
#   Windows services run in Session 0 (no desktop). The Signal Engine uses
#   the MT5 Python API, which cannot attach to a terminal running in the
#   user's session from Session 0. A scheduled task runs as the logged-in
#   user in their own session — MT5 is fully visible and accessible.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_service.ps1
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 uninstall
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 update
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 -VenvName .venv

param(
    [ValidateSet("install", "uninstall", "update")]
    [string]$Action = "install",

    [string]$VenvName = "venv"
)

$ErrorActionPreference = "Stop"

$TaskName    = "AQ Signal Engine"
$TaskFolder  = "\Apex Quantel\"
$Description = "Apex Quantel Signal Engine - real-time forex signal generation and broadcast"
$EngineDir   = Split-Path -Parent $MyInvocation.MyCommand.Path

# Use pythonw.exe (no console window) so the process is fully detached from
# any terminal session and cannot receive CTRL_CLOSE events when a terminal closes.
$AppExe      = Join-Path $EngineDir "$VenvName\Scripts\pythonw.exe"
$AppArg      = "-c `"from interfaces.cli.main import main; main()`""

# ── Helpers ───────────────────────────────────────────────────────────────────
function Stop-Task {
    $t = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($t -and $t.State -eq "Running") {
        Write-Host "  Stopping running task..."
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
        Start-Sleep 3
    }
}

function Remove-Task {
    Stop-Task
    if (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue) {
        Write-Host "  Removing existing task..."
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -Confirm:$false
    }
}

function Remove-OldNssmService {
    # Migrate: remove the old NSSM service if it is still installed
    $OldService = "BobiFXSignalEngineV2"
    $NssmExe    = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
    if (Get-Service $OldService -ErrorAction SilentlyContinue) {
        Write-Host "  Removing legacy NSSM service ($OldService)..." -ForegroundColor DarkYellow
        if (Test-Path -LiteralPath $NssmExe) {
            try { & $NssmExe stop   $OldService confirm 2>$null | Out-Null } catch {}
            try { & $NssmExe remove $OldService confirm 2>$null | Out-Null } catch {}
        }
        try { sc.exe delete $OldService 2>$null | Out-Null } catch {}
        Start-Sleep 2
    }
}

function Cleanup-Orphans {
    $escapedDir = [regex]::Escape($EngineDir)
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine -match $escapedDir -and
        ($_.Name -like "signal-engine*" -or $_.Name -like "python*")
    } | ForEach-Object {
        Write-Host "  Stopping orphan PID $($_.ProcessId): $($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Validate-Exe {
    if (-not (Test-Path -LiteralPath $AppExe)) {
        Write-Host ""
        Write-Host "ERROR: pythonw.exe not found: $AppExe" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Create the venv and install:" -ForegroundColor Yellow
        Write-Host "    py -3.12 -m venv $VenvName" -ForegroundColor Yellow
        Write-Host "    $VenvName\Scripts\pip install -e ." -ForegroundColor Yellow
        exit 1
    }
}

# ── Install ───────────────────────────────────────────────────────────────────
function _install {
    Validate-Exe
    Remove-OldNssmService
    Remove-Task
    Cleanup-Orphans

    Write-Host ""
    Write-Host "  Registering scheduled task..."
    Write-Host "    Task : $TaskFolder$TaskName"
    Write-Host "    Exe  : $AppExe"
    Write-Host "    CWD  : $EngineDir"
    Write-Host "    User : $env:USERDOMAIN\$env:USERNAME"

    # Action: run via pythonw.exe (no console window — fully detached from terminals)
    $action = New-ScheduledTaskAction `
        -Execute          $AppExe `
        -Argument         $AppArg `
        -WorkingDirectory $EngineDir

    # Trigger: at logon for this user, 30-second delay
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = "PT30S"

    # Settings: no execution time limit, restart up to 10x on failure
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances      IgnoreNew `
        -ExecutionTimeLimit     ([TimeSpan]::Zero) `
        -RestartCount           10 `
        -RestartInterval        (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable

    # Principal: the logged-in user, highest available privilege
    $principal = New-ScheduledTaskPrincipal `
        -UserId    "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel  Highest

    Register-ScheduledTask `
        -TaskName    $TaskName `
        -TaskPath    $TaskFolder `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Description $Description `
        -Force | Out-Null

    Write-Host ""
    Write-Host "  Starting task now..."
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder

    Start-Sleep 3
    $state = (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder).State
    Write-Host "  Task state: $state" -ForegroundColor $(if ($state -eq "Running") { "Green" } else { "Yellow" })

    Write-Host ""
    Write-Host "  AQ Signal Engine will start automatically 30 s after each login."
    Write-Host "  Logs: $EngineDir\logs\signal_engine.log"
}

# ── Update ────────────────────────────────────────────────────────────────────
function _update {
    Validate-Exe
    # Full re-register so the exe path is always current
    _install
}

# ── Entry point ───────────────────────────────────────────────────────────────
switch ($Action) {
    "uninstall" {
        Remove-OldNssmService
        Remove-Task
        Cleanup-Orphans
        Write-Host "Uninstall complete."
    }
    "update"  { _update }
    default   { _install }
}
