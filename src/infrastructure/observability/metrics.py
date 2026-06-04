"""
infrastructure/observability/metrics.py — SQLite observability store.

Ported from core/metrics_collector.py.
Changes:
  - DB path derived from settings.base_dir, not a hardcoded "metrics/" folder.
  - All _cfg.* references replaced with self._cfg.* on the injected instance.
  - build_snapshot() no longer imports asset_config at call time; registry
    injected instead.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS ticks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_day TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    mode        TEXT    NOT NULL,
    fired_at    INTEGER NOT NULL,
    duration_ms REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticks_day    ON ticks(session_day);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol ON ticks(session_day, symbol);

CREATE TABLE IF NOT EXISTS signal_latency (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_day         TEXT    NOT NULL,
    signal_id           TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    tf_pair             TEXT    NOT NULL DEFAULT '',
    fired_at            INTEGER NOT NULL,
    analyze_start_ms    INTEGER NOT NULL,
    rejection_found_ms  INTEGER,
    emit_ms             INTEGER,
    broadcast_ms        INTEGER,
    scheduler_lag_ms    REAL,
    analyze_duration_ms REAL,
    total_ms            REAL
);
CREATE INDEX IF NOT EXISTS idx_lat_day    ON signal_latency(session_day);
CREATE INDEX IF NOT EXISTS idx_lat_symbol ON signal_latency(session_day, symbol);
CREATE INDEX IF NOT EXISTS idx_lat_pair   ON signal_latency(session_day, tf_pair);

CREATE TABLE IF NOT EXISTS api_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_day TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    called_at   INTEGER NOT NULL,
    duration_ms REAL    NOT NULL,
    success     INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_day    ON api_calls(session_day);
CREATE INDEX IF NOT EXISTS idx_api_symbol ON api_calls(session_day, symbol);

CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_day TEXT    NOT NULL,
    module      TEXT    NOT NULL,
    level       TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_err_day    ON errors(session_day);
CREATE INDEX IF NOT EXISTS idx_err_module ON errors(session_day, module);

CREATE TABLE IF NOT EXISTS ws_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_day TEXT    NOT NULL,
    client_id   TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    symbols     TEXT,
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_day ON ws_events(session_day);

CREATE TABLE IF NOT EXISTS metrics_counters (
    name        TEXT PRIMARY KEY,
    value       INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS metrics_gauges (
    name        TEXT PRIMARY KEY,
    value       REAL NOT NULL DEFAULT 0,
    updated_at  INTEGER
);
"""

_FLUSH_INTERVAL_SEC = 30


def _get_memory_mb() -> float:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        pass
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except ImportError:
        pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[max(0, int(len(s) * 0.95) - 1)], 2)


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


