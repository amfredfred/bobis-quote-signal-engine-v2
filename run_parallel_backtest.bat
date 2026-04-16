@echo off
REM run_parallel_all.bat - Run all 10 symbols in parallel (same window, background)

echo Starting 10 parallel backtests in background...
echo.

start /B python -m src.app.backtesting.backtest --symbol XAUUSD --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/XAUUSD.csv
start /B python -m src.app.backtesting.backtest --symbol EURUSD --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/EURUSD.csv
start /B python -m src.app.backtesting.backtest --symbol GBPUSD --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/GBPUSD.csv
start /B python -m src.app.backtesting.backtest --symbol USDJPY --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/USDJPY.csv
start /B python -m src.app.backtesting.backtest --symbol USDCHF --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/USDCHF.csv
start /B python -m src.app.backtesting.backtest --symbol AUDUSD --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/AUDUSD.csv
start /B python -m src.app.backtesting.backtest --symbol USDCAD --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/USDCAD.csv
start /B python -m src.app.backtesting.backtest --symbol NZDUSD --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/NZDUSD.csv
start /B python -m src.app.backtesting.backtest --symbol EURJPY --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/EURJPY.csv
start /B python -m src.app.backtesting.backtest --symbol US500 --from-date 2025-01-01  --to-date 2026-04-09 --output results/fbs-pattern-all/US500.csv

echo 10 processes launched in background!
echo Estimated time: 18-20 hours
echo.
pause