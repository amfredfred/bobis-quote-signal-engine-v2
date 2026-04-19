@echo off
REM run_parallel.bat - Parallel backtesting runner with logging

setlocal enabledelayedexpansion

set PYTHONIOENCODING=utf-8

REM Set UTF-8 encoding for console
chcp 65001 >nul

echo.
echo ========================================
echo PARALLEL BACKTESTING LAUNCHER
echo ========================================
echo.

set FROM_DATE=2025-01-01
set TO_DATE=2026-04-16
set OUTPUT_DIR=results\fbs-2025-h1-10min-5min
set LOG_DIR=logs\backtests\%OUTPUT_DIR%\

mkdir %OUTPUT_DIR% 2>nul
mkdir %LOG_DIR% 2>nul

echo Configuration:
echo   Date Range: %FROM_DATE% to %TO_DATE%
echo   Output Dir: %OUTPUT_DIR%
echo   Log Dir: %LOG_DIR%
echo.

set /a LAUNCHED=0

echo Launching backtests in parallel...
echo.

REM Launch all 12 backtests
for %%S in (XAUUSD EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD US500 US30 US100 BTCUSD) do (
    set /a LAUNCHED+=1
    
    set SYMBOL=%%S
    set LOG_FILE=%LOG_DIR%\!SYMBOL!.log
    set OUTPUT_FILE=%OUTPUT_DIR%\!SYMBOL!.csv
    
    echo [!LAUNCHED!/12] Launching !SYMBOL!...
    
    start "!SYMBOL!" /B cmd /c ^
        python -m src.app.backtesting.backtest ^
            --symbol !SYMBOL! ^
            --from-date %FROM_DATE% ^
            --to-date %TO_DATE% ^
            --output !OUTPUT_FILE! ^
            1>>!LOG_FILE! 2>&1
)

echo.
echo [OK] All 12 backtests launched!
echo [*] Monitoring progress...
echo.

REM Wait for all python processes to complete
:wait_loop
cls
echo.
echo ========================================
echo BACKTESTING IN PROGRESS
echo ========================================
echo.

set /a RUNNING=0
for /f %%A in ('tasklist ^| find /c "python.exe"') do set /a RUNNING=%%A

if %RUNNING% gtr 0 (
    echo [*] Active processes: %RUNNING%
    echo [*] Check log files in: %LOG_DIR%
    echo.
    echo Results will be saved to: %OUTPUT_DIR%
    echo.
    echo Press Ctrl+C to stop monitoring...
    timeout /t 10 /nobreak
    goto wait_loop
)

echo.
echo ========================================
echo ALL BACKTESTS COMPLETE!
echo ========================================
echo.
echo Results saved to: %OUTPUT_DIR%
echo Logs saved to: %LOG_DIR%
echo.

REM List completed files
echo Completed backtests:
for %%F in (%OUTPUT_DIR%\*.csv) do (
    echo   [OK] %%~nF
)

echo.
pause
endlocal@echo off
REM run_parallel.bat - Parallel backtesting runner with logging

setlocal enabledelayedexpansion

REM Set UTF-8 encoding for console
chcp 65001 >nul

echo.
echo ========================================
echo PARALLEL BACKTESTING LAUNCHER
echo ========================================
echo.

set FROM_DATE=2025-01-01
set TO_DATE=2026-04-16
set OUTPUT_DIR=results\results\fbs-2025
set LOG_DIR=logs\backtests

mkdir %OUTPUT_DIR% 2>nul
mkdir %LOG_DIR% 2>nul

echo Configuration:
echo   Date Range: %FROM_DATE% to %TO_DATE%
echo   Output Dir: %OUTPUT_DIR%
echo   Log Dir: %LOG_DIR%
echo.

set /a LAUNCHED=0

echo Launching backtests in parallel...
echo.

REM Launch all 12 backtests
for %%S in (XAUUSD EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD US500 US30 US100 BTCUSD) do (
    set /a LAUNCHED+=1
    
    set SYMBOL=%%S
    set LOG_FILE=%LOG_DIR%\!SYMBOL!.log
    set OUTPUT_FILE=%OUTPUT_DIR%\!SYMBOL!.csv
    
    echo [!LAUNCHED!/12] Launching !SYMBOL!...
    
    start "!SYMBOL!" /B cmd /c ^
        python -m src.app.backtesting.backtest ^
            --symbol !SYMBOL! ^
            --from-date %FROM_DATE% ^
            --to-date %TO_DATE% ^
            --output !OUTPUT_FILE! ^
            1>>!LOG_FILE! 2>&1
)

echo.
echo [OK] All 12 backtests launched!
echo [*] Monitoring progress...
echo.

REM Wait for all python processes to complete
:wait_loop
cls
echo.
echo ========================================
echo BACKTESTING IN PROGRESS
echo ========================================
echo.

set /a RUNNING=0
for /f %%A in ('tasklist ^| find /c "python.exe"') do set /a RUNNING=%%A

if %RUNNING% gtr 0 (
    echo [*] Active processes: %RUNNING%
    echo [*] Check log files in: %LOG_DIR%
    echo.
    echo Results will be saved to: %OUTPUT_DIR%
    echo.
    echo Press Ctrl+C to stop monitoring...
    timeout /t 10 /nobreak
    goto wait_loop
)

echo.
echo ========================================
echo ALL BACKTESTS COMPLETE!
echo ========================================
echo.
echo Results saved to: %OUTPUT_DIR%
echo Logs saved to: %LOG_DIR%
echo.

REM List completed files
echo Completed backtests:
for %%F in (%OUTPUT_DIR%\*.csv) do (
    echo   [OK] %%~nF
)

echo.
pause
endlocal