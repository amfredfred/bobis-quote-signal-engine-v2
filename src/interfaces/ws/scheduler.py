"""
interfaces/ws/scheduler.py — candle scheduler (LTF cadence only).

WatchMode / HTF_WATCH removed: the engine now runs every symbol at LTF
cadence unconditionally. Direct MT5 access makes the old API-cost optimisation
unnecessary.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

AnalysisCallback = Callable[[str], Awaitable[None]]


class SymbolSchedule:
    __slots__ = ("symbol", "is_running", "timer", "subscriber_ids")

    def __init__(self, symbol: str) -> None:
        self.symbol         = symbol
        self.is_running     = False
        self.timer: threading.Timer | None = None
        self.subscriber_ids: set[str]      = set()


class SignalScheduler:

    def __init__(
        self,
        loop:     asyncio.AbstractEventLoop,
        callback: AnalysisCallback,
        settings,
    ) -> None:
        self._loop      = loop
        self._callback  = callback
        self._cfg       = settings
        self._schedules: dict[str, SymbolSchedule] = {}
        self._lock      = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, subscriber_id: str, symbols: list[str]) -> None:
        with self._lock:
            for symbol in symbols:
                upper = symbol.upper()
                if upper not in self._schedules:
                    self._schedules[upper] = SymbolSchedule(upper)
                    immediate = threading.Timer(0.0, self._run, args=(upper,))
                    immediate.daemon = True
                    immediate.start()
                    self._schedules[upper].timer = immediate
                    logger.info("[Scheduler] Started %s — firing immediately", upper)
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
        now_ms = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp() * 1000)
        now_s  = now_ms // 1000
        iv_s   = interval_minutes * 60
        next_s = (now_s // iv_s + 1) * iv_s
        return (next_s * 1000 - now_ms) + self._cfg.ws_candle_buffer_ms

    def _schedule_next(self, symbol: str) -> None:
        from config.settings import interval_to_minutes
        interval = min(interval_to_minutes(ltf) for _, ltf in self._cfg.tf_pairs)
        delay_s  = self._ms_until_next_boundary(interval) / 1000
        timer    = threading.Timer(delay_s, self._run, args=(symbol,))
        timer.daemon = True
        timer.start()
        self._schedules[symbol].timer = timer

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

        logger.info("[Scheduler] Tick %s", symbol)
        future = asyncio.run_coroutine_threadsafe(self._callback(symbol), self._loop)

        def _on_done(fut: asyncio.Future) -> None:
            if exc := fut.exception():
                logger.error("[Scheduler] %s error: %s", symbol, exc)
            with self._lock:
                if symbol in self._schedules:
                    self._schedules[symbol].is_running = False
                    self._schedule_next(symbol)

        future.add_done_callback(_on_done)
