# Commands

This file lists the common commands for installing, configuring, running, testing, backtesting, and managing the Signal Engine.

Run commands from the repository root unless noted otherwise.

## Environment

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

Upgrade pip:

```powershell
python -m pip install -U pip
```

Install the engine:

```powershell
pip install -e .
```

Install with test/development dependencies:

```powershell
pip install -e ".[dev]"
```

## Configuration

Create a local `.env` from the template:

```powershell
Copy-Item .env.example .env
```

Use the default YAML config:

```powershell
$env:APEX_CONFIG = "config.yaml"
```

Equivalent config pointer:

```powershell
$env:USE_CONFIG = "config.yaml"
```

Run in paper mode:

```powershell
$env:APEX_ENV = "paper"
```

Run in live mode:

```powershell
$env:APEX_ENV = "live"
$env:APEX_LIVE_CONFIRM = "YES_I_ACCEPT_RISK"
```

Enable the emergency kill switch:

```powershell
$env:APEX_DISABLE_TRADING = "1"
```

Disable the emergency kill switch:

```powershell
Remove-Item Env:\APEX_DISABLE_TRADING
```

Set MT5 credentials for the current shell:

```powershell
$env:MT5_LOGIN = "103021602"
$env:MT5_PASSWORD = "your-password"
$env:MT5_SERVER = "FBS-Demo"
```

## Run

Run the installed CLI:

```powershell
signal-engine
```

Run directly from source:

```powershell
$env:PYTHONPATH = "src"
python src\interfaces\cli\main.py
```

Run with a specific config file:

```powershell
$env:APEX_CONFIG = "config.yaml"
signal-engine
```

Run with WebSocket auth enabled:

```powershell
$env:WS_SECRET = "change-me"
signal-engine
```

## Test

Run the test suite:

```powershell
pytest
```

Run one test file:

```powershell
pytest src\tests\test_signal_lifecycle.py
```

Run a compile/import syntax check:

```powershell
python -m compileall src
```

## Backtest

Show backtest help:

```powershell
backtest --help
```

Backtest a symbol using MT5 data:

```powershell
backtest --symbol EURUSD --from-date 2024-01-01 --to-date 2024-12-31 --output results\EURUSD.csv
```

Backtest one timeframe pair:

```powershell
backtest --symbol EURUSD --tf-pair 1h:5min --from-date 2024-01-01 --output results\EURUSD_1h_5m.csv
```

Backtest from CSV files:

```powershell
backtest --csv-htf data\EURUSD_1h.csv --csv-ltf data\EURUSD_5m.csv --output results\EURUSD.csv
```

Override basic filters during a backtest:

```powershell
backtest --symbol EURUSD --min-rr 1.5 --max-rr 5 --max-sl-mult 3 --from-date 2024-01-01 --output results\EURUSD.csv
```

Disable optional filters during a backtest:

```powershell
backtest --symbol EURUSD --no-breakeven --no-invalidation --no-trend-filter --no-session-filter --output results\EURUSD.csv
```

Run the bundled multi-symbol PowerShell backtest script:

```powershell
powershell -ExecutionPolicy Bypass -File run_backtests.ps1
```

## Windows Service

Install and start the Windows service. Run PowerShell as Administrator:

```powershell
powershell -ExecutionPolicy Bypass -File install_service.ps1
```

Uninstall the Windows service. Run PowerShell as Administrator:

```powershell
powershell -ExecutionPolicy Bypass -File install_service.ps1 uninstall
```

Check service status:

```powershell
Get-Service BobiFXSignalEngineV2
```

Start the service:

```powershell
Start-Service BobiFXSignalEngineV2
```

Stop the service:

```powershell
Stop-Service BobiFXSignalEngineV2
```

Tail service stderr logs:

```powershell
Get-Content .\logs\stderr.log -Tail 50 -Wait
```

Tail service stdout logs:

```powershell
Get-Content .\logs\stdout.log -Tail 50 -Wait
```

Tail application logs:

```powershell
Get-Content .\logs\signal_engine.log -Tail 50 -Wait
```

## WebSocket Smoke Checks

Install a temporary WebSocket CLI if needed:

```powershell
npm install -g wscat
```

Connect without auth:

```powershell
wscat -c ws://localhost:8765
```

Connect with auth:

```powershell
wscat -c "ws://localhost:8765?token=change-me"
```

Subscribe to symbols after connecting:

```json
{ "action": "subscribe", "symbols": ["EURUSD", "XAUUSD"] }
```

Request server status:

```json
{ "action": "status" }
```

Subscribe to metrics:

```json
{ "action": "subscribe_metrics" }
```

Fetch recent candles:

```json
{ "action": "candles", "symbol": "EURUSD", "interval": "1h", "limit": 100 }
```

## Git

Show changed files:

```powershell
git status --short
```

Show a summary diff:

```powershell
git diff --stat
```

Show the full diff:

```powershell
git diff
```

## Cleanup

Remove Python cache folders:

```powershell
Get-ChildItem -Path . -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

Remove pytest cache:

```powershell
Remove-Item .pytest_cache -Recurse -Force
```

Remove generated runtime output:

```powershell
Remove-Item sessions,logs,results,metrics,charts -Recurse -Force
```
