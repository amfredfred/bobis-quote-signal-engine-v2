# Signal Engine — Technical Reference

> Complete documentation covering architecture, domain model, signal lifecycle,
> entry models, deduplication, risk management, configuration, and extension points.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Project Layout](#2-project-layout)
3. [Domain Model](#3-domain-model)
   - 3.1 [Candle](#31-candle)
   - 3.2 [HtfRange](#32-htfrange)
   - 3.3 [LtfRange](#33-ltfrange)
   - 3.4 [RejectionCandle](#34-rejectioncandle)
   - 3.5 [TradeSignal](#35-tradesignal)
   - 3.6 [Enumerations](#36-enumerations)
4. [Analysis Pipeline](#4-analysis-pipeline)
   - 4.1 [HTF Data Fetch](#41-htf-data-fetch)
   - 4.2 [Trend Filter](#42-trend-filter)
   - 4.3 [HTF Zone Detection](#43-htf-zone-detection)
   - 4.4 [LTF Range Detection](#44-ltf-range-detection)
   - 4.5 [Candle Re-test Gate](#45-candle-re-test-gate)
   - 4.6 [Entry Detection](#46-entry-detection)
   - 4.7 [Signal Construction & Quality Gates](#47-signal-construction--quality-gates)
5. [Entry Models](#5-entry-models)
   - 5.1 [CANDLE_PATTERN — Hammer & Shooting Star](#51-candle_pattern--hammer--shooting-star)
   - 5.2 [CRT — Candle Range Theory](#52-crt--candle-range-theory)
   - 5.3 [ALL — Both Models Active](#53-all--both-models-active)
   - 5.4 [RejectionScore](#54-rejectionscore)
6. [Deduplication & Session State](#6-deduplication--session-state)
   - 6.1 [Rules A–E](#61-rules-ae)
   - 6.2 [Position Lock (Rule B)](#62-position-lock-rule-b)
   - 6.3 [Circuit Breaker](#63-circuit-breaker)
   - 6.4 [Session Rollover](#64-session-rollover)
7. [Signal Lifecycle](#7-signal-lifecycle)
   - 7.1 [Status Machine](#71-status-machine)
   - 7.2 [Watchlist Evaluation](#72-watchlist-evaluation)
   - 7.3 [Invalidation vs. SL](#73-invalidation-vs-sl)
   - 7.4 [Outcomes & Realized RR](#74-outcomes--realized-rr)
8. [Risk & Trade Management](#8-risk--trade-management)
   - 8.1 [Stop Loss Placement](#81-stop-loss-placement)
   - 8.2 [Take Profit Levels](#82-take-profit-levels)
   - 8.3 [RR Gates](#83-rr-gates)
   - 8.4 [Breakeven](#84-breakeven)
9. [Asset Profiles](#9-asset-profiles)
10. [Session Filter](#10-session-filter)
11. [Multi-Timeframe Support](#11-multi-timeframe-support)
12. [Configuration Reference](#12-configuration-reference)
13. [Event System](#13-event-system)
14. [Persistence Layer](#14-persistence-layer)
15. [Backtesting](#15-backtesting)
16. [Extending the Engine](#16-extending-the-engine)
17. [Glossary](#17-glossary)

---

## 1. Architecture Overview

The engine follows a strict **layered architecture**. Dependencies only flow inward — outer layers may import from inner layers, never the reverse.

```
┌─────────────────────────────────────────────────┐
│  app/                  Application layer         │
│   services/signal_service.py  ← orchestrator    │
│   session/coordinator.py      ← state owner     │
├─────────────────────────────────────────────────┤
│  domain/               Pure domain logic         │
│   market/  — BOS, swings, rejection, structure  │
│   signals/ — builder, dedup rules               │
│   entities/— value objects, enums               │
│   assets/  — per-symbol profiles                │
├─────────────────────────────────────────────────┤
│  infrastructure/       I/O adapters              │
│   data_providers/ — market data (MT5 / HTTP)    │
│   persistence/    — SQLite, JSON, CSV           │
│   observability/  — metrics                     │
├─────────────────────────────────────────────────┤
│  config/              Settings (dataclass)       │
└─────────────────────────────────────────────────┘
```

**Key design rules:**

- `domain/` has **zero** imports from `config/` or `infrastructure/`. All parameters are injected as plain arguments.
- `config/Settings` is a **frozen dataclass** — safe to share across threads, no accidental mutation.
- The `SessionCoordinator` owns all mutable runtime state. `SignalService` orchestrates but does not hold state directly beyond caches.
- Every domain function is **pure** and fully unit-testable with no mocking required.

---

## 2. Project Layout

```
src/
├── app/
│   ├── backtesting/
│   │   ├── backtest.py             Walk-forward signal replay
│   │   ├── run_backtest_all.py     Batch runner (all symbols × TF pairs)
│   │   └── summarize_result.py     Stats aggregator / reporter
│   ├── services/
│   │   └── signal_service.py       Main analysis pipeline & watchlist
│   └── session/
│       └── coordinator.py          Dedup state, history, circuit breaker
│
├── config/
│   └── settings.py                 Frozen Settings dataclass + from_env()
│
├── domain/
│   ├── assets/
│   │   └── profiles.py             AssetProfile, AssetRegistry, session defs
│   ├── entities/
│   │   ├── candle.py               Raw OHLCV atom
│   │   ├── enums.py                All enumerations (canonical source)
│   │   ├── payloads.py             WebSocket payload dataclasses
│   │   ├── ranges.py               HtfRange, LtfRange, RejectionCandle
│   │   ├── session.py              ClosedSignalRecord, WsMessage
│   │   └── trade.py                TradeSignal + to_dict / from_dict
│   ├── market/
│   │   ├── rejection.py            RejectionDetector, CrtDetector, RejectionScore
│   │   ├── structure.py            MarketStructure (HTF bias from BOS sequence)
│   │   └── swings.py               SwingDetector, detect_bos_events, pivot helpers
│   └── signals/
│       ├── builder.py              build_signal() — validation + construction
│       └── dedup.py                DedupState, should_emit(), DedupResult
│
└── infrastructure/
    ├── data_providers/
    │   ├── chart_data.py           Chart artefact builder
    │   └── market_data.py          HTTP client → MT5 bridge
    ├── observability/
    │   └── metrics.py              Prometheus / structured metrics
    └── persistence/
        ├── signal_store.py         SQLite — open & closed signals
        └── session_store.py        JSON session backup + CSV trade log
```

---

## 3. Domain Model

### 3.1 Candle

```python
@dataclass(slots=True)
class Candle:
    timestamp: int    # UTC milliseconds
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float = 0.0
```

Derived properties computed on access:

| Property | Formula |
|---|---|
| `is_bullish` | `close >= open` |
| `body` | `abs(close - open)` |
| `total_range` | `high - low` |
| `upper_wick` | `high - max(open, close)` |
| `lower_wick` | `min(open, close) - low` |

All timestamps throughout the engine are **UTC milliseconds** (integer). The `dt(tz)` method converts for display only — the domain never formats timestamps.

---

### 3.2 HtfRange

A supply/demand zone confirmed by a Break of Structure on the higher timeframe.

```
  range_high ──── top of swing candle wick
  ░░░░░░░░░░      zone body (the HTF candle)
  range_low  ──── bottom of swing candle wick

  broken_at  ──── timestamp of the BOS candle (close confirms the break)
  tp_level   ──── the swing level that was broken (the measured-move target)
```

| Field | Meaning |
|---|---|
| `range_high` / `range_low` | Wick extremes of the swing candle — the zone box |
| `bos_direction` | `BULLISH` (demand zone) or `BEARISH` (supply zone) |
| `timestamp` | Open timestamp of the swing candle (ms) |
| `broken_at` | Candle that confirmed the BOS by closing beyond the broken level |
| `tp_level` | The broken swing level — the true TP2 target (BOS measured-move) |
| `htf_candle_open` / `htf_candle_close` | Window for LTF range search |
| `midpoint` | `(range_high + range_low) / 2` |
| `height` | `range_high - range_low` |

**Zone freshness**: a zone is only returned by `SwingDetector.find_htf_ranges()` if no candle has closed beyond its far edge after `broken_at`. Taken-out zones are silently discarded.

---

### 3.3 LtfRange

The most extreme LTF swing that formed **inside** the HTF swing candle's time window. This is the re-test level — price must return to this zone to trigger an entry.

```
  SHORT zone          LONG zone
  ─────────           ─────────
  range_high ← sl     range_high
  ░░░░░░░░░░          ░░░░░░░░░░
  range_low           range_low  ← sl
```

| Field | Meaning |
|---|---|
| `range_high` / `range_low` | Wick extremes of the most extreme LTF swing in window |
| `timestamp` | The LTF swing candle timestamp |
| `direction` | `SHORT` or `LONG` — derived from HTF BOS direction |
| `sl_level` | `range_high` (SHORT) or `range_low` (LONG) — the invalidation level |

**How it's found**: `SwingDetector.find_ltf_range()` filters all LTF candles to those whose timestamps fall within `[htf_candle_open, htf_candle_close]`, then picks the one with the highest `high` (SHORT) or lowest `low` (LONG).

---

### 3.4 RejectionCandle

The entry trigger candle. It has retested the LTF range and confirmed the rejection.

```python
@dataclass(slots=True)
class RejectionCandle:
    open, high, low, close: float
    timestamp:  int
    wick_ratio: float         # sweep wick / total_range
    pattern:    CandlePattern # SHOOTING_STAR | HAMMER | CRT_SELL | CRT_BUY
```

`wick_tip` — the extreme of the sweep wick, used as SL reference when `stop_placement = "wick"`:

| Pattern | `wick_tip` |
|---|---|
| `SHOOTING_STAR` | `high` |
| `HAMMER` | `low` |
| `CRT_SELL` | `high` |
| `CRT_BUY` | `low` |

The entry price is always `rejection_candle.close` — execution at the close of the trigger bar.

---

### 3.5 TradeSignal

The fully-qualified trade setup. Mutable by the watchlist manager as the trade progresses.

```
Identity:    id, symbol, direction, status
Levels:      entry_price, stop_loss, tp1, tp2
Structure:   htf_range, ltf_range, rejection_candle
Risk:        risk_reward_ratio, risk_pips
TF pair:     htf_interval, ltf_interval
Timestamps:  created_at → triggered_at → tp1_hit_at → tp2_hit_at / sl_hit_at → closed_at
Result:      outcome, realized_rr, close_price
```

Serialised by `to_dict()` / `from_dict()` — the wire format used by WebSocket events, persistence, and the query API. The `pattern` field in `rejectionCandle` will be one of `SHOOTING_STAR`, `HAMMER`, `CRT_SELL`, or `CRT_BUY` — useful for filtering/reporting by entry model.

---

### 3.6 Enumerations

All enums live in `domain/entities/enums.py` — the canonical single import point.

| Enum | Values |
|---|---|
| `SignalDirection` | `LONG`, `SHORT` |
| `SignalStatus` | `PENDING`, `TRIGGERED`, `TP1_HIT`, `TP2_HIT`, `SL_HIT`, `INVALIDATED`, `EXPIRED` |
| `SignalOutcome` | `WIN_FULL`, `BREAKEVEN`, `LOSS`, `INVALIDATED`, `EXPIRED` |
| `SignalEvent` | `signal.pending`, `signal.triggered`, `signal.tp1_hit`, `signal.tp2_hit`, `signal.sl_hit`, `signal.invalidated`, `signal.expired`, `signal.updated` |
| `BosDirection` | `BULLISH`, `BEARISH` |
| `CandlePattern` | `SHOOTING_STAR`, `HAMMER`, `CRT_SELL`, `CRT_BUY` |
| `EntryModel` | `candle_pattern`, `crt`, `all` |
| `TrendBias` | `LONG`, `SHORT`, `NEUTRAL` |

Convenience frozensets for outcome grouping:

```python
WIN_OUTCOMES     = {WIN_FULL, BREAKEVEN}
CLOSED_OUTCOMES  = {WIN_FULL, BREAKEVEN, LOSS}
VOID_OUTCOMES    = {INVALIDATED, EXPIRED}
```

---

## 4. Analysis Pipeline

`SignalService.analyze(symbol, fired_at)` is the entry point. It loops over all configured `tf_pairs` and calls `_analyze_pair()` for each. Each pair runs independently through the following stages.

```
fetch HTF candles
       │
       ▼
trend filter (optional)
       │
       ▼
detect HTF zones (BOS-confirmed)
       │
       ▼
for each zone:
    find LTF range inside zone window
           │
           ▼
    filter candles re-testing LTF range
           │
           ▼
    run entry detector (CANDLE_PATTERN / CRT / ALL)
           │
           ▼
    dedup gate (rules A–E + position lock + circuit breaker)
           │
           ▼
    build_signal() — quality gates + RR validation
           │
           ▼
    emit SIGNAL_TRIGGERED event
    register in watchlist
    break (one signal per pair per call)
```

### 4.1 HTF Data Fetch

```python
htf_full = await md.fetch_candles(symbol, htf_interval, htf_outputsize, "ASC")
htf      = htf_full[-profile.htf_lookback:]
```

`htf_outputsize` (default 1000) determines how far back history is fetched. `htf_lookback` (default 120) slices the working window to avoid stale zones dominating. Both are configurable per-symbol via `AssetProfile`.

LTF candles are fetched from the timestamp of the oldest HTF candle onward:

```python
ltf_all = await md.fetch_candles_range(symbol, ltf_interval, htf[0].timestamp)
```

### 4.2 Trend Filter

When `use_trend_filter=True` (the default), `MarketStructure.detect()` runs on the HTF candle slice.

`MarketStructure` scans BOS events chronologically and derives bias from the **most recent** BOS direction:

- Last BOS was `BULLISH` → bias = `LONG` → only LONG signals pass.
- Last BOS was `BEARISH` → bias = `SHORT` → only SHORT signals pass.
- No BOS detected → bias = `NEUTRAL` → pair is **skipped entirely**.

This enforces trend-following discipline — counter-trend zones are detected but suppressed.

### 4.3 HTF Zone Detection

`SwingDetector.find_htf_ranges()` runs `detect_bos_events()` to find pivot-confirmed BOS events, then for each:

1. Constructs an `HtfRange` from the swing candle's wick extremes.
2. **Freshness check**: scans all subsequent candles — if any closed beyond `range_high` (for a bearish zone) or `range_low` (for a bullish zone), the zone is discarded.
3. **TP level**: scans post-BOS candles to find the swing level that price has already reached and sets `tp_level` to that extreme.
4. Caps results to `max_htf_zones_per_dir` most recent zones per direction.

**BOS detection** (`detect_bos_events`):

Pivot highs/lows are confirmed with `pivot_bars` neighbours on each side (default 1 — the immediate neighbours must be strictly lower/higher). After confirming both a pivot high and low, the engine watches for a candle that closes beyond the last confirmed pivot:

- Close > pivot high → `BULLISH BOS`, zone candle = last pivot **low**.
- Close < pivot low → `BEARISH BOS`, zone candle = last pivot **high**.

After a BOS fires, both references reset so stale levels never carry forward.

### 4.4 LTF Range Detection

For each HTF zone, `SwingDetector.find_ltf_range()` filters LTF candles to the window `[htf_candle_open, htf_candle_close]` and picks the extreme:

- **SHORT zone** (bearish BOS): candle with the highest `high` → this is the supply level to sell from.
- **LONG zone** (bullish BOS): candle with the lowest `low` → this is the demand level to buy from.

The resulting `LtfRange.sl_level` is that same extreme — `range_high` for SHORT, `range_low` for LONG.

### 4.5 Candle Re-test Gate

`SwingDetector.candles_entering_ltf()` enforces that price **first left the zone** before re-entering. This prevents triggering on the initial zone formation.

```
SHORT re-test sequence:
  1. A candle closes BELOW range_low  (price left downward)
  2. A later candle wicks back UP to range_low but closes back below it
     → this candle qualifies as a re-test candle

LONG re-test sequence:
  1. A candle closes ABOVE range_high (price left upward)
  2. A later candle wicks back DOWN to range_high but closes back above it
     → this candle qualifies as a re-test candle
```

Both candles must be timestamped after `htf_candle_close` — entry signals cannot fire during zone formation.

### 4.6 Entry Detection

See [Section 5](#5-entry-models) for full detail. The dispatcher:

```python
def _find_entry(self, entries, ltf_range):
    model = self._cfg.entry_model   # "candle_pattern" | "crt" | "all"
    candidates = []

    if model in ("candle_pattern", "all"):
        r = RejectionDetector.find_most_recent(entries, ltf_range, min_wick_ratio=...)
        if r: candidates.append(r)

    if model in ("crt", "all"):
        r = CrtDetector.find_most_recent(entries, ltf_range)
        if r: candidates.append(r)

    if not candidates:
        return None
    return max(candidates, key=lambda r: r[0].timestamp)  # most recent wins
```

### 4.7 Signal Construction & Quality Gates

`build_signal()` enforces six gates in order. **Any failure returns `None`** (silent skip, debug log):

| Gate | Check |
|---|---|
| 1. SL direction | SL must be beyond entry in the trade direction |
| 2. SL distance cap | `risk ≤ max_sl_zone_mult × zone_height` |
| 3. TP2 direction | TP2 must be beyond entry in the trade direction |
| 4. RR floor | `RR ≥ min_rr` |
| 5. RR cap | If `RR > max_rr`, TP2 is adjusted inward (signal not skipped) |
| 6. Session filter | Rejection candle timestamp must be inside an allowed session |

---

## 5. Entry Models

The entry model is controlled by `ENTRY_MODEL` in `.env` / `Settings.entry_model`. Both detectors live inside the rejection module and share the same `(RejectionCandle, RejectionScore)` return contract — nothing downstream changes based on which model fired.

### 5.1 CANDLE_PATTERN — Hammer & Shooting Star

Classic wick-dominance rejection. Scans re-test candles in reverse (most recent first).

**SHOOTING_STAR** (SHORT / sell):
```
Condition:  candle.high ≥ ltf_range.range_low          (wick entered zone)
            upper_wick / total_range ≥ min_wick_ratio   (wick is dominant)
            upper_wick = candle.high - max(open, close)

Metrics:    wick_penetration = (candle.high - range_low) / zone_size
            close_proximity  = (range_low - candle.close) / zone_size   (+ve = outside)
```

**HAMMER** (LONG / buy):
```
Condition:  candle.low ≤ ltf_range.range_high           (wick entered zone)
            lower_wick / total_range ≥ min_wick_ratio   (wick is dominant)
            lower_wick = min(open, close) - candle.low

Metrics:    wick_penetration = (range_high - candle.low) / zone_size
            close_proximity  = (candle.close - range_high) / zone_size  (+ve = outside)
```

`min_wick_ratio` defaults to `0.65` — the wick must account for at least 65% of the candle's total range. Adjustable via `MIN_WICK_RATIO` env var.

### 5.2 CRT — Candle Range Theory

CRT entries trigger when price **sweeps a key level and immediately closes back inside**. This captures liquidity grabs and false breaks, giving an earlier and often tighter entry than waiting for a full wick-dominance candle.

**CRT SELL** (SHORT zone):
```
Condition:  candle.high > ltf_range.range_high   (wick swept above the level)
            candle.close < ltf_range.range_high   (closed back inside)

Interpretation: liquidity above the swing high was grabbed, sellers stepped in,
                close confirms rejection → enter sell immediately on close.

Entry:   candle.close
SL:      candle.high + buffer   (the sweep wick tip)
Pattern: CRT_SELL
```

**CRT BUY** (LONG zone):
```
Condition:  candle.low < ltf_range.range_low     (wick swept below the level)
            candle.close > ltf_range.range_low    (closed back inside)

Interpretation: liquidity below the swing low was grabbed, buyers stepped in,
                close confirms rejection → enter buy immediately on close.

Entry:   candle.close
SL:      candle.low - buffer    (the sweep wick tip)
Pattern: CRT_BUY
```

The sweep wick used for `wick_ratio` in CRT is:
- **SELL**: `candle.high - max(open, close)` (upper shadow above body)
- **BUY**: `min(open, close) - candle.low` (lower shadow below body)

Unlike `CANDLE_PATTERN`, CRT has **no `min_wick_ratio` gate** — the required condition is purely about crossing and closing back through the level. A small sweep is still valid.

> **Diagram (from spec image):**
>
> ```
>  STOP LOSS ─── candle.high (sweep tip)
>      │
>      │  ← CRT candle: wick above range_high, close back inside
> ─────┼───── range_high  (CRT ENTRY LEVEL)
>      │
>      │
> ─────┼───── range_low   (TAKE PROFIT LEVEL)
> ```

### 5.3 ALL — Both Models Active

When `ENTRY_MODEL=all`, both detectors scan the same `entries` slice independently. The result with the **most recent timestamp** is used. This means:

- If CRT fires on candle T+3 and HAMMER fires on candle T+1, CRT wins (newer).
- If only one model produces a result, that result is used.
- In the rare case where both models match the **same candle** (e.g. a candle that both wicks through the range_high AND has dominant upper wick), the candle-pattern result is used (appended first, `max()` is stable).

The `signal_id` includes the rejection candle's timestamp, so even in `ALL` mode the two models can never produce a duplicate signal ID for the same candle.

### 5.4 RejectionScore

Both detectors compute a composite quality score for logging and sorting:

```python
total = wick_penetration * 0.5
      + wick_ratio       * 0.3
      + close_proximity  * 0.2
```

| Component | Meaning | Weight |
|---|---|---|
| `wick_penetration` | How deep the wick entered the zone (normalised by zone size) | 50% |
| `wick_ratio` | Sweep wick as fraction of total candle range | 30% |
| `close_proximity` | How far the close is from the level (signed: + = further away) | 20% |

The score is available in debug logs but does not gate signals — it exists for ranking in `find_all_scored()` and future analytics use.

---

## 6. Deduplication & Session State

All dedup logic lives in `domain/signals/dedup.py` (pure rules) and `app/session/coordinator.py` (state owner + persistence bridge).

### 6.1 Rules A–E

| Rule | Key | What it prevents |
|---|---|---|
| A | `(htf_range.timestamp, direction)` | Same BOS zone firing more than once per session |
| B | `(symbol, direction[, htf, ltf])` | Opening a second position in the same direction while one is live |
| C | `(ltf_range.timestamp, direction)` | Same LTF swing candle being reused after a zone expires |
| D | Age check | Rejection candles older than `stale_hours` being reused |
| E | `rejection.timestamp` | The exact same rejection candle firing twice |

Rules A, C, E are persisted in `DedupState` and replayed from the SQLite / JSON store on startup, so they survive restarts. Rule B is ephemeral — it clears when a position closes.

**Stale hours** is computed dynamically as `3 × ltf_minutes / 60` — i.e. 3 LTF candles' worth of time. For a 5-minute LTF that is 15 minutes. This prevents a rejection candle that formed long ago from firing on the next analyze tick.

### 6.2 Position Lock (Rule B)

When `multi_tf_independent_positions=True` (default), the key is `(symbol, direction, htf_interval, ltf_interval)` — so a LONG on the 1h/5min pair does **not** block a LONG on the 4h/15min pair for the same symbol.

When `multi_tf_independent_positions=False`, the key is `(symbol, direction, "", "")` — one position per direction per symbol regardless of timeframe.

The lock is released by `SessionCoordinator.record_outcome()` when the signal closes.

### 6.3 Circuit Breaker

After `max_consecutive_losses` losses in a row (default 3), the engine pauses signal emission for `pause_after_streak_h` hours (default 12). The pause timer is derived from `closed_at` of the most recent loss — no extra persistence is needed.

The pause is re-evaluated on every `should_emit()` call. If the pause window has elapsed since the last loss, the breaker resets silently and trading resumes.

```
max_consecutive_losses = 3
pause_after_streak_h   = 12.0

→ 3rd consecutive loss closed at 14:00 UTC
→ engine blocked until 02:00 UTC next day
→ status_line() shows: ⚠ 3 consecutive losses  🔴 PAUSED
```

### 6.4 Session Rollover

`SessionCoordinator._maybe_rollover()` is called on every `should_emit()`. When midnight (in `SESSION_TIMEZONE`) passes, it:

1. Resets `DedupState` — dead zones, seen LTF, seen rejections all clear.
2. Clears history deque.
3. Clears `paused_until`.
4. Calls `_load_session()` for the new day (in case of a delayed restart after midnight).

This means each calendar day gets a fresh slate — zones from yesterday cannot carry into today.

---

## 7. Signal Lifecycle

### 7.1 Status Machine

```
                  ┌─────────────────────────────┐
                  │         PENDING              │  (zone armed, no entry yet)
                  └──────────────┬──────────────┘
                                 │  entry candle forms
                                 ▼
                  ┌─────────────────────────────┐
                  │        TRIGGERED             │  added to watchlist
                  └──┬──────────────────────┬───┘
                     │                      │
              tp1 hit│                   sl hit│ (or expiry / invalidation)
                     ▼                      ▼
          ┌──────────────────┐    ┌──────────────────────┐
          │    TP1_HIT        │    │  SL_HIT / INVALIDATED│
          └──────┬───────────┘    │  / EXPIRED            │
                 │                └──────────────────────┘
       sl hit or │expiry
       (breakeven)│              tp2 hit│
                 ▼                      ▼
          ┌──────────────────────────────────┐
          │  TP2_HIT / SL_HIT / INVALIDATED  │
          │  / EXPIRED                        │ ← terminal states
          └──────────────────────────────────┘
```

### 7.2 Watchlist Evaluation

`SignalService.update_watchlist()` is called on each LTF candle close. It fetches fresh candles from `signal.triggered_at` onward and replays them **candle by candle** (not collapsed) through `_evaluate_signal()`.

The candle-by-candle replay is critical for correct SL-before-TP1 detection. An implementation that checks aggregate `high`/`low` across all candles would misreport a trade that hit SL before TP1 as a breakeven or win.

Each candle provides:
- `price` = `candle.close` (settlement)
- `high` / `low` (for SL and TP range checks)
- `candle_close` = previous candle's close (for invalidation logic)

### 7.3 Invalidation vs. SL

An **invalidation** occurs when a candle closes beyond the LTF range boundary in the wrong direction:

- SHORT: `candle_close > ltf_range.range_high`
- LONG: `candle_close < ltf_range.range_low`

Behaviour depends on `use_invalidation` (per asset profile):

| Setting | Behaviour |
|---|---|
| `use_invalidation=False` (default) | Signal stays open; `SIGNAL_INVALIDATED` event emitted once for logging; SL/TP still close the trade |
| `use_invalidation=True` | Trade closes immediately; if `TP1_HIT` + `use_breakeven=True`, closes at breakeven |

### 7.4 Outcomes & Realized RR

| Outcome | Condition | `realized_rr` |
|---|---|---|
| `WIN_FULL` | TP2 reached | `risk_reward_ratio` (full RR) |
| `BREAKEVEN` | TP1 hit, then SL/expiry/invalidation | `risk_reward_ratio × tp1_multiplier` |
| `LOSS` | SL hit before TP1 | `-1.0` |
| `EXPIRED` | Age > `signal_expiry_hours` | `0.0` (or BREAKEVEN if TP1 was hit) |
| `INVALIDATED` | `use_invalidation=True`, close beyond range | `-1.0` (or BREAKEVEN if TP1 was hit) |

---

## 8. Risk & Trade Management

### 8.1 Stop Loss Placement

Controlled by `stop_placement_method` (fixed to `"swing"` globally, overridable in asset profiles):

| Method | SL reference |
|---|---|
| `"swing"` | `ltf_range.sl_level` — the LTF swing extreme (`range_high` SHORT, `range_low` LONG) |
| `"wick"` | `rejection.wick_tip` — the sweep wick tip of the trigger candle |

A small buffer is added: `sl = sl_level + sl_level × stop_buffer_pct` (SHORT) or `sl = sl_level - sl_level × stop_buffer_pct` (LONG). Default `stop_buffer_pct = 0.00001` (1 pip equivalent for 5-decimal instruments).

### 8.2 Take Profit Levels

**TP2** (`htf_range.tp_level`) is the broken HTF swing level — the BOS measured-move target. This produces average 1:4 R:R setups. It is **never** the zone edge (`range_low` for SHORT, `range_high` for LONG) — that would give ≈1:1 and is explicitly avoided.

**TP1** is always `entry + (tp2 - entry) × tp1_multiplier`. Default `tp1_multiplier = 0.5` — halfway to TP2. TP1 is a partial close trigger; the remaining position runs to TP2.

### 8.3 RR Gates

| Parameter | Default | Env var |
|---|---|---|
| `min_rr` | `1.5` | `MIN_RR` |
| `max_rr` | `9.0` | `MAX_RR` |

`max_rr = 0` disables the cap. When `max_rr > 0` and the natural RR exceeds it, TP2 is adjusted closer (the signal is not skipped — the trade is taken with a tighter target).

### 8.4 Breakeven

When `use_breakeven=True` (default), after TP1 is hit the effective SL becomes the entry price. This is modelled in outcome logic:

- Any close after TP1 that would have been a loss → `BREAKEVEN` outcome, `realized_rr = rr × tp1_multiplier`.
- This applies to: SL hit after TP1, invalidation after TP1, expiry after TP1.

---

## 9. Asset Profiles

`AssetProfile` is a frozen dataclass holding all per-symbol quality parameters. `AssetRegistry.get(symbol)` resolves a symbol to its profile through three layers of precedence:

```
SYMBOL_OVERRIDES  (highest — explicit per-symbol tuning)
        ↓
_CLASS_OVERRIDES  (asset-class defaults: FOREX, COMMODITY, INDICES)
        ↓
Settings defaults  (fallback for unknown symbols)
```

Symbol normalisation strips `/` and uppercases: `"eurusd"` → `"EUR/USD"`, `"xauusd"` → `"XAU/USD"`.

**Currently mapped symbols:**

| Symbol | Class |
|---|---|
| `XAU/USD` | COMMODITY |
| `EUR/USD`, `GBP/USD`, `USD/JPY`, `USD/CHF`, `AUD/USD`, `USD/CAD`, `NZD/USD`, `EUR/JPY` | FOREX |
| `US500` | INDICES |

To add a new symbol: one line in `ASSET_CLASS_MAP`. To tune a class: edit `_CLASS_OVERRIDES`. To tune a specific symbol: add to `SYMBOL_OVERRIDES` with only the keys you want to override.

---

## 10. Session Filter

The session filter gates signal emission to configured trading hours. It is applied inside `build_signal()` as gate 6 — the rejection candle's timestamp must fall within an allowed session window.

Three sessions are defined, each independently enabled/disabled and with configurable blocked hours:

| Session | Default window (UTC) | Enabled by default |
|---|---|---|
| `TOKYO` | 00:00 – 08:00 | No |
| `LONDON` | 08:00 – 16:00 | Yes (hour 9 blocked) |
| `NEW_YORK` | 16:00 – 00:00 | Yes (hours 17, 19 blocked) |

Blocked hours prevent trading during specific high-volatility or illiquid intraday windows (e.g. first hour of London, spread-widening hours in NY).

When `use_session_filter=False`, the filter is bypassed entirely and all hours are valid.

Session windows and blocked hours can be fully customised per-symbol via `AssetProfile.sessions`.

---

## 11. Multi-Timeframe Support

`TF_PAIRS` defines one or more `htf:ltf` pairs to analyse simultaneously:

```env
TF_PAIRS=1h:5min               # single pair (default)
TF_PAIRS=1h:5min,4h:15min      # two pairs, each runs independently
```

Each pair runs a **fully independent** pipeline with its own HTF zones, LTF ranges, and watchlist signals. The `multi_tf_independent_positions` setting controls whether the direction lock (Rule B) is scoped to the TF pair or shared across all pairs for a symbol.

Signal IDs embed the TF pair: `{symbol}_{htf}_{ltf}_{rej_ts}_{direction}` — so the same rejection candle on different TF pairs produces different IDs and can coexist in the watchlist.

---

## 12. Configuration Reference

All settings are read by `Settings.from_env()` at startup. `Settings()` (no args) uses defaults — safe for unit tests.

### Timeframes

| Env var | Default | Description |
|---|---|---|
| `TF_PAIRS` | `1h:5min` | Comma-separated `htf:ltf` pairs |
| `HTF_LOOKBACK` | `120` | HTF candles to keep in the working window |
| `HTF_OUTPUTSIZE` | `1000` | Total HTF candles to fetch |

### Signal Quality

| Env var | Default | Description |
|---|---|---|
| `MIN_WICK_RATIO` | `0.65` | Minimum wick dominance for CANDLE_PATTERN entries |
| `MAX_SL_ZONE_MULT` | `2.0` | Maximum risk as a multiple of HTF zone height |
| `MIN_RR` | `1.5` | Minimum risk:reward ratio |
| `MAX_RR` | `9.0` | Maximum RR (TP2 adjusted if exceeded; `0` = disabled) |
| `SIGNAL_EXPIRY_HOURS` | `120` | Hours before an open signal is expired |
| `MAX_HTF_ZONES_PER_DIR` | `1` | Most recent zones to keep per direction |

### Entry Model

| Env var | Default | Values |
|---|---|---|
| `ENTRY_MODEL` | `candle_pattern` | `candle_pattern` / `crt` / `all` |

### Feature Flags

| Env var | Default | Description |
|---|---|---|
| `USE_TREND_FILTER` | `true` | Require HTF bias alignment |
| `USE_BREAKEVEN` | `true` | Move SL to entry after TP1 |
| `USE_INVALIDATION` | `false` | Close trade on range break (vs. log-only) |
| `MULTI_TF_INDEPENDENT_POSITIONS` | `true` | Scope direction lock to TF pair |

### Circuit Breaker

| Env var | Default | Description |
|---|---|---|
| `MAX_CONSECUTIVE_LOSSES` | `3` | Loss streak threshold before pause |
| `PAUSE_AFTER_STREAK_H` | `12` | Hours to pause after streak |

### Sessions

| Env var | Default | Description |
|---|---|---|
| `USE_SESSION_FILTER` | `true` | Enable session hour gating |
| `SESSION_TOKYO_ENABLED` | `false` | Trade Tokyo session |
| `SESSION_LONDON_ENABLED` | `true` | Trade London session |
| `SESSION_NY_ENABLED` | `true` | Trade New York session |
| `BLOCKED_HOURS_LONDON` | `9` | Comma-separated UTC hours to skip in London |
| `BLOCKED_HOURS_NY` | `17,19` | Comma-separated UTC hours to skip in NY |

### Infrastructure

| Env var | Default | Description |
|---|---|---|
| `WS_HOST` | `0.0.0.0` | WebSocket server bind address |
| `WS_PORT` | `8765` | WebSocket server port |
| `WS_SECRET` | `` | Shared secret for client auth |
| `MAX_WS_CLIENTS` | `10` | Maximum concurrent WebSocket connections |
| `LOCAL_BASE_URL` | `http://localhost:8000` | MT5 bridge HTTP base URL |
| `SESSION_TIMEZONE` | `UTC` | Timezone for session day boundaries |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## 13. Event System

`SignalService` exposes a simple observer pattern. Listeners receive every lifecycle event synchronously before `analyze()` returns.

```python
service.add_listener(fn)    # fn(event: SignalEvent, payload: dict) -> None
service.remove_listener(fn)
```

Events and their payloads:

| Event | Payload |
|---|---|
| `signal.pending` | `SignalPendingPayload` — zone armed, no entry yet |
| `signal.triggered` | Full `TradeSignal.to_dict()` |
| `signal.tp1_hit` | Update payload with `previousStatus`, `currentStatus`, `price` |
| `signal.tp2_hit` | Update payload with outcome, `realizedRR`, `closePrice` |
| `signal.sl_hit` | Update payload with outcome, `realizedRR`, `closePrice` |
| `signal.invalidated` | Update payload (may be emitted multiple times if `use_invalidation=False`) |
| `signal.expired` | Update payload with outcome |
| `signal.updated` | Generic update (used by chart/metrics layer) |

All update payloads include `sessionStats` — the full `SessionCoordinator.stats()` dict — so downstream consumers can display live session P&L without polling.

**MetricsCollector** hooks into `_emit()` automatically when injected and receives every event for Prometheus counters/gauges.

---

## 14. Persistence Layer

### SignalStore (SQLite)

Primary persistence. Two tables:

- **`open_signals`**: upserted on every watchlist change; deleted when a signal closes.
- **`closed_signals`**: insert-once on close; used for session replay on startup.

On startup, `SignalService._restore_open_signals()` loads all `TRIGGERED` / `TP1_HIT` signals from SQLite and adds them back to the watchlist. Signals older than `signal_expiry_hours` are discarded.

`SessionCoordinator._load_session()` loads today's `closed_signals` to rebuild `DedupState` and history.

### SessionStore (JSON + CSV)

- **JSON**: per-day session backup written after every trade close. Used as fallback if SQLite is unavailable.
- **CSV**: append-only trade log — one row per closed signal with all fields. Never overwritten.

### Startup Replay Priority

```
SQLite closed_signals for today   ← primary
        │  (if empty or fails)
        ▼
JSON session backup for today     ← fallback
        │  (if also empty)
        ▼
Fresh session — no history
```

---

## 15. Backtesting

`app/backtesting/backtest.py` provides a walk-forward backtest that reuses all domain logic unchanged — the same `SwingDetector`, `RejectionDetector` / `CrtDetector`, `build_signal()`, and `should_emit()` that run live.

The backtest simulates the engine's `analyze()` cadence by advancing a cursor through historical LTF candles and calling the pipeline at each step. Dedup state is tracked per-session-day with automatic rollover. All five dedup rules (A–E) apply.

Results are written as JSON + CSV and summarised by `summarize_result.py`, which produces per-symbol and aggregate statistics: win rate, total R, Sharpe-like metrics, drawdown, and pattern-level breakdowns (including `CRT_SELL`, `CRT_BUY`, `HAMMER`, `SHOOTING_STAR`).

`run_backtest_all.py` is a batch runner that iterates all configured symbols and TF pairs, spawning a backtest per combination and collecting results into a single report.

---

## 16. Extending the Engine

### Adding a New Entry Model

1. Add a new `CandlePattern` variant to `domain/entities/enums.py`.
2. Add the new detector class to `domain/market/rejection.py`. It must return `Optional[tuple[RejectionCandle, RejectionScore]]`.
3. Update `EntryModel` enum with the new value.
4. Add the new variant to `_find_entry()` in `SignalService`.
5. Add validation in `Settings.__post_init__()`.
6. Update `ENTRY_MODEL` env var docs.

The rest of the pipeline — dedup, `build_signal()`, watchlist evaluation, persistence, metrics — needs no changes because they all operate on `RejectionCandle` regardless of pattern.

### Adding a New Symbol

```python
# domain/assets/profiles.py
ASSET_CLASS_MAP["BTC/USD"] = "CRYPTO"
```

If no class override exists, the symbol inherits `Settings` defaults. Add `"CRYPTO"` to `_CLASS_OVERRIDES` to tune the whole class.

### Adding a New Asset Class Override

```python
_CLASS_OVERRIDES["CRYPTO"] = {
    "min_rr": 2.0,
    "max_rr": 0.0,       # no cap
    "signal_expiry_hours": 48.0,
}
```

### Adding a New TF Pair

```env
TF_PAIRS=1h:5min,4h:15min,1day:1h
```

No code changes needed. Each pair is fully independent. Ensure the MT5 bridge returns data for the requested intervals.

### Plugging in a New Data Provider

`MarketDataClient` (in `infrastructure/data_providers/market_data.py`) is injected into `SignalService`. To swap providers, implement the same two methods:

```python
def fetch_candles(symbol, interval, outputsize, sort) -> list[Candle]: ...
def fetch_candles_range(symbol, interval, from_ts_ms) -> list[Candle]: ...
```

Pass the new implementation at construction time.

---

## 17. Glossary

| Term | Definition |
|---|---|
| **BOS** | Break of Structure — a candle close beyond a confirmed pivot level, confirming trend direction |
| **HTF** | Higher Timeframe — the timeframe used for zone detection (e.g. 1h) |
| **LTF** | Lower Timeframe — the timeframe used for entry detection (e.g. 5min) |
| **HTF Zone** | Supply (bearish BOS) or demand (bullish BOS) zone defined by a swing candle's wick extremes |
| **LTF Range** | The most extreme LTF swing candle that formed inside the HTF swing candle's time window |
| **Rejection Candle** | The trigger candle — wicks into the LTF range and closes back out |
| **CRT** | Candle Range Theory — entry model based on a liquidity sweep (wick beyond level) followed by immediate close-back-inside |
| **Sweep** | Price trading beyond a key level only temporarily — the wick crosses but the close does not |
| **Dead Zone** | An HTF zone that has already produced a signal this session (Rule A) — no further signals from it |
| **Re-test** | Price returning to the LTF range from outside after having initially left |
| **Stale** | A rejection candle whose timestamp is more than `stale_hours` before the current analysis tick |
| **TP1** | First take-profit level — `entry + (tp2 - entry) × 0.5` (partial close) |
| **TP2** | Second take-profit level — `htf_range.tp_level` (broken swing, BOS measured-move target) |
| **RR** | Risk:Reward ratio — `abs(tp2 - entry) / abs(entry - stop_loss)` |
| **Realized RR** | Actual R gained or lost when a signal closes |
| **Circuit Breaker** | Automatic pause after `N` consecutive losses for `H` hours |
| **Session Rollover** | Midnight boundary in `SESSION_TIMEZONE` — dedup state and history reset |
| **Breakeven** | After TP1, SL effectively moves to entry — a subsequent loss becomes 0R (reported as partial win) |
| `pivot_bars` | Number of neighbours required on each side for a pivot high/low confirmation (default 1) |
| `multi_tf_independent_positions` | When true, each TF pair has its own direction lock — multiple pairs can be long simultaneously |
