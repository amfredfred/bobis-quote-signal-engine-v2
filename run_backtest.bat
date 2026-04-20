@echo off
mkdir results\2023 2>nul

start /b py -m src.app.backtesting.backtest --symbol XAUUSD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/XAUUSD.csv
start /b py -m src.app.backtesting.backtest --symbol EURUSD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/EURUSD.csv
start /b py -m src.app.backtesting.backtest --symbol GBPUSD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/GBPUSD.csv
start /b py -m src.app.backtesting.backtest --symbol USDJPY  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/USDJPY.csv
start /b py -m src.app.backtesting.backtest --symbol USDCHF  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/USDCHF.csv
start /b py -m src.app.backtesting.backtest --symbol AUDUSD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/AUDUSD.csv
start /b py -m src.app.backtesting.backtest --symbol USDCAD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/USDCAD.csv
start /b py -m src.app.backtesting.backtest --symbol NZDUSD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/NZDUSD.csv
start /b py -m src.app.backtesting.backtest --symbol US500   --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/US500.csv
start /b py -m src.app.backtesting.backtest --symbol US30    --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/US30.csv
start /b py -m src.app.backtesting.backtest --symbol US100   --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/US100.csv
start /b py -m src.app.backtesting.backtest --symbol BTCUSD  --from-date 2023-01-01 --to-date 2026-04-13 --output results/2023/BTCUSD.csv

echo All 12 backtests launched in background.