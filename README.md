# Signal Engine

**Version:** 2.0.0  
**Runtime:** Python 3.11+  
**Entry point:** `signal-engine`

Signal Engine is a real-time trading signal service. It reads candles directly from a local MetaTrader 5 terminal, detects high-timeframe structure and low-timeframe rejection setups, then broadcasts structured signal events to WebSocket clients.

The configured trading universe is focused on two symbols:

- `XAUUSD`
- `JP225`

## Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running](#running)
6. [Commands](#commands)
7. [WebSocket API](#websocket-api)
8. [Signal Lifecycle](#signal-lifecycle)
9. [Scheduler](#scheduler)
10. [Storage](#storage)
11. [Backtesting](#backtesting)

## Overview

The engine:

- Fetches OHLC candles directly from MetaTrader 5 through the `MetaTrader5` Python package.
- Detects HTF swing ranges and break-of-structure targets.
- Watches LTF candles for rejection patterns or CRT sweep-and-reverse entries.
- Applies configurable filters for wick ratio, R:R, sessions, trend, displacement, and circuit breaker pauses.
- Broadcasts signal and metrics events over a raw asyncio WebSocket server.
- Persists open and closed signal state to SQLite and live result files.

## Architecture

```text
Settings
  -> MarketDataClient   direct MT5 terminal access
  -> AssetRegistry      per-symbol profiles
  -> SignalStore        SQLite signal history
  -> SessionStore       JSON/CSV live records
  -> MetricsCollector   SQLite metrics and snapshots
  -> SessionCoordinator dedup, streaks, pauses
  -> SignalService      signal analysis pipeline
  -> SignalScheduler    candle-boundary timer
  -> WebSocketServer    client API and broadcasts
```

Layer map:

| Layer | Path | Responsibility |
|---|---|---|
| Domain | `src/domain/` | Entities, enums, market structure, signal builders |
| Application | `src/app/` | Signal service, session coordinator, backtesting |
| Infrastructure | `src/infrastructure/` | MT5 client, persistence, metrics |
| Interfaces | `src/interfaces/` | CLI, WebSocket server, scheduler |
| Config | `src/config/settings.py` | `.env` and YAML loading |

## Installation

Prerequisites:

- Python 3.11 or newer.
- A local MetaTrader 5 terminal.
- Required symbols visible in MT5 Market Watch.

Install:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\pip install -e ".[dev]"
```

The editable install registers:

- `signal-engine`
- `backtest`

## Configuration

Runtime secrets and deployment guardrails live in `.env`. Strategy and engine settings live in YAML.

Copy the env template:

```powershell
Copy-Item .env.example .env
```

Example `.env`:

```env
MT5_LOGIN=103021602
MT5_PASSWORD="..."
MT5_SERVER="FBS-Demo"

APEX_ENV=paper
APEX_CONFIG=config.yaml
```

`USE_CONFIG=config.yaml` is also accepted. If neither `APEX_CONFIG` nor `USE_CONFIG` is set, the engine loads `config.yaml` when it exists.

Live-mode guardrail:

```env
APEX_ENV=live
APEX_LIVE_CONFIRM=YES_I_ACCEPT_RISK
```

Emergency kill switch, reserved for order execution:

```env
APEX_DISABLE_TRADING=1
```

Edit [config.yaml](config.yaml) for:

- WebSocket host, port, and client limit.
- MT5 terminal path, timeout, and portable mode.
- HTF/LTF timeframe pairs.
- Signal quality filters.
- Feature flags.
- Displacement filter.
- Circuit breaker.
- UTC session filters.
- Logging.

MT5 Python candle timestamps are UTC epoch timestamps. Broker chart/server time, such as a UTC+3 broker display, is not applied in the data provider. Backtest date prints are therefore UTC; a latest 5-minute bar at `22:15` is expected when the machine UTC clock is around `22:18`.

## Running

Start the engine:

```powershell
signal-engine
```

Or run the module directly:

```powershell
$env:PYTHONPATH = "src"
python src\interfaces\cli\main.py
```

On startup the engine loads `.env`, loads the configured YAML file, initializes MT5, starts the WebSocket server, and waits for clients to subscribe to symbols.

## Commands

See [COMMANDS.md](COMMANDS.md) for install, run, test, backtest, service, and troubleshooting commands.

## WebSocket API

Connect to:

```text
ws://<host>:<port>
ws://<host>:<port>?token=<WS_SECRET>
```

The `WS_SECRET` value is optional and comes from `.env`. When it is empty, WebSocket auth is disabled.

Client actions:

| Action | Purpose |
|---|---|
| `subscribe` | Start analysis and broadcasts for one or more symbols |
| `unsubscribe` | Remove symbols from the client subscription |
| `subscribe_metrics` | Receive metrics snapshots every 5 seconds |
| `unsubscribe_metrics` | Stop metrics snapshots |
| `ping` | Keepalive check |
| `status` | Get server/client/scheduler state |
| `candles` | Fetch recent candles from MT5 through the engine |
| `signal.query` | Replay candles to evaluate a known signal payload |
| `zone.sync` | Return currently armed zones |
| `inject` | Broadcast a manual signal payload for testing |

Example subscribe:

```json
{ "action": "subscribe", "symbols": ["XAUUSD", "JP225"] }
```

If a request includes unsupported symbols, the server reports them and proceeds with the allowed symbols.

Example candle request:

```json
{
  "action": "candles",
  "symbol": "XAUUSD",
  "interval": "1h",
  "limit": 200,
  "reqId": "optional-id"
}
```

Server events:

| Event | Trigger |
|---|---|
| `signal.pending` | HTF/LTF zone is armed and waiting for rejection |
| `signal.triggered` | A trade signal is live |
| `signal.tp1_hit` | TP1 was reached |
| `signal.tp2_hit` | TP2 was reached |
| `signal.sl_hit` | Stop loss was reached |
| `signal.invalidated` | LTF invalidation closed the signal |
| `signal.expired` | Signal exceeded configured lifetime |
| `signal.updated` | Non-terminal status update |
| `metrics.snapshot` | Periodic metrics push |

## Signal Lifecycle

```text
zone detected
  -> signal.pending
  -> rejection confirmed
  -> signal.triggered
      -> signal.tp1_hit
          -> signal.tp2_hit
          -> signal.sl_hit / breakeven
      -> signal.sl_hit
      -> signal.invalidated
      -> signal.expired
```

Open signals in `TRIGGERED` or `TP1_HIT` state are restored from SQLite on restart when they have not expired.

## Scheduler

Subscribed symbols run at the lowest configured LTF cadence. For example, with `30min:5min` and `1h:5min`, symbols tick every 5 minutes.

Each tick aligns to UTC candle boundaries plus the configured MT5 settle buffer. The scheduler skips overlapping ticks for the same symbol to avoid race conditions.

## Storage

Generated runtime data:

| Path | Purpose |
|---|---|
| `sessions/signals.db` | SQLite open/closed signal history |
| `sessions/session_YYYY-MM-DD.json` | Human-readable session backup |
| `results/live/*.csv` | Live closed-signal exports |
| `metrics/metrics.db` | Metrics and latency data |
| `logs/signal_engine.log` | Rotating application log |

These folders are ignored by git.

## Backtesting

Backtesting lives in `src/app/backtesting/` and can read from MT5 or CSV files.

Examples:

```powershell
backtest --symbol XAUUSD --from-date 2025-01-01 --output results\XAUUSD.csv --start-balance 100 --risk-percent 5 --spread-points 5
backtest --symbol JP225 --from-date 2025-01-01 --output results\JP225.csv --start-balance 100 --risk-percent 5 --spread-points 3
py -m src.app.backtesting.rba --spread-points 3 --from-date 2025-01-01 --start-balance 100 --risk-percent 5
.\run_backtests.ps1
```

Spread input is broker-style points. For `XAUUSD`, `--spread-points 5` applies `0.50` price units. For `JP225`, `--spread-points 5` applies `5.0` price units.

See [COMMANDS.md](COMMANDS.md) for the full command list.