class MetricsCollector:
    """
    SQLite-backed observability store.

    Instantiate once and inject wherever metrics recording is needed.
    All _cfg references removed — settings injected at __init__.
    """

    def __init__(self, settings) -> None:
        self._cfg = settings
        self._lock = threading.Lock()
        self._start = self._cfg.now_ms()
        _DB_PATH = Path(settings.metric_dir) / "metrics.db"
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Migration — add tf_pair column if missing
        try:
            self._conn.execute(
                "ALTER TABLE signal_latency ADD COLUMN tf_pair TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()
        except Exception:
            pass
        self._conn.executescript(_DDL)
        self._conn.commit()

        self._scheduler_state: dict[str, dict] = {}
        self._ws_clients: dict[str, dict] = {}
        self._active_signals: list[dict] = []
        self._active_zones: dict[tuple, dict] = {}
        self._pending_latency: dict[str, dict] = {}
        self._pending_by_signal_id: dict[str, str] = {}
        self._tick_durations: dict[str, deque] = {}
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._flush_timer: threading.Timer | None = None
        self._restore_counter_gauge_metrics()
        self._schedule_flush()

    def _now_ms(self) -> int:
        return self._cfg.now_ms()

    def _session_day(self, ts_ms: Optional[int] = None) -> str:
        ts = (ts_ms or self._now_ms()) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()

    # ── Counters / gauges ─────────────────────────────────────────────────

    def _restore_counter_gauge_metrics(self) -> None:
        try:
            counters = self._conn.execute(
                "SELECT name, value FROM metrics_counters"
            ).fetchall()
            gauges = self._conn.execute(
                "SELECT name, value FROM metrics_gauges"
            ).fetchall()
            with self._lock:
                for name, value in counters:
                    self._counters[name] = int(value)
                for name, value in gauges:
                    self._gauges[name] = float(value)
        except Exception:
            pass

    def increment(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] += by

    def set_gauge(self, name: str, value: float | int | None) -> None:
        if value is None:
            return
        with self._lock:
            self._gauges[name] = float(value)

    def counter(self, name: str) -> int:
        with self._lock:
            return int(self._counters[name])

    def gauge(self, name: str) -> float:
        with self._lock:
            return float(self._gauges.get(name, 0.0))

    def metrics_snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }

    def flush_metrics(self) -> None:
        try:
            snap = self.metrics_snapshot()
            ts = self._now_ms()
            with self._lock:
                for name, value in snap["counters"].items():
                    self._conn.execute(
                        """
                        INSERT INTO metrics_counters (name, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            value=excluded.value,
                            updated_at=excluded.updated_at
                        """,
                        (name, int(value), ts),
                    )
                for name, value in snap["gauges"].items():
                    self._conn.execute(
                        """
                        INSERT INTO metrics_gauges (name, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            value=excluded.value,
                            updated_at=excluded.updated_at
                        """,
                        (name, float(value), ts),
                    )
                self._conn.commit()
        except Exception:
            pass

    def _schedule_flush(self) -> None:
        self._flush_timer = threading.Timer(_FLUSH_INTERVAL_SEC, self._flush_tick)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _flush_tick(self) -> None:
        self.flush_metrics()
        self._schedule_flush()

    # ── Scheduler ─────────────────────────────────────────────────────────────

    def record_tick(
        self, symbol: str, mode: str, fired_at: int, duration_ms: float
    ) -> None:
        day = self._session_day(fired_at)
        with self._lock:
            self._conn.execute(
                "INSERT INTO ticks (session_day, symbol, mode, fired_at, duration_ms) VALUES (?,?,?,?,?)",
                (day, symbol, mode, fired_at, duration_ms),
            )
            self._conn.commit()
            if symbol not in self._tick_durations:
                self._tick_durations[symbol] = deque(maxlen=100)
            self._tick_durations[symbol].append(duration_ms)
            self._counters["scanner.ticks"] += 1
            self._gauges["scanner.last_tick_duration_ms"] = float(duration_ms)
            self._gauges[f"scanner.{symbol}.last_tick_duration_ms"] = float(duration_ms)
            self._gauges[f"scanner.{symbol}.last_fired_at"] = float(fired_at)

    def update_scheduler_state(
        self,
        symbol: str,
        mode: str,
        last_fired_at: Optional[int] = None,
        next_fired_at: Optional[int] = None,
    ) -> None:
        with self._lock:
            ex = self._scheduler_state.get(symbol, {})
            self._scheduler_state[symbol] = {
                "mode": mode,
                "last_fired_at": last_fired_at or ex.get("last_fired_at"),
                "next_fired_at": next_fired_at or ex.get("next_fired_at"),
                "tick_count": ex.get("tick_count", 0) + (1 if last_fired_at else 0),
            }

    # ── Signal latency ─────────────────────────────────────────────────────────

    def signal_analyze_start(self, symbol: str, fired_at: int) -> int:
        now = self._now_ms()
        key = f"{symbol}_{fired_at}"
        with self._lock:
            self._pending_latency[key] = {
                "symbol": symbol,
                "fired_at": fired_at,
                "analyze_start_ms": now,
                "scheduler_lag_ms": now - fired_at,
            }
            self._counters["scanner.analysis_started"] += 1
            self._gauges["scanner.scheduler_lag_ms"] = float(now - fired_at)
            self._gauges[f"scanner.{symbol}.scheduler_lag_ms"] = float(now - fired_at)
        return now

    def signal_rejection_found(self, symbol: str, fired_at: int) -> None:
        key = f"{symbol}_{fired_at}"
        with self._lock:
            if key in self._pending_latency:
                self._pending_latency[key]["rejection_found_ms"] = self._now_ms()
            self._counters["signals.rejections_found"] += 1

    def signal_emitted(
        self, signal_id: str, symbol: str, fired_at: int, tf_pair: str = ""
    ) -> None:
        key = f"{symbol}_{fired_at}"
        now = self._now_ms()
        with self._lock:
            rec = self._pending_latency.get(key, {})
            rec["signal_id"] = signal_id
            rec["tf_pair"] = tf_pair
            rec["emit_ms"] = now
            start = rec.get("analyze_start_ms", now)
            rec["analyze_duration_ms"] = now - start
            self._pending_latency[key] = rec
            self._pending_by_signal_id[signal_id] = key
            self._counters["signals.emitted"] += 1
            self._gauges["latency.analysis_to_emit_ms"] = float(now - start)
            self._gauges["signals.last_emitted_at"] = float(now)

    def signal_broadcast_done(self, signal_id: str, symbol: str, fired_at: int) -> None:
        now = self._now_ms()
        with self._lock:
            key = (
                self._pending_by_signal_id.pop(signal_id, None)
                or f"{symbol}_{fired_at}"
            )
            rec = self._pending_latency.pop(key, None)
            if not rec:
                return
            rec["broadcast_ms"] = now
            rec["total_ms"] = now - rec.get("fired_at", now)
            day = self._session_day(rec.get("fired_at", fired_at))
            self._conn.execute(
                """INSERT INTO signal_latency (
                    session_day, signal_id, symbol, tf_pair, fired_at,
                    analyze_start_ms, rejection_found_ms, emit_ms, broadcast_ms,
                    scheduler_lag_ms, analyze_duration_ms, total_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    day,
                    rec.get("signal_id", ""),
                    rec["symbol"],
                    rec.get("tf_pair", ""),
                    rec["fired_at"],
                    rec.get("analyze_start_ms"),
                    rec.get("rejection_found_ms"),
                    rec.get("emit_ms"),
                    rec["broadcast_ms"],
                    rec.get("scheduler_lag_ms"),
                    rec.get("analyze_duration_ms"),
                    rec["total_ms"],
                ),
            )
            self._conn.commit()
            self._gauges["latency.signal_broadcast_total_ms"] = float(rec["total_ms"])

    # ── API calls ──────────────────────────────────────────────────────────────

    def on_api_call(
        self,
        symbol: str,
        interval: str,
        source: str,
        called_at: int,
        duration_ms: float,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Matches the metrics_fn signature expected by MarketDataClient."""
        day = self._session_day(called_at)
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_calls (session_day, symbol, interval, source, called_at, duration_ms, success, error) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    day,
                    symbol,
                    interval,
                    source,
                    called_at,
                    duration_ms,
                    int(success),
                    error,
                ),
            )
            self._conn.commit()
            self._counters["mt5.calls"] += 1
            self._counters[f"mt5.calls.{interval}"] += 1
            if not success:
                self._counters["mt5.errors"] += 1
            self._gauges["mt5.last_call_ms"] = float(duration_ms)
            self._gauges[f"mt5.{symbol}.{interval}.last_call_ms"] = float(duration_ms)

    # Keep old name for compatibility
    record_api_call = on_api_call

    # ── Errors ─────────────────────────────────────────────────────────────────

    def record_error(self, module: str, level: str, message: str) -> None:
        now = self._now_ms()
        day = self._session_day(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO errors (session_day, module, level, message, occurred_at) VALUES (?,?,?,?,?)",
                (day, module, level, message, now),
            )
            self._conn.commit()
            self._counters["errors.total"] += 1

    # ── WS clients ─────────────────────────────────────────────────────────────

    def record_ws_event(
        self, client_id: str, event: str, symbols: Optional[list[str]] = None
    ) -> None:
        import json as _json

        now = self._now_ms()
        day = self._session_day(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO ws_events (session_day, client_id, event, symbols, occurred_at) VALUES (?,?,?,?,?)",
                (day, client_id, event, _json.dumps(symbols) if symbols else None, now),
            )
            self._conn.commit()
            self._counters[f"websocket.{event}"] += 1

    def update_ws_client(self, client_id: str, symbols: list[str]) -> None:
        with self._lock:
            ex = self._ws_clients.get(client_id, {})
            self._ws_clients[client_id] = {
                "symbols": symbols,
                "connected_at": ex.get("connected_at", self._now_ms()),
            }
            self._gauges["websocket.client_count"] = float(len(self._ws_clients))

    def remove_ws_client(self, client_id: str) -> None:
        with self._lock:
            self._ws_clients.pop(client_id, None)
            self._gauges["websocket.client_count"] = float(len(self._ws_clients))

    def set_active_signals(self, signals: list[dict]) -> None:
        with self._lock:
            self._active_signals = signals
            self._gauges["signals.active_count"] = float(len(signals))

    def upsert_active_zone(self, zone: dict) -> None:
        key = (zone["symbol"], zone["direction"], zone["ltfTimestamp"])
        with self._lock:
            self._active_zones[key] = zone
            self._gauges["signals.active_zones"] = float(len(self._active_zones))

    def remove_active_zone(
        self, symbol: str, direction: str, ltf_timestamp: int
    ) -> None:
        with self._lock:
            self._active_zones.pop((symbol, direction, ltf_timestamp), None)
            self._gauges["signals.active_zones"] = float(len(self._active_zones))

    def on_signal_event(self, event, payload: dict) -> None:
        """Called by SignalService._emit via the metrics hook."""
        from domain.entities.enums import SignalEvent

        try:
            event_name = getattr(event, "value", str(event))
            self.increment(f"events.{event_name}")
            if event == SignalEvent.SIGNAL_PENDING:
                self.increment("signals.pending")
                self.upsert_active_zone(
                    {
                        "symbol": payload["symbol"],
                        "direction": payload["direction"],
                        "ltfTimestamp": payload["ltfTimestamp"],
                        "pendingAt": payload["pendingAt"],
                        "htfRange": payload["htfRange"],
                        "ltfRange": payload["ltfRange"],
                        "htfInterval": payload["htfInterval"],
                        "ltfInterval": payload["ltfInterval"],
                    }
                )
            elif event in (
                SignalEvent.SIGNAL_TRIGGERED,
                SignalEvent.SIGNAL_INVALIDATED,
                SignalEvent.SIGNAL_EXPIRED,
                SignalEvent.SIGNAL_SL_HIT,
                SignalEvent.SIGNAL_TP2_HIT,
            ):
                if event == SignalEvent.SIGNAL_TRIGGERED:
                    self.increment("signals.triggered")
                elif event == SignalEvent.SIGNAL_INVALIDATED:
                    self.increment("signals.invalidated")
                elif event == SignalEvent.SIGNAL_EXPIRED:
                    self.increment("signals.expired")
                elif event == SignalEvent.SIGNAL_SL_HIT:
                    self.increment("signals.sl_hit")
                elif event == SignalEvent.SIGNAL_TP2_HIT:
                    self.increment("signals.tp2_hit")
                sig = payload.get("signal") or payload
                ltf_ts = sig.get("ltfTimestamp") or (sig.get("ltfRange") or {}).get(
                    "timestamp", 0
                )
                self.remove_active_zone(
                    symbol=sig.get("symbol", ""),
                    direction=sig.get("direction", ""),
                    ltf_timestamp=ltf_ts,
                )
        except Exception:
            pass

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def build_snapshot(self) -> dict:
        today = self._session_day()
        now = self._now_ms()

        with self._lock:
            scheduler = dict(self._scheduler_state)
            ws_clients = dict(self._ws_clients)
            active_sig = list(self._active_signals)
            active_zones = list(self._active_zones.values())
            tick_dur = {s: list(v) for s, v in self._tick_durations.items()}
            counters = dict(self._counters)
            gauges = dict(self._gauges)

        tick_rows = self._conn.execute(
            "SELECT symbol, mode, COUNT(*), AVG(duration_ms), MAX(duration_ms) "
            "FROM ticks WHERE session_day=? GROUP BY symbol, mode",
            (today,),
        ).fetchall()
        tick_stats: dict[str, dict] = {}
        for sym, mode, cnt, avg_d, max_d in tick_rows:
            tick_stats.setdefault(sym, {})[mode] = {
                "count": cnt,
                "avg_ms": round(avg_d or 0, 2),
                "max_ms": round(max_d or 0, 2),
                "p95_ms": _p95(tick_dur.get(sym, [])),
            }

        scheduler_out = [
            {
                "symbol": sym,
                "mode": s["mode"],
                "last_fired_at": s.get("last_fired_at"),
                "next_fired_at": s.get("next_fired_at"),
                "tick_count": s.get("tick_count", 0),
                "tick_stats": tick_stats.get(sym, {}),
            }
            for sym, s in scheduler.items()
        ]

        lat = self._conn.execute(
            "SELECT AVG(scheduler_lag_ms), MAX(scheduler_lag_ms), "
            "AVG(analyze_duration_ms), MAX(analyze_duration_ms), "
            "AVG(total_ms), MAX(total_ms), COUNT(*) "
            "FROM signal_latency WHERE session_day=?",
            (today,),
        ).fetchone()
        latency = {
            "avg_scheduler_lag_ms": round(lat[0] or 0, 2),
            "max_scheduler_lag_ms": round(lat[1] or 0, 2),
            "avg_analyze_ms": round(lat[2] or 0, 2),
            "max_analyze_ms": round(lat[3] or 0, 2),
            "avg_total_ms": round(lat[4] or 0, 2),
            "max_total_ms": round(lat[5] or 0, 2),
            "signal_count": lat[6] or 0,
        }

        api_rows = self._conn.execute(
            "SELECT source, COUNT(*), SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), AVG(duration_ms) "
            "FROM api_calls WHERE session_day=? GROUP BY source",
            (today,),
        ).fetchall()
        api_stats = {
            src: {"total_calls": tot, "errors": err or 0, "avg_ms": round(avg or 0, 2)}
            for src, tot, err, avg in api_rows
        }
        calls_last_min = self._conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE called_at >= ?",
            (now - 60_000,),
        ).fetchone()[0]

        err_rows = self._conn.execute(
            "SELECT module, level, COUNT(*) FROM errors WHERE session_day=? GROUP BY module, level",
            (today,),
        ).fetchall()
        error_stats: dict[str, dict] = {}
        for module, level, cnt in err_rows:
            error_stats.setdefault(module, {})[level] = cnt
        total_errors = self._conn.execute(
            "SELECT COUNT(*) FROM errors WHERE session_day=?",
            (today,),
        ).fetchone()[0]
        recent_errors = self._conn.execute(
            "SELECT module, level, message, occurred_at FROM errors "
            "WHERE session_day=? ORDER BY occurred_at DESC LIMIT 10",
            (today,),
        ).fetchall()

        ws_out = [
            {
                "client_id": cid,
                "symbols": info["symbols"],
                "connected_at": info["connected_at"],
                "uptime_s": round((now - info["connected_at"]) / 1000),
            }
            for cid, info in ws_clients.items()
        ]

        config_out = {
            "tf_pairs": [f"{h}/{l}" for h, l in self._cfg.tf_pairs],
            "htf_lookback": self._cfg.htf_lookback,
            "pivot_bars": self._cfg.pivot_bars,
            "min_wick_ratio": self._cfg.min_wick_ratio,
            "stop_placement": self._cfg.stop_placement_method,
            "stop_buffer_pct": self._cfg.stop_buffer_pct,
            "max_sl_zone_mult": self._cfg.max_sl_zone_mult,
            "min_rr": self._cfg.min_rr,
            "max_rr": self._cfg.max_rr,
            "tp1_multiplier": self._cfg.tp1_multiplier,
            "signal_expiry_hours": self._cfg.signal_expiry_hours,
            "max_htf_zones_per_dir": self._cfg.max_htf_zones_per_dir,
            "use_trend_filter": self._cfg.use_trend_filter,
            "use_breakeven": self._cfg.use_breakeven,
            "use_invalidation": self._cfg.use_invalidation,
            "max_consecutive_losses": self._cfg.max_consecutive_losses,
            "pause_after_streak_h": self._cfg.pause_after_streak_h,
            "ws_port": self._cfg.ws_port,
            "ws_candle_buffer_ms": self._cfg.ws_candle_buffer_ms,
        }

        return {
            "ts": now,
            "system": {
                "uptime_ms": now - self._start,
                "uptime_s": round((now - self._start) / 1000),
                "memory_mb": round(_get_memory_mb(), 1),
                "python": platform.python_version(),
                "platform": platform.system(),
                "pid": os.getpid(),
                "session_day": today,
            },
            "config": config_out,
            "scheduler": scheduler_out,
            "latency": latency,
            "api": {"by_source": api_stats, "calls_last_min": calls_last_min},
            "errors": {
                "total_today": total_errors,
                "by_module": error_stats,
                "recent": [
                    {"module": r[0], "level": r[1], "message": r[2], "at": r[3]}
                    for r in recent_errors
                ],
            },
            "websocket": {"client_count": len(ws_out), "clients": ws_out},
            "active_signals": active_sig,
            "active_zones": active_zones,
            "metrics": {
                "raw_counters": counters,
                "raw_gauges": gauges,
                "scanner_ticks": counters.get("scanner.ticks", 0),
                "scanner_tick_errors": counters.get("scanner.tick_errors", 0),
                "analysis_started": counters.get("scanner.analysis_started", 0),
                "signals_pending": counters.get("signals.pending", 0),
                "signals_emitted": counters.get("signals.emitted", 0),
                "signals_triggered": counters.get("signals.triggered", 0),
                "signals_stale_skipped": counters.get("signals.stale_skipped", 0),
                "signals_dedup_blocked": counters.get("signals.dedup_blocked", 0),
                "signals_correlation_blocked": counters.get(
                    "signals.correlation_blocked", 0
                ),
                "signals_decision_blocked": counters.get("signals.decision_blocked", 0),
                "signals_no_ltf_range": counters.get("signals.no_ltf_range", 0),
                "signals_no_rejection": counters.get("signals.no_rejection", 0),
                "mt5_calls": counters.get("mt5.calls", 0),
                "mt5_errors": counters.get("mt5.errors", 0),
                "last_emit_lag_ms": gauges.get("latency.emit_lag_ms", 0),
                "last_scan_lag_ms": gauges.get("scanner.scan_lag_ms", 0),
                "last_analysis_ms": gauges.get("scanner.analysis_ms", 0),
                "active_signals": gauges.get("signals.active_count", len(active_sig)),
                "active_zones": gauges.get("signals.active_zones", len(active_zones)),
                "websocket_clients": len(ws_out),
            },
        }

    def close(self) -> None:
        if self._flush_timer:
            self._flush_timer.cancel()
        self.flush_metrics()
        with self._lock:
            self._conn.close()
