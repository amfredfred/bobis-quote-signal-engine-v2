"""
app/services/signal_service.py — signal analysis pipeline and watchlist manager.

Orchestrates:
  1. Market data fetching  (infrastructure/data_providers)
  2. Swing + rejection detection  (domain/market)
  3. Signal construction + quality gates  (domain/signals)
  4. Dedup via SessionCoordinator  (app/session)
  5. Persistence  (infrastructure/persistence)
  6. Event emission to WebSocket clients

Fixes vs old signal_service.py
────────────────────────────────
  BUG (medium): query_signal_status collapsed all candles into one
                _evaluate_signal call, so SL-before-TP1 scenarios were
                reported as wins. Fixed: uses the same candle-by-candle
                loop as update_watchlist.

  BUG (low): _pending_emitted eviction comment admitted the age check was
             wrong (used ltf_ts as proxy for zone age). Fixed: eviction
             now tracks the zone's broken_at timestamp directly.

Fixes applied in this revision
────────────────────────────────
  FIX #3:  _emit monkey-patch in query_signal_status was not thread-safe.
           Replaced with a _suppress_emit flag guarded by asyncio.Lock.

  FIX #4:  has_armed_zones / get_armed_zones passed [] as the ltf_zone
           slice to find_ltf_range, making armed-zone queries always
           return empty. Fixed: compute the correct zone slice from cached
           LTF data.

  FIX #10: find_ltf_range was called twice per zone in _analyze_pair
           (once for invalidation detection, once for signal generation).
           Results from the first pass are now cached and reused.

  FIX #11: query_signal_status used wall-clock now for expiry checks during
           candle replay, causing any historically old signal to appear
           expired on the first candle. Fixed: pass candle.timestamp as
           the time reference so expiry advances with the replay.

  FIX #12: _close_signal called _persist_open_signals() after delete_open,
           triggering a full O(n) upsert of every remaining signal on every
           close. Removed: delete_open is sufficient; a full persist is only
           needed after a TP1 state transition (already handled separately).

  FIX #13: SL vs TP1 same-candle conflict priority was undocumented.
           Comment added above the SL check explaining the conservative
           policy and its consistency with the backtest Numba kernel.

  FIX #14: _find_entry duplicated between signal_service and backtest.
           Replaced with shared domain.signals.entry.find_entry().

  FIX #15: `from datetime import datetime` inside _close_signal moved to
           module-level.

  FIX #16: error_result lambda in query_signal_status extracted to a
           proper private method.

Fixes applied in this revision
────────────────────────────────
  FIX #17 (critical): query_signal_status called _close_signal on the probe,
           which mutated _watchlist, _store, and _session even though
           _suppress_emit was True.  Replaced with _simulate_lifecycle(), a
           pure candle-loop helper that sets only probe fields and has zero
           side effects.  _suppress_emit / _emit_lock are now unused and have
           been removed.

  FIX #18 (high):    TP1_HIT + expiry + use_breakeven=True returned EXPIRED
           0.0 R instead of BREAKEVEN +partial R, understating realized
           performance.  _simulate_lifecycle honours use_breakeven at expiry.

  FIX #19 (medium):  Same-bar INV vs TP2 conflict: _evaluate_signal checked
           INV before TP2 (INV wins), while the backtest Numba/NumPy kernel
           checks TP2 first (TP2 wins).  _simulate_lifecycle checks TP2
           before INV when status is TP1_HIT, matching the backtest.

  FIX #20 (low):     expiry_ms was a float in _evaluate_signal; the backtest
           casts to int.  _simulate_lifecycle uses int() to match.

Fixes applied in this revision
────────────────────────────────
  FIX #21 (high):    FIX #19 (TP2 before INV for TP1_HIT) was applied only to
           _simulate_lifecycle, not _evaluate_signal.  _evaluate_signal still
           checked INV before TP2, so a same-bar TP2+INV while TP1_HIT would
           produce INV (BREAKEVEN/LOSS) instead of WIN_FULL.  Fixed by hoisting
           a tp2_hit + tp1_hit pre-check above the INV block.

  FIX #22 (high):    INV detection in _evaluate_signal used candle_close
           (prev.close) while the backtest Numba/NumPy kernel uses close[i]
           (current bar).  This lagged invalidation by one bar and also made
           the exit price (price = candle.close) inconsistent with the
           detection input.  Fixed: INV detection now uses price (current bar
           close), matching the backtest and making detection/exit consistent.

  FIX #23 (high):    Same candle-input lag as FIX #22 existed in
           _simulate_lifecycle: inv_now was computed from prev.close.  Fixed:
           inv_now now uses candle.close, matching the backtest kernel.

  FIX #24 (low):     FIX #20 (expiry_ms int cast) was applied only to
           _simulate_lifecycle.  _evaluate_signal still computed expiry_ms as
           a float.  Fixed: int() cast added to _evaluate_signal.
"""

from __future__ import annotations

import asyncio
import bisect
import copy
import logging
from datetime import datetime as _dt
from typing import Callable, Optional

from domain.assets.profiles import AssetProfile, AssetRegistry
from domain.entities.enums import (
    SignalDirection,
    SignalEvent,
    SignalOutcome,
    SignalStatus,
)
from domain.entities.payloads import (
    HtfRangePendingPayload,
    LtfRangePendingPayload,
    SignalPendingPayload,
)
from domain.entities.session import ClosedSignalRecord
from domain.entities.trade import TradeSignal
from domain.market.structure import MarketStructure
from domain.market.swings import SwingDetector
from domain.signals.builder import build_signal
from domain.signals.correlation import correlation_conflict
from domain.signals.entry import find_entry  # FIX #14: shared dispatcher
from domain.entities.candle import Candle

