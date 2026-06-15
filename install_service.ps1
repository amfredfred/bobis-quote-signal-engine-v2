# install_service.ps1  (DEPRECATED)
#
# The signal engine no longer runs standalone.  It is managed in-process by
# the Signal Manager (signal-engine/manager/).
#
# Use the manager's install script instead:
#   powershell -ExecutionPolicy Bypass -File manager\install.ps1
#   powershell -ExecutionPolicy Bypass -File manager\install.ps1 -Uninstall
#
# This file is kept only to cleanly uninstall the old "AQ Signal Engine"
# scheduled task if it is still registered on this machine.  Running it
# without arguments will remove the old task and print the redirect notice.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$EngineDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManagerInstall = Join-Path $EngineDir "manager\install.ps1"

# ── Remove the old standalone "AQ Signal Engine" task if present ──────────────
$OldTaskName   = "AQ Signal Engine"
$OldTaskFolder = "\Apex Quantel\"
$OldTask = Get-ScheduledTask -TaskName $OldTaskName -TaskPath $OldTaskFolder -ErrorAction SilentlyContinue
if ($OldTask) {
    Write-Host "Removing deprecated standalone task '$OldTaskFolder$OldTaskName'..." -ForegroundColor DarkYellow
    Stop-ScheduledTask  -TaskName $OldTaskName -TaskPath $OldTaskFolder -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $OldTaskName -TaskPath $OldTaskFolder -Confirm:$false
    Write-Host "  Removed." -ForegroundColor Green
} else {
    Write-Host "Old 'AQ Signal Engine' task not found — nothing to remove."
}

# ── Remove legacy NSSM service if still present ───────────────────────────────
$OldService = "BobiFXSignalEngineV2"
$NssmExe    = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
if (Get-Service $OldService -ErrorAction SilentlyContinue) {
    Write-Host "Removing legacy NSSM service ($OldService)..." -ForegroundColor DarkYellow
    if (Test-Path -LiteralPath $NssmExe) {
        try { & $NssmExe stop   $OldService confirm 2>$null | Out-Null } catch {}
        try { & $NssmExe remove $OldService confirm 2>$null | Out-Null } catch {}
    }
    try { sc.exe delete $OldService 2>$null | Out-Null } catch {}
}

Write-Host ""
Write-Host "The signal engine is now managed in-process by the Signal Manager." -ForegroundColor Cyan
Write-Host "Use the manager install script to register the service:" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File manager\install.ps1" -ForegroundColor White
Write-Host ""

if (-not $Uninstall -and (Test-Path $ManagerInstall)) {
    $run = Read-Host "Run manager\install.ps1 now? [Y/n]"
    if ($run -eq "" -or $run -match "^[Yy]") {
        & powershell -ExecutionPolicy Bypass -File $ManagerInstall
    }
}
