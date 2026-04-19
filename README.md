# Signal Engine — Documentation

**Version:** 2.0.0  
**Runtime:** Python ≥ 3.11  
**Entry point:** `signal-engine` (CLI) or `python -m interfaces.cli.main`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Installation](#3-installation)
4. [Configuration](#4-configuration)
5. [Running the Engine](#5-running-the-engine)
6. [WebSocket API](#6-websocket-api)
7. [Signal Lifecycle](#7-signal-lifecycle)
8. [Scheduler — HTF / LTF Watch Modes](#8-scheduler--htf--ltf-watch-modes)
9. [Key Components](#9-key-components)
10. [Data Persistence](#10-data-persistence)
11. [Observability](#11-observability)
12. [Backtesting](#12-backtesting)

---

## 1. Overview

Signal Engine is a real-time trading signal generator that sits between a [MetaTrader 5 (MT5)](https://github.com/amfredfred/MT5-BRIDGE) local data bridge and one or more WebSocket clients (dashboards, bots, alerting systems). It detects high-timeframe (HTF) supply/demand zones, waits for low-timeframe (LTF) price rejection inside those zones, and fires structured trade signals with precise entry, stop-loss, TP1, and TP2 levels.

**What it does:**

- Fetches OHLC candle data from a local MT5 HTTP bridge
- Detects HTF swing ranges and break-of-structure (BOS) targets
- Monitors LTF candles for rejection patterns (Hammer, Shooting Star) or CRT (sweep-and-reverse) setups
- Applies configurable quality filters: wick ratio, R:R, session filter, trend filter
- Broadcasts live signal events over a raw WebSocket connection (no third-party WS library)
- Persists signal history to SQLite and manages open-trade state across restarts
- Exposes a live metrics stream for connected dashboards

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        SignalEngine                          │
│   (interfaces/cli/main.py — composition root)                │
│                                                              │
│  Settings ──────────────────────────────────────┐            │
│  MarketDataClient  (HTTP → MT5 bridge)          │            │
│  AssetRegistry     (per-symbol profiles)        │            │
│  SignalStore       (SQLite — signal history)    │            │
│  SessionStore      (SQLite — session state)     │            │
│  MetricsCollector  (in-memory snapshot)         │            │
│  SessionCoordinator ← SignalStore + SessionStore│            │
│  SignalService     ← all domain + infra deps   │            │
│  SignalScheduler   ← asyncio timer per symbol  │            │
│  WebSocketServer   ← broadcasts to clients     │            │
└──────────────────────────────────────────────────────────────┘
```

**Dependency flow — nothing imports a module-level singleton:**

```
Settings (frozen dataclass)
  └─► MarketDataClient
  └─► AssetRegistry
  └─► SignalStore  ──┐
  └─► SessionStore ──┴──► SessionCoordinator
  └─► MetricsCollector
  └─► SignalService  (uses all of the above)
        └─► SignalScheduler  (timer-driven; calls SignalService.analyze)
              └─► WebSocketServer  (broadcasts SignalService events)
```

**Layer map:**

| Layer | Path | Responsibility |
|---|---|---|
| Domain | `src/domain/` | Entities, enums, market analysis (swings, rejection, structure) |
| Application | `src/app/` | Signal service, session coordinator, backtesting |
| Infrastructure | `src/infrastructure/` | MT5 HTTP client, SQLite stores, metrics |
| Interfaces | `src/interfaces/` | WebSocket server, CLI entry point, scheduler |
| Config | `src/config/settings.py` | Frozen `Settings` dataclass, env loading |

---

## 3. Installation

### Prerequisites

- Python 3.11 or 3.12
- A running MT5 local HTTP bridge at the URL defined by `LOCAL_BASE_URL` (default `http://localhost:8000`)

### Install

```bash
# Clone / unzip the project, then from the project root:
pip install -e .

# For development extras (pytest):
pip install -e ".[dev]"
```

Installing in editable mode registers the `signal-engine` console script defined in `pyproject.toml`.

### Verify

```bash
signal-engine --help
```

---

## 4. Configuration

All configuration is driven by environment variables. Copy `.env.example` to `.env` and fill in your values. The engine calls `Settings.from_env()` once at startup — existing OS env vars always win over `.env` values.

> **Defaults are production-safe.** Override only what you need.

### WebSocket Server

| Variable | Default | Description |
|---|---|---|
| `WS_HOST` | `0.0.0.0` | Bind address for the WebSocket server |
| `WS_PORT` | `8765` | Listen port |
| `WS_SECRET` | *(empty)* | Shared secret token for auth. Leave empty to disable auth (dev mode). Clients must pass `?token=<secret>` in the URL or via `Sec-WebSocket-Protocol` header |
| `MAX_WS_CLIENTS` | `10` | Hard cap on simultaneous WebSocket connections |

### MT5 Data Bridge

| Variable | Default | Description |
|---|---|---|
| `LOCAL_BASE_URL` | `http://localhost:8000` | Base URL of the local MT5 HTTP bridge that serves OHLC data |

### Timezone

| Variable | Default | Description |
|---|---|---|
| `SESSION_TIMEZONE` | `UTC` | IANA timezone string used for session filtering and log timestamps. e.g. `Europe/London` |

### Timeframes

| Variable | Default | Description |
|---|---|---|
| `TF_PAIRS` | `5min:1min` | Comma-separated `htf:ltf` pairs. e.g. `1h:5min,30min:5min`. The HTF must be strictly larger than the LTF |
| `HTF_LOOKBACK` | `240` | Number of HTF candles to look back when seeding zone detection on startup |
| `HTF_OUTPUTSIZE` | `1000` | Total HTF candles fetched per tick from the data bridge |

### Signal Quality Filters

| Variable | Default | Description |
|---|---|---|
| `MIN_WICK_RATIO` | `0.51` | Minimum rejection candle wick-to-range ratio (0–1). Higher = stricter rejection |
| `MAX_SL_ZONE_MULT` | `2.0` | Maximum allowed stop-loss distance as a multiple of the zone height |
| `MIN_RR` | `1.5` | Minimum risk:reward ratio to accept a signal |
| `MAX_RR` | `2.5` | Maximum risk:reward ratio. `0` disables the ceiling |
| `TF_MAX_RR` | `5/1:2.5` | Per-pair RR caps. Format: `HTFmin/LTFmin:cap` comma-separated. e.g. `5/1:2.5,60/5:8`. Falls back to `MAX_RR` for unlisted pairs. Also accepts JSON: `{"5/1": 2.5}` |

### Signal Lifetime

| Variable | Default | Description |
|---|---|---|
| `SIGNAL_EXPIRY_HOURS` | `120` | Hours after creation before an open signal is auto-expired |

### Zone Limits

| Variable | Default | Description |
|---|---|---|
| `MAX_HTF_ZONES_PER_DIR` | `1` | Maximum number of armed HTF zones per direction (LONG/SHORT) per symbol |

### Feature Flags

| Variable | Default | Description |
|---|---|---|
| `USE_TREND_FILTER` | `true` | Only take signals aligned with the higher-timeframe trend bias |
| `USE_BREAKEVEN` | `true` | Move stop to breakeven after TP1 is hit |
| `USE_INVALIDATION` | `false` | Close the trade immediately if price crosses the LTF range mid. When `false`, the crossing is logged but the trade stays open and is only closed by SL/TP |
| `MULTI_TF_INDEPENDENT_POSITIONS` | `true` | Allow independent positions per TF pair on the same symbol |
| `ENTRY_MODEL` | `all` | Entry trigger model. Options: `candle_pattern` (Hammer/Shooting Star only), `crt` (CRT sweep-and-reverse only), `all` (either model) |

### Circuit Breaker

| Variable | Default | Description |
|---|---|---|
| `MAX_CONSECUTIVE_LOSSES` | `10` | Number of consecutive losses before the engine pauses signal generation for a symbol |
| `PAUSE_AFTER_STREAK_H` | `12` | Hours to pause after hitting the consecutive-loss limit |

### Session Filter

| Variable | Default | Description |
|---|---|---|
| `USE_SESSION_FILTER` | `true` | Enable/disable all session filtering |
| `SESSION_TOKYO_ENABLED` | `false` | Allow signals during the Tokyo session (00:00–08:00 UTC) |
| `SESSION_LONDON_ENABLED` | `true` | Allow signals during the London session (08:00–16:00 UTC) |
| `SESSION_NY_ENABLED` | `true` | Allow signals during the New York session (16:00–00:00 UTC) |
| `BLOCKED_HOURS_LONDON` | `9` | Comma-separated UTC hours to block within the London session |
| `BLOCKED_HOURS_NY` | `17,19` | Comma-separated UTC hours to block within the New York session |

### Logging

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `ERROR` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_DIR` | `logs` | Directory for rotating log files. Leave empty to disable file logging |

---

## 5. Running the Engine

```bash
# Using the installed entry point:
signal-engine

# Or directly:
python src/interfaces/cli/main.py

# With a custom env file:
ENV_FILE=/path/to/custom.env signal-engine
```

On startup the engine:

1. Loads `Settings.from_env()`
2. Sets up rotating file + stdout logging
3. Constructs all dependencies (composition root in `main.py`)
4. Starts the WebSocket server
5. Waits for clients to subscribe to symbols, then begins scheduling candle ticks

Shutdown is clean on `SIGINT` or `SIGTERM` — the scheduler drains, open signals are persisted, and all WebSocket connections are closed with a proper RFC 6455 close frame.

---

## 6. WebSocket API

Connect to `ws://<host>:<port>` (or `ws://<host>:<port>?token=<WS_SECRET>` when auth is enabled).

On connect you receive:

```json
{
  "event": "connected",
  "payload": {
    "clientId": "a1b2c3d4",
    "message": "Connected to Signal Engine. Send {action: subscribe, symbols: [...]} to start."
  }
}
```

All messages are JSON. The engine rate-limits clients to **60 messages per minute**.

---

### Client → Server Actions

#### `subscribe`

Start receiving signal events for one or more symbols. Symbols are case-insensitive. Maximum 100 symbols per client.

```json
{ "action": "subscribe", "symbols": ["EURUSD", "GBPUSD"] }
```

Response:
```json
{
  "event": "subscribed",
  "payload": { "symbols": ["EURUSD", "GBPUSD"], "newSymbols": ["EURUSD", "GBPUSD"] }
}
```

---

#### `unsubscribe`

Stop receiving events for specific symbols.

```json
{ "action": "unsubscribe", "symbols": ["GBPUSD"] }
```

Response:
```json
{ "event": "unsubscribed", "payload": { "symbols": ["EURUSD"] } }
```

---

#### `subscribe_metrics`

Receive a metrics snapshot immediately, then every 5 seconds.

```json
{ "action": "subscribe_metrics" }
```

Response: `metrics_subscribed` followed by periodic `metrics.snapshot` events.

---

#### `unsubscribe_metrics`

```json
{ "action": "unsubscribe_metrics" }
```

---

#### `ping`

Keepalive check.

```json
{ "action": "ping" }
```

Response: `{ "event": "pong", "payload": {} }`

---

#### `status`

Get current server state.

```json
{ "action": "status" }
```

Response:
```json
{
  "event": "status",
  "payload": {
    "connectedClients": 3,
    "yourSymbols": ["EURUSD"],
    "schedules": [
      { "symbol": "EURUSD", "mode": "LTF_WATCH", "subscribers": 2, "isRunning": false }
    ]
  }
}
```

---

#### `candles`

Fetch raw OHLC candles for a symbol directly from the MT5 bridge.

```json
{
  "action": "candles",
  "symbol": "EURUSD",
  "interval": "1h",
  "limit": 200,
  "reqId": "optional-correlation-id"
}
```

Response:
```json
{
  "event": "candles",
  "payload": {
    "symbol": "EURUSD",
    "interval": "1h",
    "reqId": "optional-correlation-id",
    "candles": [
      { "t": 1713484800000, "o": 1.0850, "h": 1.0890, "l": 1.0830, "c": 1.0875 }
    ]
  }
}
```

Maximum `limit` is 1000.

---

#### `signal.query`

Query the current status of a known signal by passing its original payload. The engine replays candles to evaluate the outcome.

```json
{
  "action": "signal.query",
  "requestId": "req-001",
  "signal": { "id": "abc123", "symbol": "EURUSD", ... }
}
```

Response: `signal.query_result` with the evaluated outcome.

---

#### `zone.sync`

Get a snapshot of all currently armed HTF zones across all subscribed symbols.

```json
{ "action": "zone.sync", "requestId": "sync-001" }
```

Response:
```json
{
  "event": "zone.sync_result",
  "requestId": "sync-001",
  "zones": [ ... ],
  "count": 4
}
```

---

#### `inject`

Manually inject a signal payload and broadcast it to all subscribed clients (testing/simulation).

```json
{
  "action": "inject",
  "payload": { "id": "test-001", "symbol": "EURUSD", ... }
}
```

---

### Server → Client Events

| Event | Trigger |
|---|---|
| `signal.pending` | A new zone is armed — price has not yet rejected |
| `signal.triggered` | A rejection was confirmed — trade entry is live |
| `signal.tp1_hit` | Price reached TP1 (50% of the way to TP2) |
| `signal.tp2_hit` | Price reached the full BOS target |
| `signal.sl_hit` | Price hit the stop-loss |
| `signal.invalidated` | Price crossed the LTF zone mid (when `USE_INVALIDATION=true`) |
| `signal.expired` | Signal exceeded `SIGNAL_EXPIRY_HOURS` without resolving |
| `signal.updated` | Any other status change (e.g. breakeven move logged) |
| `metrics.snapshot` | Periodic metrics push (every 5 s, opt-in) |

#### Signal Payload Shape

All `signal.*` events carry a payload with this structure:

```json
{
  "id": "eurusd-1h-5min-LONG-1713484800000",
  "symbol": "EURUSD",
  "direction": "LONG",
  "status": "TRIGGERED",
  "entryPrice": 1.0852,
  "stopLoss": 1.0830,
  "tp1": 1.0897,
  "tp2": 1.0942,
  "riskRewardRatio": 2.04,
  "riskPips": 0.0022,
  "htfInterval": "1h",
  "ltfInterval": "5min",
  "createdAt": 1713484800000,
  "triggeredAt": 1713485400000,
  "tp1HitAt": null,
  "tp2HitAt": null,
  "slHitAt": null,
  "outcome": null,
  "realizedRr": null,
  "htfRange": { ... },
  "ltfRange": { ... },
  "rejectionCandle": { ... }
}
```

---

## 7. Signal Lifecycle

```
Zone detected (HTF candle tick)
        │
        ▼
[signal.pending]  ← zone is armed, waiting for LTF rejection
        │
        ▼  (rejection candle confirmed on LTF tick)
[signal.triggered]  ← entry is live, signal added to watchlist
        │
        ├──► [signal.tp1_hit]  → breakeven move applied (if USE_BREAKEVEN)
        │         │
        │         └──► [signal.tp2_hit]  → outcome: WIN_FULL
        │
        ├──► [signal.sl_hit]  → outcome: LOSS
        │
        ├──► [signal.invalidated]  → outcome: INVALIDATED (USE_INVALIDATION=true)
        │
        └──► [signal.expired]  → outcome: EXPIRED (age > SIGNAL_EXPIRY_HOURS)
```

**Breakeven logic:** When TP1 is hit and `USE_BREAKEVEN=true`, the stop-loss is moved to the entry price. The signal status becomes `TP1_HIT` and the watchlist continues monitoring for TP2 or SL.

**Restoration:** On restart, open signals in `TRIGGERED` or `TP1_HIT` state are restored from SQLite into the watchlist, provided they have not expired.

---

## 8. Scheduler — HTF / LTF Watch Modes

Each subscribed symbol runs on an adaptive two-speed schedule managed by `SignalScheduler`.

### HTF_WATCH (default)

The scheduler fires at every HTF candle boundary (e.g. every hour for a `1h:5min` pair). The engine fetches new HTF candles, updates zone detection, and checks whether any zones are now armed.

If armed zones exist → the symbol is promoted to `LTF_WATCH`.

### LTF_WATCH

The scheduler fires at every LTF candle boundary (e.g. every 5 minutes). The engine fetches LTF candles and runs the full rejection + signal analysis pipeline.

If no armed zones remain and no open signals exist → the symbol is demoted back to `HTF_WATCH` to reduce polling frequency.

### Timer internals

Each symbol has its own `threading.Timer` that is rescheduled after each tick completes. Timers align to real candle close boundaries, plus a configurable `ws_candle_buffer_ms` (1500 ms) to allow the MT5 bridge time to settle before the fetch. If a tick is still running when the next boundary fires, the overlapping tick is skipped (logged as a warning) to prevent race conditions.

---

## 9. Key Components

### `Settings` (`src/config/settings.py`)

A frozen dataclass. Instantiate with `Settings()` for unit tests (all defaults, no env reads), or `Settings.from_env()` for production. The frozen constraint makes instances safe to share across threads. Derived properties (`stale_rejection_hours`, `htf_minutes`, etc.) are computed from stored fields and never stored in env vars.

### `SignalService` (`src/app/services/signal_service.py`)

The stateful analysis pipeline. Maintains:

- `_watchlist` — open signals currently being monitored
- `_last_htf / _last_ltf` — candle caches per `(symbol, htf, ltf)` key
- `_last_ranges` — detected HTF ranges per symbol
- `_pending_emitted` — dedup tracker for zone-pending events

On each `analyze(symbol, fired_at)` call it runs: swing detection → range detection → rejection detection → signal construction → quality gates → dedup → persistence → event emission.

### `WebSocketServer` (`src/interfaces/ws/server.py`)

A pure-asyncio RFC 6455 implementation with no third-party WebSocket library. Features:

- HTTP upgrade handshake with HMAC token auth
- Per-client async send queue (max 100 messages) with a poison-pill shutdown
- 30-second heartbeat ping to detect dead connections
- 60 req/min per-client rate limiter
- Graceful close (RFC 6455 close frame + transport abort)

### `SignalScheduler` (`src/interfaces/ws/scheduler.py`)

Thread-safe timer manager. Uses `threading.Timer` (runs in the thread pool) and posts callbacks to the asyncio event loop via `asyncio.run_coroutine_threadsafe`. The `_lock` guards all schedule mutations.

### `SessionCoordinator` (`src/app/session/coordinator.py`)

Tracks per-symbol session state, consecutive loss streaks, and circuit-breaker pauses. Acts as the gate between signal construction and emission — a signal that passes quality filters can still be suppressed here if the session is paused or the session filter blocks the current hour.

### `AssetRegistry` (`src/domain/assets/profiles.py`)

Maintains per-symbol configuration profiles (pip size, spread tolerance, etc.). Looked up by symbol string during signal construction.

### `MarketDataClient` (`src/infrastructure/data_providers/market_data.py`)

Synchronous HTTP client (uses `requests`) wrapping the MT5 local bridge. Called from asyncio via `loop.run_in_executor` so it never blocks the event loop. Methods: `fetch_candles`, `fetch_candles_range`, `close`.

---

## 10. Data Persistence

### SQLite — `signals.db`

Located at `<base_dir>/sessions/signals.db`. Managed by `SignalStore`.

Stores the complete history of all signals, including status transitions and outcome. Open signals (status `TRIGGERED` or `TP1_HIT`) are restored into the watchlist on restart. On a clean shutdown, all open signals are upserted before the process exits.

### Session files — `SessionStore`

Located at `<base_dir>/sessions/`. Tracks per-symbol session state (streak counts, pause expiry). Also writes closed signal records to `<base_dir>/results/live/` as JSON for external consumption.

### Log files

Located at `<log_dir>/signal_engine.log` (default `logs/`). Rotating handler with 10 MB max size and 5 backups. Also streams to stdout.

---

## 11. Observability

`MetricsCollector` (`src/infrastructure/observability/metrics.py`) maintains an in-memory snapshot of:

- Total signals by event type
- Win rate and average R:R
- Per-symbol breakdowns
- WebSocket client connection events
- Signal broadcast latency

Clients opt in via `subscribe_metrics`. The server pushes `metrics.snapshot` every 5 seconds to all subscribed connections.

---

## 12. Backtesting

The `src/app/backtesting/` module provides offline simulation of the signal engine against historical OHLC data.

Key files:

| File | Purpose |
|---|---|
| `backtest.py` | Core simulation loop. Uses Numba JIT (`@njit`) for performance on large datasets |
| `backtest_turbo.py` | Faster variant with simplified assumptions for parameter sweeps |
| `rba.py` | Range-based analysis helpers |
| `summarize_result.py` | Aggregates raw simulation output into summary statistics |

The backtesting module is independent of the live engine's asyncio infrastructure — it can be run directly as a script for strategy validation before deploying settings to production.

---

*Generated from source — signal-engine v2.0.0*