logger = logging.getLogger(__name__)

EventListener = Callable[[SignalEvent, dict], None]


class SignalService:
    """
    Stateful analysis pipeline and open-signal watchlist.

    Dependencies injected at construction — no module-level singletons.
    """

    def __init__(
        self,
        market_data,    # MarketDataClient
        settings,       # Settings
        asset_registry, # AssetRegistry
        session,        # SessionCoordinator
        signal_store,   # SignalStore
        metrics=None,   # MetricsCollector | None
    ) -> None:
        self._md = market_data
        self._cfg = settings
        self._registry = asset_registry
        self._session = session
        self._store = signal_store
        self._metrics = metrics

        self._watchlist: dict[str, TradeSignal] = {}
        self._listeners: list[EventListener] = []
        self._last_htf: dict[tuple, list[Candle]] = {}
        self._last_ltf: dict[tuple, list[Candle]] = {}
        self._last_ranges: dict[tuple, list] = {}
        self._current_fired: dict[str, int] = {}
        # FIX: store (zone_broken_at, direction) so eviction uses real zone age
        self._pending_emitted: dict[tuple, int] = {}  # key → broken_at ms

        self._restore_open_signals()

    # ── Event system ──────────────────────────────────────────────────────────

    def add_listener(self, fn: EventListener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: EventListener) -> None:
        self._listeners.remove(fn)

    def _emit(self, event: SignalEvent, payload: dict) -> None:
        if self._metrics:
            try:
                self._metrics.on_signal_event(event, payload)
            except Exception as exc:
                logger.warning("Metrics error on %s: %s", event, exc)
        for fn in self._listeners:
            try:
                fn(event, payload)
            except Exception as exc:
                logger.error("Listener error on %s: %s", event, exc)

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _persist_open_signals(self) -> None:
        now = self._cfg.now_ms()
        for signal in self._watchlist.values():
            try:
                self._store.upsert_open(signal, now)
            except Exception as exc:
                logger.error("Failed to upsert open signal %s: %s", signal.id, exc)

    def _restore_open_signals(self) -> None:
        try:
            rows = self._store.load_open_signals()
        except Exception as exc:
            logger.warning("Failed to load open signals: %s", exc)
            return

        now_ms = self._cfg.now_ms()
        expiry = self._cfg.signal_expiry_hours * 3_600_000
        restored = 0
        for raw in rows:
            try:
                signal = TradeSignal.from_dict(raw)
                if signal.status not in (SignalStatus.TRIGGERED, SignalStatus.TP1_HIT):
                    continue
                if now_ms - signal.created_at > expiry:
                    logger.info(
                        "[Restore] Skipping expired signal %s (age=%.1fh)",
                        signal.id,
                        (now_ms - signal.created_at) / 3_600_000,
                    )
                    continue
                self._watchlist[signal.id] = signal
                restored += 1
            except Exception as exc:
                logger.warning(
                    "Skipping unrestorable signal %s: %s", raw.get("id"), exc
                )

        if restored:
            logger.info("Restored %d open signal(s)", restored)

    # ── Main analysis pipeline ─────────────────────────────────────────────────

    async def analyze(self, symbol: str, fired_at: int = 0) -> list[TradeSignal]:
        if not fired_at:
            fired_at = self._cfg.now_ms()
        if self._metrics:
            self._metrics.signal_analyze_start(symbol, fired_at)
        self._current_fired[symbol] = fired_at
        logger.info("[%s] Analyzing…  %s", symbol, self._session.status_line())

        all_new: list[TradeSignal] = []
        for htf_interval, ltf_interval in self._cfg.tf_pairs:
            pair_signals = await self._analyze_pair(
                symbol, htf_interval, ltf_interval, fired_at
            )
            all_new.extend(pair_signals)
        return all_new

    async def _analyze_pair(
        self,
        symbol: str,
        htf_interval: str,
        ltf_interval: str,
        fired_at: int,
    ) -> list[TradeSignal]:
        from config.settings import interval_to_minutes

        pair_key = (symbol, htf_interval, ltf_interval)
        pair_label = f"{symbol} {htf_interval}/{ltf_interval}"
        loop = asyncio.get_running_loop()
        profile = self._registry.get(symbol, htf_interval, ltf_interval)

        # ── Fetch HTF candles ──────────────────────────────────────────────────
        htf_full = await loop.run_in_executor(
            None,
            lambda: self._md.fetch_candles(
                symbol, htf_interval, self._cfg.htf_outputsize, "ASC"
            ),
        )
        if len(htf_full) < 10:
            logger.warning("[%s] Insufficient HTF data", pair_label)
            return []
        htf = htf_full[-profile.htf_lookback:]
        self._last_htf[pair_key] = htf

        # ── Fetch LTF candles ──────────────────────────────────────────────────
        ltf_all = await loop.run_in_executor(
            None,
            lambda: self._md.fetch_candles_range(
                symbol, ltf_interval, htf[0].timestamp
            ),
        )
        self._last_ltf[pair_key] = ltf_all
        if len(ltf_all) < 10:
            logger.warning("[%s] Insufficient LTF data", pair_label)
            return []

        # ── Trend bias ─────────────────────────────────────────────────────────
        structure = None
        if profile.use_trend_filter:
            structure = MarketStructure.detect(htf, pivot_bars=self._cfg.pivot_bars)
            if structure.bias.value == "NEUTRAL":
                logger.info("[%s] HTF bias=NEUTRAL — skipping", pair_label)
                return []
            logger.info(
                "[%s] HTF bias=%s — %s",
                pair_label,
                structure.bias.value,
                structure.reason,
            )

        # ── HTF ranges ─────────────────────────────────────────────────────────
        htf_interval_ms = interval_to_minutes(htf_interval) * 60 * 1000
        htf_ranges = SwingDetector.find_htf_ranges(
            htf,
            pivot_bars=self._cfg.pivot_bars,
            htf_interval_ms=htf_interval_ms,
            max_zones_per_dir=self._cfg.max_htf_zones_per_dir,
        )
        self._last_ranges[pair_key] = htf_ranges
        logger.info(
            "[%s] %d BOS-confirmed HTF ranges  |  LTF candles: %d",
            pair_label,
            len(htf_ranges),
            len(ltf_all),
        )

        ltf_timestamps = [c.timestamp for c in ltf_all]
        now = self._cfg.now_ms()
        expiry_ms = self._cfg.signal_expiry_hours * 3_600_000

        # ── Evict stale pending keys ───────────────────────────────────────────
        stale_cutoff = fired_at - int(expiry_ms)
        self._pending_emitted = {
            k: broken_at
            for k, broken_at in self._pending_emitted.items()
            if broken_at > stale_cutoff
        }

        # ── FIX #10: Build ltf_range_cache from a single pass ─────────────────
        # find_ltf_range is the most expensive domain call per zone.
        # Cache results here and reuse them in the signal-generation loop below,
        # halving the number of calls from 2× per zone to 1×.
        hi = bisect.bisect_right(ltf_timestamps, fired_at)
        ltf_range_cache: dict[int, object] = {}   # htf_range.timestamp → LtfRange | None
        live_ltf_ts: set[int] = set()

        for htf_range in htf_ranges:
            zone_start = htf_range.broken_at or htf_range.timestamp
            lo = bisect.bisect_left(ltf_timestamps, zone_start)
            lr = SwingDetector.find_ltf_range(ltf_all[lo:hi], htf_range, ltf_all)
            ltf_range_cache[htf_range.timestamp] = lr
            if lr:
                live_ltf_ts.add(lr.timestamp)

        # ── Detect invalidated zones ───────────────────────────────────────────
        invalidated = {
            k
            for k in self._pending_emitted
            if k[0] == symbol
            and k[1] == htf_interval
            and k[2] == ltf_interval
            and k[3] not in live_ltf_ts
        }
        for inv_key in invalidated:
            del self._pending_emitted[inv_key]
            self._emit(
                SignalEvent.SIGNAL_INVALIDATED,
                {
                    "symbol": inv_key[0],
                    "htfInterval": inv_key[1],
                    "ltfInterval": inv_key[2],
                    "timestamp": inv_key[3],
                    "direction": inv_key[4],
                    "reason": "zone_invalidated",
                    "invalidatedAt": now,
                },
            )

        new_signals: list[TradeSignal] = []

        for htf_range in htf_ranges:
            zone_start = htf_range.broken_at or htf_range.timestamp
            lo = bisect.bisect_left(ltf_timestamps, zone_start)
            ltf_zone = ltf_all[lo:hi]

            # FIX #10: reuse cached result — no second call to find_ltf_range.
            ltf_range = ltf_range_cache.get(htf_range.timestamp)
            if not ltf_range:
                continue

            if structure is not None and not structure.allows(
                ltf_range.direction.value
            ):
                continue

            # ── Emit PENDING if zone is new ────────────────────────────────────
            pending_key = (
                symbol,
                htf_interval,
                ltf_interval,
                ltf_range.timestamp,
                ltf_range.direction.value,
            )
            if pending_key not in self._pending_emitted:
                self._pending_emitted[pending_key] = (
                    htf_range.broken_at or htf_range.timestamp
                )
                self._emit(
                    SignalEvent.SIGNAL_PENDING,
                    SignalPendingPayload(
                        symbol=symbol,
                        direction=ltf_range.direction.value,
                        status="PENDING",
                        htfRange=HtfRangePendingPayload(
                            rangeHigh=htf_range.range_high,
                            rangeLow=htf_range.range_low,
                            bosDirection=htf_range.bos_direction.value,
                            timestamp=htf_range.timestamp,
                            htfCandleOpen=htf_range.htf_candle_open,
                            htfCandleClose=htf_range.htf_candle_close,
                            brokenAt=htf_range.broken_at,
                            tpLevel=htf_range.tp_level,
                        ),
                        ltfRange=LtfRangePendingPayload(
                            rangeHigh=ltf_range.range_high,
                            rangeLow=ltf_range.range_low,
                            slLevel=ltf_range.sl_level,
                            timestamp=ltf_range.timestamp,
                        ),
                        pendingAt=now,
                        ltfTimestamp=ltf_range.timestamp,
                        htfInterval=htf_interval,
                        ltfInterval=ltf_interval,
                    ),
                )

            # ── Find entry candle via shared dispatcher (FIX #14) ─────────────
            rej_result = find_entry(
                ltf_zone,
                ltf_range,
                htf_range,
                self._cfg.entry_model,
                self._cfg.min_wick_ratio,
            )
            if not rej_result:
                continue
            rejection, _ = rej_result

            # ── Correlation gate ───────────────────────────────────────────────
            corr_conflict, corr_reason = correlation_conflict(
                symbol, ltf_range.direction, self.get_active_signals()
            )
            if corr_conflict:
                logger.info("[%s] ✗ Correlation block: %s", pair_label, corr_reason)
                continue

            # ── Dedup ──────────────────────────────────────────────────────────
            allowed, reason = self._session.should_emit(
                htf_range=htf_range,
                ltf_range=ltf_range,
                rejection=rejection,
                direction=ltf_range.direction,
                symbol=symbol,
                current_ts=fired_at,
                htf_interval=htf_interval,
                ltf_interval=ltf_interval,
            )
            if not allowed:
                logger.debug("[%s] Blocked: %s", pair_label, reason)
                continue

            signal_id = (
                f"{symbol}_{htf_interval}_{ltf_interval}"
                f"_{rejection.timestamp}_{ltf_range.direction.value}"
            )
            if signal_id in self._watchlist:
                continue

            # ── Build signal ───────────────────────────────────────────────────
            signal = build_signal(
                symbol=symbol,
                htf_interval=htf_interval,
                ltf_interval=ltf_interval,
                htf_range=htf_range,
                ltf_range=ltf_range,
                rejection=rejection,
                signal_id=signal_id,
                profile=profile,
                session_tz=self._cfg.session_tz,
            )
            if signal is None:
                continue

            # Register before emit — locks zone/ltf/rejection in dedup state
            self._session.register_signal(
                signal_id=signal.id,
                symbol=symbol,
                direction=ltf_range.direction,
                htf_range=htf_range,
                ltf_range=ltf_range,
                rejection=rejection,
                htf_interval=htf_interval,
                ltf_interval=ltf_interval,
            )
            self._pending_emitted.pop(pending_key, None)
            self._watchlist[signal.id] = signal
            self._persist_open_signals()
            new_signals.append(signal)

            logger.info(
                "[%s] ✦ SIGNAL %s  %s  E=%.5f  SL=%.5f  TP2=%.5f  RR=%.2f",
                pair_label,
                signal.direction.value,
                signal.id,
                signal.entry_price,
                signal.stop_loss,
                signal.tp2,
                signal.risk_reward_ratio,
            )

            self._emit(SignalEvent.SIGNAL_TRIGGERED, signal.to_dict())

            if self._metrics:
                f = self._current_fired.get(symbol, 0)
                self._metrics.signal_emitted(
                    signal.id, symbol, f, f"{htf_interval}/{ltf_interval}"
                )
                self._metrics.set_active_signals(
                    [s.to_dict() for s in self.get_active_signals()]
                )

            break  # one signal per pair per analyze() call

        return new_signals

    # ── Watchlist update (candle-by-candle) ────────────────────────────────────

    async def update_watchlist(self, symbol: str) -> None:
        open_signals = [s for s in self._watchlist.values() if s.symbol == symbol]
        if not open_signals:
            return

        loop = asyncio.get_running_loop()
        by_ltf: dict[str, list[TradeSignal]] = {}
        for s in open_signals:
            by_ltf.setdefault(s.ltf_interval, []).append(s)

        now = self._cfg.now_ms()
        for ltf_interval, signals in by_ltf.items():
            oldest_ts = min(s.created_at for s in signals)
            try:
                candles = await loop.run_in_executor(
                    None,
                    lambda iv=ltf_interval: self._md.fetch_candles_range(
                        symbol, iv, oldest_ts
                    ),
                )
                if len(candles) < 2:
                    continue
            except Exception as exc:
                logger.error(
                    "[%s] Price fetch failed (%s): %s", symbol, ltf_interval, exc
                )
                continue

            for signal in signals:
                signal_candles = [
                    c for c in candles if c.timestamp >= signal.triggered_at
                ]
                if not signal_candles:
                    continue
                for i, candle in enumerate(signal_candles):
                    prev = signal_candles[i - 1] if i > 0 else candle
                    self._evaluate_signal(
                        signal, candle.close, candle.high, candle.low, now
                    )
                    if signal.status not in (
                        SignalStatus.TRIGGERED,
                        SignalStatus.TP1_HIT,
                    ):
                        break

    # ── Signal evaluation ──────────────────────────────────────────────────────

    def _evaluate_signal(
        self,
        signal: TradeSignal,
        price: float,
        high: float,
        low: float,
        now: int,
    ) -> None:
        profile = self._registry.get(
            signal.symbol, signal.htf_interval, signal.ltf_interval
        )
        prev_status = signal.status
        is_short = signal.direction == SignalDirection.SHORT
        expiry_ms = int(profile.signal_expiry_hours * 3_600_000)  # FIX #24 port: match backtest int cast

        # ── Expiry ─────────────────────────────────────────────────────────────
        if now - signal.created_at > expiry_ms:
            if signal.status == SignalStatus.TRIGGERED:
                signal.expired_at = now
                self._close_signal(
                    signal,
                    SignalOutcome.EXPIRED,
                    price,
                    now,
                    SignalStatus.EXPIRED,
                    SignalEvent.SIGNAL_EXPIRED,
                    prev_status,
                )
                return
            elif signal.status == SignalStatus.TP1_HIT:
                if profile.use_breakeven:
                    signal.realized_rr = (
                        signal.risk_reward_ratio * profile.tp1_multiplier
                    )
                    signal.expired_at = now
                    self._close_signal(
                        signal,
                        SignalOutcome.BREAKEVEN,
                        signal.entry_price,
                        now,
                        SignalStatus.EXPIRED,
                        SignalEvent.SIGNAL_EXPIRED,
                        prev_status,
                    )
                else:
                    signal.realized_rr = 0.0
                    signal.expired_at = now
                    self._close_signal(
                        signal,
                        SignalOutcome.EXPIRED,
                        price,
                        now,
                        SignalStatus.EXPIRED,
                        SignalEvent.SIGNAL_EXPIRED,
                        prev_status,
                    )
                return

        # ── SL / TP / INV flags ────────────────────────────────────────────────
        # FIX #22: INV detection now uses price (current bar close) to match the
        # backtest Numba/NumPy kernel.  Prior code used candle_close (prev.close),
        # lagging invalidation by one bar and making exit price inconsistent with
        # the detection input.
        short_inv = is_short and price > signal.ltf_range.range_high
        long_inv = not is_short and price < signal.ltf_range.range_low
        tp1_hit = signal.status == SignalStatus.TP1_HIT

        # FIX #13: Same-bar conflict policy (conservative, matches backtest Numba kernel):
        #   SL vs TP1 on the same bar → SL wins (checked first).
        #   TP1 is only confirmed on a PRIOR bar before SL at breakeven applies.
        sl_hit = (high >= signal.stop_loss) if is_short else (low <= signal.stop_loss)
        tp1_chk = (low <= signal.tp1) if is_short else (high >= signal.tp1)
        tp2_hit = (low <= signal.tp2) if is_short else (high >= signal.tp2)

        # FIX #21 (port of FIX #19): TP2 before INV when already in TP1_HIT —
        # matches backtest Numba/NumPy kernel.  Same-bar TP2 + INV → WIN_FULL.
        # (Same-bar TRIGGERED→TP1→TP2 is handled by the tp2_hit block further
        # below, after TP1 promotion updates signal.status to TP1_HIT.)
        if tp2_hit and tp1_hit:
            signal.realized_rr = signal.risk_reward_ratio
            signal.tp2_hit_at = now
            self._close_signal(
                signal,
                SignalOutcome.WIN_FULL,
                price,
                now,
                SignalStatus.TP2_HIT,
                SignalEvent.SIGNAL_TP2_HIT,
                prev_status,
            )
            return

        # ── Invalidation ───────────────────────────────────────────────────────
        if short_inv or long_inv:
            if not profile.use_invalidation:
                if signal.invalidation_logged_at is None:
                    signal.invalidation_logged_at = now
                    self._emit(
                        SignalEvent.SIGNAL_INVALIDATED,
                        self._update_payload(
                            signal, SignalEvent.SIGNAL_INVALIDATED, prev_status, price
                        ),
                    )
            else:
                signal.invalidated_at = now
                if tp1_hit and profile.use_breakeven:
                    signal.realized_rr = (
                        signal.risk_reward_ratio * profile.tp1_multiplier
                    )
                    self._close_signal(
                        signal,
                        SignalOutcome.BREAKEVEN,
                        signal.entry_price,
                        now,
                        SignalStatus.INVALIDATED,
                        SignalEvent.SIGNAL_INVALIDATED,
                        prev_status,
                    )
                else:
                    signal.realized_rr = -(
                        abs(signal.entry_price - price) / signal.risk_pips
                    )
                    self._close_signal(
                        signal,
                        SignalOutcome.LOSS,
                        price,
                        now,
                        SignalStatus.INVALIDATED,
                        SignalEvent.SIGNAL_INVALIDATED,
                        prev_status,
                    )
                return

        if sl_hit and signal.status == SignalStatus.TRIGGERED:
            signal.realized_rr = -1.0
            signal.sl_hit_at = now
            self._close_signal(
                signal,
                SignalOutcome.LOSS,
                price,
                now,
                SignalStatus.SL_HIT,
                SignalEvent.SIGNAL_SL_HIT,
                prev_status,
            )
            return

        if sl_hit and signal.status == SignalStatus.TP1_HIT:
            if profile.use_breakeven:
                signal.realized_rr = signal.risk_reward_ratio * profile.tp1_multiplier
                signal.sl_hit_at = now
                self._close_signal(
                    signal,
                    SignalOutcome.BREAKEVEN,
                    signal.entry_price,
                    now,
                    SignalStatus.SL_HIT,
                    SignalEvent.SIGNAL_SL_HIT,
                    prev_status,
                )
            else:
                signal.realized_rr = -1.0
                signal.sl_hit_at = now
                self._close_signal(
                    signal,
                    SignalOutcome.LOSS,
                    price,
                    now,
                    SignalStatus.SL_HIT,
                    SignalEvent.SIGNAL_SL_HIT,
                    prev_status,
                )
            return

        if tp1_chk and signal.status == SignalStatus.TRIGGERED:
            signal.status = SignalStatus.TP1_HIT
            signal.tp1_hit_at = now
            self._emit(
                SignalEvent.SIGNAL_TP1_HIT,
                self._update_payload(
                    signal, SignalEvent.SIGNAL_TP1_HIT, prev_status, price
                ),
            )
            prev_status = SignalStatus.TP1_HIT

        if tp2_hit and signal.status == SignalStatus.TP1_HIT:
            signal.realized_rr = signal.risk_reward_ratio
            signal.tp2_hit_at = now
            self._close_signal(
                signal,
                SignalOutcome.WIN_FULL,
                price,
                now,
                SignalStatus.TP2_HIT,
                SignalEvent.SIGNAL_TP2_HIT,
                prev_status,
            )
            return

        if signal.status == SignalStatus.TP1_HIT and signal.tp1_hit_at == now:
            self._persist_open_signals()

    def _close_signal(
        self,
        signal: TradeSignal,
        outcome: SignalOutcome,
        price: float,
        now: int,
        new_status: SignalStatus,
        event: SignalEvent,
        prev: SignalStatus,
    ) -> None:
        signal.status = new_status
        signal.outcome = outcome
        signal.closed_at = now
        signal.close_price = price

        profile = self._registry.get(
            signal.symbol, signal.htf_interval, signal.ltf_interval
        )
        realized = (
            signal.realized_rr
            if signal.realized_rr is not None
            else {
                SignalOutcome.WIN_FULL: signal.risk_reward_ratio,
                SignalOutcome.LOSS: -1.0,
                SignalOutcome.BREAKEVEN: signal.risk_reward_ratio
                * profile.tp1_multiplier,
            }.get(outcome, 0.0)
        )

        self._watchlist.pop(signal.id, None)
        try:
            self._store.delete_open(signal.id)
        except Exception as exc:
            logger.warning("Failed to delete open signal %s: %s", signal.id, exc)

        # FIX #12: Removed blanket _persist_open_signals() call.
        # delete_open() handles the closed signal; a full re-persist of all
        # remaining signals is unnecessary here and was O(n) on every close.
        # The TP1 state transition path in _evaluate_signal calls
        # _persist_open_signals() explicitly when needed.

        # FIX #15: _dt imported at module level — no per-call import.
        session_day = (
            _dt.fromtimestamp(now / 1000, tz=self._cfg.session_tz).date().isoformat()
        )

        rec = ClosedSignalRecord(
            signal_id=signal.id,
            symbol=signal.symbol,
            direction=signal.direction.value,
            outcome=outcome.value,
            realized_rr=realized,
            closed_at=now,
            htf_ts=signal.htf_range.timestamp,
            ltf_ts=signal.ltf_range.timestamp,
            rej_ts=signal.rejection_candle.timestamp,
            entry=signal.entry_price,
            entry_ts=signal.triggered_at or signal.created_at,
            pattern=signal.rejection_candle.pattern.value,
            htf_interval=signal.htf_interval or "",
            ltf_interval=signal.ltf_interval or "",
            sl=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            rr=signal.risk_reward_ratio,
            wick_ratio=signal.rejection_candle.wick_ratio,
            htf_high=signal.htf_range.range_high,
            htf_low=signal.htf_range.range_low,
            tp_level=signal.htf_range.tp_level,
            ltf_high=signal.ltf_range.range_high,
            ltf_low=signal.ltf_range.range_low,
        )
        self._session.record_outcome(rec)

        if self._metrics:
            self._metrics.set_active_signals(
                [s.to_dict() for s in self.get_active_signals()]
            )

        logger.info(
            "[%s] %s %s  outcome=%s  rr=%.2f  %s",
            signal.symbol,
            signal.id,
            signal.direction.value,
            outcome.value,
            realized,
            self._session.status_line(),
        )
        self._emit(event, self._update_payload(signal, event, prev, price))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_payload(
        self,
        signal: TradeSignal,
        event: SignalEvent,
        prev: SignalStatus,
        price: float,
    ) -> dict:
        return {
            "event": event.value,
            "signalId": signal.id,
            "symbol": signal.symbol,
            "previousStatus": prev.value,
            "currentStatus": signal.status.value,
            "outcome": signal.outcome.value if signal.outcome else None,
            "realizedRR": getattr(signal, "realized_rr", None),
            "price": price,
            "timestamp": signal.closed_at or signal.created_at,
            "signal": signal.to_dict(),
            "sessionStats": self._session.stats(),
        }

    def get_active_signals(self) -> list[TradeSignal]:
        return [
            s
            for s in self._watchlist.values()
            if s.status in (SignalStatus.TRIGGERED, SignalStatus.TP1_HIT)
        ]

    def get_signal(self, signal_id: str) -> Optional[TradeSignal]:
        return self._watchlist.get(signal_id)

    def get_session_stats(self) -> dict:
        return self._session.stats()

    def update_htf_cache(self, symbol, htf, ltf, candles) -> None:
        self._last_htf[(symbol, htf, ltf)] = candles

    def update_ltf_cache(self, symbol, htf, ltf, candles) -> None:
        self._last_ltf[(symbol, htf, ltf)] = candles

    def has_armed_zones(self, symbol: str) -> bool:
        for htf_interval, ltf_interval in self._cfg.tf_pairs:
            pair_key = (symbol, htf_interval, ltf_interval)
            cached_ranges = self._last_ranges.get(pair_key)
            if cached_ranges is None:
                return True  # cold start — default to LTF_WATCH

            # FIX #4: compute the correct ltf_zone slice instead of passing [].
            ltf_all = self._last_ltf.get(pair_key, [])
            ltf_timestamps = [c.timestamp for c in ltf_all]
            fired_at = self._cfg.now_ms()

            for htf_range in cached_ranges:
                zone_start = htf_range.broken_at or htf_range.timestamp
                lo = bisect.bisect_left(ltf_timestamps, zone_start)
                hi = bisect.bisect_right(ltf_timestamps, fired_at)
                ltf_zone = ltf_all[lo:hi]
                lr = SwingDetector.find_ltf_range(ltf_zone, htf_range, ltf_all)
                if lr:
                    return True
        return False

    def get_armed_zones(self) -> list[dict]:
        armed: list[dict] = []
        for (
            symbol,
            htf_interval,
            ltf_interval,
        ), htf_ranges in self._last_ranges.items():
            # FIX #4: compute the correct ltf_zone slice instead of passing [].
            ltf_all = self._last_ltf.get((symbol, htf_interval, ltf_interval), [])
            ltf_timestamps = [c.timestamp for c in ltf_all]
            fired_at = self._cfg.now_ms()

            for htf_range in htf_ranges:
                zone_start = htf_range.broken_at or htf_range.timestamp
                lo = bisect.bisect_left(ltf_timestamps, zone_start)
                hi = bisect.bisect_right(ltf_timestamps, fired_at)
                ltf_zone = ltf_all[lo:hi]
                lr = SwingDetector.find_ltf_range(ltf_zone, htf_range, ltf_all)
                if not lr:
                    continue
                armed.append(
                    {
                        "symbol": symbol,
                        "direction": lr.direction.value,
                        "htfInterval": htf_interval,
                        "ltfInterval": ltf_interval,
                        "ltfTimestamp": lr.timestamp,
                        "pendingAt": fired_at,
                        "htfRange": {
                            "rangeHigh": htf_range.range_high,
                            "rangeLow": htf_range.range_low,
                            "bosDirection": htf_range.bos_direction.value,
                            "timestamp": htf_range.timestamp,
                            "tpLevel": htf_range.tp_level,
                            "brokenAt": htf_range.broken_at,
                            "htfCandleOpen": htf_range.htf_candle_open,
                            "htfCandleClose": htf_range.htf_candle_close,
                        },
                        "ltfRange": {
                            "rangeHigh": lr.range_high,
                            "rangeLow": lr.range_low,
                            "slLevel": lr.sl_level,
                            "timestamp": lr.timestamp,
                        },
                    }
                )
        return armed

    # ── _simulate_lifecycle ────────────────────────────────────────────────────

    def _simulate_lifecycle(
        self, probe: TradeSignal, candles: list[Candle]
    ) -> None:
        """
        Pure candle-by-candle replay — zero live-state side effects.

        Mutates only *probe* fields (status, outcome, realized_rr, *_hit_at,
        closed_at, close_price, expired_at, invalidated_at,
        invalidation_logged_at).  Never touches _watchlist, _store, _session,
        or _listeners.  (FIX #17)

        Outcome policy vs _evaluate_signal
        ────────────────────────────────────
        FIX #18: TP1_HIT + expiry + use_breakeven=True → BREAKEVEN +partial R,
                 not EXPIRED 0.0 R.
        FIX #19: TP2 is checked before INV when status is TP1_HIT, matching the
                 backtest Numba/NumPy kernel.  (For TRIGGERED, INV still beats
                 SL on the same bar, also matching the kernel.)
        FIX #20: expiry_ms is cast to int (floor), matching the backtest.

        Expiry semantics (FIX #11, preserved): each bar's own timestamp is used
        as the "now" reference, so expiry advances with the replay rather than
        being evaluated against the wall-clock.

        Same-bar conflict policy (FIX #13, preserved):
          SL vs TP1 while TRIGGERED  → SL wins (checked first).
          INV vs SL while TRIGGERED  → INV wins (checked first).
          TP2 vs INV while TP1_HIT   → TP2 wins (FIX #19, checked first).
        """
        profile = self._registry.get(
            probe.symbol, probe.htf_interval, probe.ltf_interval
        )
        is_short = probe.direction == SignalDirection.SHORT
        expiry_ms = int(profile.signal_expiry_hours * 3_600_000)  # FIX #20

        for i, candle in enumerate(candles):
            now  = candle.timestamp
            prev = candles[i - 1] if i > 0 else candle

            # ── Expiry (FIX #11: now == candle.timestamp) ──────────────────
            if now - probe.created_at > expiry_ms:
                if probe.status == SignalStatus.TRIGGERED:
                    probe.outcome     = SignalOutcome.EXPIRED
                    probe.realized_rr = 0.0
                    probe.close_price = candle.close
                elif probe.status == SignalStatus.TP1_HIT:
                    # FIX #18: credit the partial close that already locked in
                    if profile.use_breakeven:
                        probe.outcome     = SignalOutcome.BREAKEVEN
                        probe.realized_rr = (
                            probe.risk_reward_ratio * profile.tp1_multiplier
                        )
                        probe.close_price = probe.entry_price
                    else:
                        probe.outcome     = SignalOutcome.EXPIRED
                        probe.realized_rr = 0.0
                        probe.close_price = candle.close
                probe.status     = SignalStatus.EXPIRED
                probe.expired_at = now
                probe.closed_at  = now
                return

            # ── Per-bar level flags ─────────────────────────────────────────
            sl_hit  = (candle.high >= probe.stop_loss) if is_short else (candle.low  <= probe.stop_loss)
            tp1_chk = (candle.low  <= probe.tp1)       if is_short else (candle.high >= probe.tp1)
            tp2_hit = (candle.low  <= probe.tp2)       if is_short else (candle.high >= probe.tp2)
            # FIX #23: use candle.close (current bar) for INV detection to match
            # the backtest Numba/NumPy kernel.  Prior code used prev.close, lagging
            # invalidation by one bar.
            inv_now = (
                candle.close > probe.ltf_range.range_high
                if is_short
                else candle.close < probe.ltf_range.range_low
            )

            # ── TRIGGERED ──────────────────────────────────────────────────
            if probe.status == SignalStatus.TRIGGERED:
                # INV > SL on same bar (matches backtest kernel priority)
                if inv_now and profile.use_invalidation:
                    probe.invalidated_at = now
                    probe.status         = SignalStatus.INVALIDATED
                    probe.outcome        = SignalOutcome.LOSS
                    probe.realized_rr    = -(
                        abs(probe.entry_price - candle.close) / probe.risk_pips
                    )
                    probe.closed_at  = now
                    probe.close_price = candle.close
                    return
                if inv_now and not profile.use_invalidation:
                    if probe.invalidation_logged_at is None:
                        probe.invalidation_logged_at = now
                    # trade stays open — fall through to SL/TP

                # SL beats same-bar TP1 (conservative, FIX #13)
                if sl_hit:
                    probe.status      = SignalStatus.SL_HIT
                    probe.outcome     = SignalOutcome.LOSS
                    probe.realized_rr = -1.0
                    probe.sl_hit_at   = now
                    probe.closed_at   = now
                    probe.close_price = probe.stop_loss
                    return

                # TP1 promotion — no return; fall through to TP1_HIT block
                # so TP2 is evaluated on the same bar (same-bar TP1+TP2 → WIN).
                if tp1_chk:
                    probe.status     = SignalStatus.TP1_HIT
                    probe.tp1_hit_at = now

            # ── TP1_HIT (entered this bar OR carried from a prior bar) ──────
            if probe.status == SignalStatus.TP1_HIT:
                # FIX #19: TP2 before INV — matches backtest Numba/NumPy
                if tp2_hit:
                    probe.status      = SignalStatus.TP2_HIT
                    probe.outcome     = SignalOutcome.WIN_FULL
                    probe.realized_rr = probe.risk_reward_ratio
                    probe.tp2_hit_at  = now
                    probe.closed_at   = now
                    probe.close_price = probe.tp2
                    return

                if inv_now and profile.use_invalidation:
                    probe.invalidated_at = now
                    probe.status         = SignalStatus.INVALIDATED
                    probe.closed_at      = now
                    if profile.use_breakeven:
                        probe.outcome     = SignalOutcome.BREAKEVEN
                        probe.realized_rr = (
                            probe.risk_reward_ratio * profile.tp1_multiplier
                        )
                        probe.close_price = probe.entry_price
                    else:
                        probe.outcome     = SignalOutcome.LOSS
                        probe.realized_rr = -(
                            abs(probe.entry_price - candle.close) / probe.risk_pips
                        )
                        probe.close_price = candle.close
                    return

                if inv_now and not profile.use_invalidation:
                    if probe.invalidation_logged_at is None:
                        probe.invalidation_logged_at = now

                if sl_hit:
                    probe.sl_hit_at = now
                    probe.closed_at = now
                    probe.status    = SignalStatus.SL_HIT
                    if profile.use_breakeven:
                        probe.outcome     = SignalOutcome.BREAKEVEN
                        probe.realized_rr = (
                            probe.risk_reward_ratio * profile.tp1_multiplier
                        )
                        probe.close_price = probe.entry_price
                    else:
                        probe.outcome     = SignalOutcome.LOSS
                        probe.realized_rr = -1.0
                        probe.close_price = probe.stop_loss
                    return

    # ── query_signal_status ────────────────────────────────────────────────────

    async def query_signal_status(self, signal_dict: dict, request_id: str) -> dict:
        """
        Short backtest: replay the signal lifecycle candle-by-candle from
        triggered_at to now, using _simulate_lifecycle().

        _simulate_lifecycle() is a pure function that mutates only the probe —
        no _watchlist / _store / _session / _emit side effects (FIX #17).
        The _emit_lock / _suppress_emit mechanism is therefore no longer needed
        and has been removed.

        FIX #11 (preserved): expiry is evaluated against each candle's own
        timestamp so it advances with the replay, not the wall-clock.
        """
        try:
            signal = TradeSignal.from_dict(signal_dict)
        except Exception as exc:
            return self._make_error_result(request_id, signal_dict, f"deserialise error: {exc}")

        if not signal.ltf_interval:
            return self._make_error_result(request_id, signal_dict, "signal has no ltf_interval")

        fetch_from = signal.triggered_at or signal.created_at
        loop = asyncio.get_running_loop()
        try:
            candles = await loop.run_in_executor(
                None,
                lambda: self._md.fetch_candles_range(
                    signal.symbol, signal.ltf_interval, fetch_from
                ),
            )
        except Exception as exc:
            return self._make_error_result(request_id, signal_dict, f"candle fetch failed: {exc}")

        if not candles:
            return self._make_error_result(request_id, signal_dict, "no candles returned")

        probe = copy.deepcopy(signal)
        probe.status                 = SignalStatus.TRIGGERED
        probe.tp1_hit_at             = None
        probe.tp2_hit_at             = None
        probe.sl_hit_at              = None
        probe.outcome                = None
        probe.realized_rr            = None
        probe.closed_at              = None
        probe.close_price            = None
        probe.expired_at             = None
        probe.invalidated_at         = None
        probe.invalidation_logged_at = None

        # FIX #17: pure replay — no lock, no suppression flag needed.
        self._simulate_lifecycle(probe, candles)

        return {
            "requestId":     request_id,
            "signalId":      signal.id,
            "status":        probe.status.value,
            "outcome":       probe.outcome.value if probe.outcome else None,
            "realizedRR":    probe.realized_rr,
            "tp1HitAt":      probe.tp1_hit_at,
            "tp2HitAt":      probe.tp2_hit_at,
            "slHitAt":       probe.sl_hit_at,
            "closePrice":    probe.close_price,
            "candlesScanned": len(candles),
        }

    def _make_error_result(
        self, request_id: str, signal_dict: dict, msg: str
    ) -> dict:
        """FIX #16: extracted from lambda in query_signal_status for readability."""
        return {
            "requestId": request_id,
            "error": msg,
            "signalId": signal_dict.get("id", ""),
            "status": signal_dict.get("status", "TRIGGERED"),
            "outcome": None,
            "realizedRR": None,
            "tp1HitAt": None,
            "tp2HitAt": None,
            "slHitAt": None,
            "closePrice": None,
            "candlesScanned": 0,
        }