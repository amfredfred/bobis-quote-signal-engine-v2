<#
.SYNOPSIS
    Install Signal Manager as a Windows Scheduled Task.

.DESCRIPTION
    Registers a task that starts the Signal Manager automatically at logon and
    restarts it if it crashes.  Output is appended to manager/logs/manager.log.
    Must be run from an elevated (Administrator) PowerShell prompt.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>

param(
    [switch]$Uninstall
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Auto-elevate if not already admin ─────────────────────────────────────────

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Relaunching as Administrator..."
    Start-Process powershell -Verb RunAs `
        -ArgumentList "-ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    exit
}

# ── Uninstall ─────────────────────────────────────────────────────────────────

$TaskName = "TradeRelay-SignalManager"

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Task '$TaskName' is not installed."
    } else {
        Stop-ScheduledTask  -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Uninstalled: $TaskName"
    }
    exit
}

# ── Paths ─────────────────────────────────────────────────────────────────────

$ManagerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineRoot = (Resolve-Path "$ManagerDir\..").Path
$Python     = "$EngineRoot\venv\Scripts\python.exe"
$LogDir     = "$ManagerDir\logs"
$LogFile    = "$LogDir\manager.log"

# ── Preflight ─────────────────────────────────────────────────────────────────

if (-not (Test-Path $Python)) {
    Write-Error "venv Python not found at:`n  $Python`nCreate the venv first: python -m venv venv && venv\Scripts\pip install -e ."
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── Build the scheduled task ──────────────────────────────────────────────────

# powershell -Command wraps the launch so we can redirect all output to a log
# file and set PYTHONUNBUFFERED so log lines are not held in Python's buffer.
$Cmd = "Set-Location '$ManagerDir'; " +
       "`$env:PYTHONUNBUFFERED = '1'; " +
       "& '$Python' -m src *>> '$LogFile'"

$Action = New-ScheduledTaskAction `
    -Execute        "powershell.exe" `
    -Argument       "-NonInteractive -WindowStyle Hidden -Command `"$Cmd`"" `
    -WorkingDirectory $ManagerDir

# Trigger at logon for the installing user (MT5 needs an interactive session).
$Trigger = New-ScheduledTaskTrigger -AtLogon -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  ([System.TimeSpan]::Zero) `
    -RestartCount        10 `
    -RestartInterval     (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances   IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Highest

# ── Register (replace if already present) ────────────────────────────────────

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Desc = "TradeRelay Signal Manager - aggregates MT5 broker signals and routes them to the execution gateway."

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -Principal   $Principal `
    -Description $Desc `
    | Out-Null

# ── Summary ───────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "Installed: $TaskName"
Write-Host "  Engine : $EngineRoot"
Write-Host "  Python : $Python"
Write-Host "  Log    : $LogFile"
Write-Host "  Runs   : at logon for $env:USERNAME, restarts on crash (up to 10x, 1 min delay)"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start now  : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Stop       : Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Host "  Status     : (Get-ScheduledTask  -TaskName '$TaskName').State"
Write-Host "  Tail log   : Get-Content '$LogFile' -Wait -Tail 50"
Write-Host "  Uninstall  : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host ""

# Offer to start immediately
$start = Read-Host "Start the task now? [Y/n]"
if ($start -eq "" -or $start -match "^[Yy]") {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Task state: $state"
}
