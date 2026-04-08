"""
interfaces/ws/scheduler.py — adaptive two-speed candle scheduler.

Identical logic to old server/scheduler.py with one fix:
  FIX: added get_mode(symbol) → WatchMode public method.
       The engine previously accessed _schedules and _lock directly
       (private fields) to read the current mode. That coupling is gone —
       SignalEngine now calls scheduler.get_mode(symbol).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from enum import Enum, auto
from typing import Callable, Awaitable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

AnalysisCallback = Callable[[str], Awaitable[None]]


class WatchMode(Enum):
    HTF_WATCH = auto()
    LTF_WATCH = auto()


class SymbolSchedule:
    __slots__ = ("symbol", "mode", "is_running", "timer", "subscriber_ids")

    def __init__(self, symbol: str) -> None:
        self.symbol         = symbol
        self.mode           = WatchMode.HTF_WATCH
        self.is_running     = False
        self.timer: threading.Timer | None = None
        self.subscriber_ids: set[str]      = set()


class SignalScheduler:

    def __init__(
        self,
        loop:     asyncio.AbstractEventLoop,
        callback: AnalysisCallback,
        settings,   # Settings — injected, no _cfg import
    ) -> None:
        self._loop      = loop
        self._callback  = callback
        self._cfg       = settings
        self._schedules: dict[str, SymbolSchedule] = {}
        self._lock      = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_mode(self, symbol: str) -> WatchMode:
        """FIX: public accessor — replaces direct _schedules/_lock access."""
        upper = symbol.upper()
        with self._lock:
            schedule = self._schedules.get(upper)
            return schedule.mode if schedule else WatchMode.HTF_WATCH

    def set_symbol_mode(self, symbol: str, mode: WatchMode) -> None:
        upper = symbol.upper()
        with self._lock:
            if upper not in self._schedules:
                return
            schedule = self._schedules[upper]
            if schedule.mode == mode:
                return
            prev          = schedule.mode
            schedule.mode = mode
            if schedule.timer:
                schedule.timer.cancel()
                schedule.timer = None
            self._schedule_next(upper)
        logger.info("[Scheduler] %s: %s → %s", upper, prev.name, mode.name)

    def subscribe(self, subscriber_id: str, symbols: list[str]) -> None:
        with self._lock:
            for symbol in symbols:
                upper = symbol.upper()
                if upper not in self._schedules:
                    self._schedules[upper] = SymbolSchedule(upper)
                    self._schedule_next(upper)
                    logger.info("[Scheduler] Started %s in HTF_WATCH mode", upper)
                self._schedules[upper].subscriber_ids.add(subscriber_id)

    def unsubscribe(self, subscriber_id: str) -> None:
        with self._lock:
            for symbol, schedule in list(self._schedules.items()):
                schedule.subscriber_ids.discard(subscriber_id)
                if not schedule.subscriber_ids:
                    self._cancel(symbol)
                    logger.info("[Scheduler] No subscribers for %s — stopped", symbol)

    def add_symbols(self, subscriber_id: str, symbols: list[str]) -> None:
        self.subscribe(subscriber_id, symbols)

    def remove_symbols(self, subscriber_id: str, symbols: list[str]) -> None:
        with self._lock:
            for symbol in symbols:
                upper = symbol.upper()
                if upper in self._schedules:
                    self._schedules[upper].subscriber_ids.discard(subscriber_id)
                    if not self._schedules[upper].subscriber_ids:
                        self._cancel(upper)

    def get_status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "symbol":      sym,
                    "mode":        s.mode.name,
                    "subscribers": len(s.subscriber_ids),
                    "isRunning":   s.is_running,
                }
                for sym, s in self._schedules.items()
            ]

    def shutdown(self) -> None:
        with self._lock:
            for symbol in list(self._schedules.keys()):
                self._cancel(symbol)
        logger.info("[Scheduler] Shutdown complete")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _ms_until_next_boundary(self, interval_minutes: int) -> int:
        from config.settings import interval_to_minutes
        tz    = ZoneInfo(self._cfg.session_timezone)
        now_ms = int(datetime.datetime.now(tz=tz).timestamp() * 1000)
        now_s  = now_ms // 1000
        iv_s   = interval_minutes * 60
        next_s = (now_s // iv_s + 1) * iv_s
        return (next_s * 1000 - now_ms) + self._cfg.ws_candle_buffer_ms

    def _schedule_next(self, symbol: str) -> None:
        from config.settings import interval_to_minutes
        schedule = self._schedules[symbol]
        if schedule.mode == WatchMode.LTF_WATCH:
            interval = min(interval_to_minutes(ltf) for _, ltf in self._cfg.tf_pairs)
        else:
            interval = min(interval_to_minutes(htf) for htf, _ in self._cfg.tf_pairs)

        delay_ms = self._ms_until_next_boundary(interval)
        delay_s  = delay_ms / 1000
        timer    = threading.Timer(delay_s, self._run, args=(symbol,))
        timer.daemon = True
        timer.start()
        schedule.timer = timer

    def _cancel(self, symbol: str) -> None:
        schedule = self._schedules.pop(symbol, None)
        if schedule and schedule.timer:
            schedule.timer.cancel()

    def _run(self, symbol: str) -> None:
        fired_at = self._cfg.now_ms()
        with self._lock:
            if symbol not in self._schedules:
                return
            schedule = self._schedules[symbol]
            if schedule.is_running:
                logger.warning("[Scheduler] %s still running — skipping tick", symbol)
                return
            schedule.is_running = True
            mode_name = schedule.mode.name

        logger.info("[Scheduler] Tick %s (%s)", symbol, mode_name)
        future = asyncio.run_coroutine_threadsafe(self._callback(symbol), self._loop)

        def _on_done(fut: asyncio.Future) -> None:
            duration_ms = self._cfg.now_ms() - fired_at
            if exc := fut.exception():
                logger.error("[Scheduler] %s error: %s", symbol, exc)
            with self._lock:
                if symbol in self._schedules:
                    self._schedules[symbol].is_running = False
                    self._schedule_next(symbol)

        future.add_done_callback(_on_done)
