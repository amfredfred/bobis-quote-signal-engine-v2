"""
app/services/signal_service.py — signal analysis pipeline and watchlist manager.

Orchestrates:
  1. Market data fetching
  2. Swing + rejection detection
  3. Signal construction + quality gates
  4. Deduplication via SessionCoordinator
  5. Persistence
  6. Event emission

Architecture:
• app.engine.market_replay is the single source of truth for lifecycle logic.
• Live evaluation, query replay, and backtest paths share identical behavior.
"""

from __future__ import annotations

import asyncio
import bisect
import copy
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from app.engine.decision_engine import DecisionEngine
from app.engine.market_replay import StepOutcome, step_signal_state
from domain.assets.profiles import AssetProfile, AssetRegistry
from domain.entities.candle import Candle
from domain.entities.enums import (
    SignalDirection,
    SignalEvent,
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
from domain.market.swings import SwingDetector, detect_displacement
from domain.signals.entry import find_entry

logger = logging.getLogger(__name__)

EventListener = Callable[[SignalEvent, dict], None]


# ── Dependency Protocols ─────────────────────────────────────────────────────


class _MarketDataClient(Protocol):
    def fetch_candles(
        self, symbol: str, interval: str, outputsize: int
    ) -> list[Candle]: ...
    def fetch_candles_range(
        self, symbol: str, interval: str, from_ts: int, end_ts: Optional[int] = None
    ) -> list[Candle]: ...


class _Settings(Protocol):
    tf_pairs: list[tuple[str, str]]
    htf_outputsize: int
    pivot_bars: int
    max_htf_zones_per_dir: int
    max_signal_count_per_zone: int
    use_displacement_filter: bool
    displacement_atr_period: int
    signal_expiry_hours: float
    entry_model: str
    crt_mode: str
    min_wick_ratio: float

    def now_ms(self) -> int: ...
    def entry_model_for(self, htf_interval: str, ltf_interval: str) -> str: ...
    def displacement_mult_for(self, htf_interval: str, ltf_interval: str) -> float: ...


class _SessionCoordinator(Protocol):
    def should_emit(
        self,
        *,
        htf_range: object,
        ltf_range: object,
        rejection: object,
        direction: SignalDirection,
        symbol: str,
        current_ts: int,
        htf_interval: str,
        ltf_interval: str,
    ) -> tuple[bool, str]: ...
    def register_signal(
        self,
        *,
        signal_id: str,
        symbol: str,
        direction: SignalDirection,
        htf_range: object,
        ltf_range: object,
        rejection: object,
        htf_interval: str,
        ltf_interval: str,
    ) -> None: ...
    def record_outcome(self, rec: ClosedSignalRecord) -> None: ...
    def stats(self) -> dict: ...
    def status_line(self) -> str: ...


class _SignalStore(Protocol):
    def upsert_open(self, signal: TradeSignal, now: int) -> None: ...
    def delete_open(self, signal_id: str) -> None: ...
    def load_open_signals(self) -> list[dict]: ...


class _MetricsCollector(Protocol):
    def on_signal_event(self, event: SignalEvent, payload: dict) -> None: ...
    def signal_analyze_start(self, symbol: str, fired_at: int) -> None: ...
    def signal_rejection_found(self, symbol: str, fired_at: int) -> None: ...
    def signal_emitted(
        self, signal_id: str, symbol: str, fired_at: int, pair: str
    ) -> None: ...
    def set_active_signals(self, signals: list[dict]) -> None: ...
    def increment(self, name: str, by: int = 1) -> None: ...
    def set_gauge(self, name: str, value: float | int | None) -> None: ...


# ── Value Objects ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PendingKey:
    symbol: str
    htf_interval: str
    ltf_interval: str
    ltf_ts: int
    direction: str


# ── Module Helpers ───────────────────────────────────────────────────────────

_STATUS_TO_CLOSE_EVENT: dict[SignalStatus, SignalEvent] = {
    SignalStatus.EXPIRED: SignalEvent.SIGNAL_EXPIRED,
    SignalStatus.TP2_HIT: SignalEvent.SIGNAL_TP2_HIT,
    SignalStatus.INVALIDATED: SignalEvent.SIGNAL_INVALIDATED,
    SignalStatus.SL_HIT: SignalEvent.SIGNAL_SL_HIT,
}


class SignalService:
    """
    Stateful signal analysis pipeline and open-signal watchlist.
    """

    def __init__(
        self,
        market_data: _MarketDataClient,
        settings: _Settings,
        asset_registry: AssetRegistry,
        session: _SessionCoordinator,
        signal_store: _SignalStore,
        metrics: Optional[_MetricsCollector] = None,
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
        self._last_ltf_ranges: dict[tuple, dict[tuple[int, str], object]] = {}
        self._current_fired: dict[str, int] = {}
        self._pending_emitted: dict[_PendingKey, int] = {}
        self._decision_engine = DecisionEngine()

    # ── Initialization ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Restore persisted open signals."""
        loop = asyncio.get_running_loop()
        try:
            rows: list[dict] = await loop.run_in_executor(
                None, self._store.load_open_signals
            )
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
                signal.zone_attempt = self._session.register_signal(
                    signal_id=signal.id,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    htf_range=signal.htf_range,
                    ltf_range=signal.ltf_range,
                    rejection=signal.rejection_candle,
                    htf_interval=signal.htf_interval,
                    ltf_interval=signal.ltf_interval,
                )
                restored += 1
            except Exception as exc:
                logger.warning(
                    "Skipping unrestorable signal %s: %s", raw.get("id"), exc
                )

        if restored:
            if self._metrics:
                self._metrics.set_active_signals(
                    [signal.to_dict() for signal in self._watchlist.values()]
                )
            logger.info("Restored %d open signal(s)", restored)

    # ── Event System ─────────────────────────────────────────────────────────

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

    # ── Persistence ──────────────────────────────────────────────────────────

    def _persist_open_signals(self) -> None:
        now = self._cfg.now_ms()
        for signal in self._watchlist.values():
            try:
                self._store.upsert_open(signal, now)
            except Exception as exc:
                logger.error("Failed to upsert open signal %s: %s", signal.id, exc)

    # ── Analysis Pipeline ────────────────────────────────────────────────────

    async def analyze(self, symbol: str, fired_at: int = 0) -> list[TradeSignal]:
        if not fired_at:
            fired_at = self._cfg.now_ms()
        analyze_started = self._cfg.now_ms()
        if self._metrics:
            self._metrics.signal_analyze_start(symbol, fired_at)
            self._metrics.set_gauge(f"scanner.{symbol}.analysis_fired_at", fired_at)
        self._current_fired[symbol] = fired_at
        logger.info("[%s] Analyzing…  %s", symbol, self._session.status_line())

        results = await asyncio.gather(
            *[
                self._analyze_pair(symbol, htf_interval, ltf_interval, fired_at)
                for htf_interval, ltf_interval in self._cfg.tf_pairs
            ],
            return_exceptions=True,
        )

        all_new: list[TradeSignal] = []
        for (htf_interval, ltf_interval), result in zip(self._cfg.tf_pairs, results):
            if isinstance(result, Exception):
                logger.error(
                    "[%s %s/%s] _analyze_pair failed: %s",
                    symbol,
                    htf_interval,
                    ltf_interval,
                    result,
                )
            else:
                all_new.extend(result)
        if self._metrics:
            duration_ms = self._cfg.now_ms() - analyze_started
            stats = self._session.stats()
            self._metrics.set_gauge("scanner.analysis_ms", duration_ms)
            self._metrics.set_gauge(f"scanner.{symbol}.analysis_ms", duration_ms)
            self._metrics.set_gauge("state.watchlist_open", len(self._watchlist))
            self._metrics.set_gauge("state.pending_zones", len(self._pending_emitted))
            self._metrics.set_gauge("state.session_r", stats.get("session_r", 0.0))
            self._metrics.set_gauge("state.dead_zones", stats.get("dead_zones", 0))
            self._metrics.set_gauge(
                "state.open_positions", len(stats.get("open_positions", []))
            )
        return all_new

    async def _analyze_pair(
        self, symbol: str, htf_interval: str, ltf_interval: str, fired_at: int
    ) -> list[TradeSignal]:
        from config.settings import interval_to_minutes

        pair_key = (symbol, htf_interval, ltf_interval)
        pair_label = f"{symbol} {htf_interval}/{ltf_interval}"
        loop = asyncio.get_running_loop()
        profile = self._registry.get(symbol, htf_interval, ltf_interval)
        htf_interval_ms = interval_to_minutes(htf_interval) * 60 * 1000
        ltf_interval_ms = interval_to_minutes(ltf_interval) * 60 * 1000
        analysis_close = fired_at
        pair_metric = f"scanner.{symbol}.{htf_interval}_{ltf_interval}"
        entry_model = self._cfg.entry_model_for(htf_interval, ltf_interval)

        if analysis_close % ltf_interval_ms != 0:
            if self._metrics:
                self._metrics.increment("scanner.pair_skipped.awaiting_ltf_close")
                self._metrics.increment(f"{pair_metric}.skipped.awaiting_ltf_close")
            logger.debug(
                "[%s] Skipping pair until next LTF close analysis_close=%s ltf_interval_ms=%s",
                pair_label,
                self._cfg.dt_ms(analysis_close),
                ltf_interval_ms,
            )
            return []

        # analysis_close is the close boundary of the newest fully closed LTF candle.
        # Example on M5: at 10:35:01, analysis_close=10:35:00 and
        # pair_fired_at=10:30:00, the open timestamp of the latest closed candle.
        pair_fired_at = (
            (analysis_close // ltf_interval_ms) * ltf_interval_ms - ltf_interval_ms
        )
        real_now = self._cfg.now_ms()
        expected_latest_closed_open = (
            (real_now // ltf_interval_ms) * ltf_interval_ms - ltf_interval_ms
        )
        drift_ms = expected_latest_closed_open - pair_fired_at
        logger.info(
            "[%s] Signal analysis timing analysis_close=%s pair_fired_at=%s ltf_interval_ms=%s",
            pair_label,
            self._cfg.dt_ms(analysis_close),
            self._cfg.dt_ms(pair_fired_at),
            ltf_interval_ms,
        )
        if drift_ms > 0:
            if self._metrics:
                self._metrics.increment("scanner.analysis_drift_detected")
                self._metrics.set_gauge("scanner.analysis_drift_ms", drift_ms)
                self._metrics.set_gauge(f"{pair_metric}.analysis_drift_ms", drift_ms)
            logger.warning(
                "[%s] Signal analysis lag detected expected_latest_closed_open=%s actual_pair_fired_at=%s drift_ms=%s",
                pair_label,
                self._cfg.dt_ms(expected_latest_closed_open),
                self._cfg.dt_ms(pair_fired_at),
                drift_ms,
            )

        # HTF
        htf_full = await loop.run_in_executor(
            None,
            lambda: self._md.fetch_candles(
                symbol, htf_interval, self._cfg.htf_outputsize
            ),
        )
        if len(htf_full) < 10:
            if self._metrics:
                self._metrics.increment("scanner.insufficient_htf")
            logger.warning("[%s] Insufficient HTF data", pair_label)
            return []
        htf_closed_cutoff = analysis_close - htf_interval_ms
        htf_visible = [c for c in htf_full if c.timestamp <= htf_closed_cutoff]
        if len(htf_visible) < 10:
            if self._metrics:
                self._metrics.increment("scanner.insufficient_closed_htf")
            logger.warning("[%s] Insufficient closed HTF data", pair_label)
            return []
        htf = htf_visible[-profile.htf_lookback :]
        self._last_htf[pair_key] = htf

        # LTF
        ltf_all = await loop.run_in_executor(
            None,
            lambda: self._md.fetch_candles_range(
                symbol, ltf_interval, htf[0].timestamp, analysis_close
            ),
        )
        ltf_timestamps = [c.timestamp for c in ltf_all]
        hi = bisect.bisect_right(ltf_timestamps, pair_fired_at)
        ltf_visible = ltf_all[:hi]
        self._last_ltf[pair_key] = ltf_visible
        if len(ltf_visible) < 10:
            if self._metrics:
                self._metrics.increment("scanner.insufficient_ltf")
            logger.warning("[%s] Insufficient LTF data", pair_label)
            return []
        scan_lag_ms = real_now - (pair_fired_at + ltf_interval_ms)
        if self._metrics:
            self._metrics.increment("scanner.pair_scans")
            self._metrics.increment(f"{pair_metric}.scans")
            self._metrics.set_gauge("scanner.scan_lag_ms", scan_lag_ms)
            self._metrics.set_gauge(f"{pair_metric}.scan_lag_ms", scan_lag_ms)
            self._metrics.set_gauge(
                f"{pair_metric}.latest_visible_ltf_open",
                ltf_visible[-1].timestamp,
            )
            self._metrics.set_gauge(f"{pair_metric}.pair_fired_at", pair_fired_at)
        logger.info(
            "[%s] Signal scan",
            pair_label,
            extra={
                "real_now": real_now,
                "fired_at": fired_at,
                "analysis_close": analysis_close,
                "pair_fired_at": pair_fired_at,
                "latest_closed_candle_open": pair_fired_at,
                "evaluated_candle_close": pair_fired_at + ltf_interval_ms,
                "latest_visible_ltf_open": ltf_visible[-1].timestamp,
                "latest_visible_ltf_close": ltf_visible[-1].timestamp + ltf_interval_ms,
                "scan_lag_ms": scan_lag_ms,
            },
        )

        # Trend bias
        structure = None
        if profile.use_trend_filter:
            structure = MarketStructure.detect(htf, pivot_bars=self._cfg.pivot_bars)
            if structure.bias.value == "NEUTRAL":
                if self._metrics:
                    self._metrics.increment("signals.trend_blocked")
                    self._metrics.increment("signals.trend_blocked.neutral")
                logger.info("[%s] HTF bias=NEUTRAL — skipping", pair_label)
                return []

        # HTF ranges
        htf_ranges = SwingDetector.find_htf_ranges(
            htf,
            pivot_bars=self._cfg.pivot_bars,
            htf_interval_ms=htf_interval_ms,
            max_zones_per_dir=self._cfg.max_htf_zones_per_dir,
        )

        before_displacement_count = len(htf_ranges)

        # Displacement filter
        if self._cfg.use_displacement_filter:
            pair_disp_mult = self._cfg.displacement_mult_for(htf_interval, ltf_interval)
            htf_ranges = [
                z
                for z in htf_ranges
                if detect_displacement(
                    htf_visible,
                    z.broken_at,
                    atr_period=self._cfg.displacement_atr_period,
                    atr_mult=pair_disp_mult,
                )
            ]
            if self._metrics:
                blocked = before_displacement_count - len(htf_ranges)
                if blocked > 0:
                    self._metrics.increment("filters.displacement_blocked", blocked)
        if self._metrics:
            self._metrics.set_gauge(f"{pair_metric}.htf_ranges", len(htf_ranges))
        self._last_ranges[pair_key] = htf_ranges

        now = pair_fired_at
        expiry_ms = self._cfg.signal_expiry_hours * 3_600_000

        # Evict stale pending
        stale_cutoff = pair_fired_at - int(expiry_ms)
        self._pending_emitted = {
            k: v for k, v in self._pending_emitted.items() if v > stale_cutoff
        }

        # LTF range cache (single pass)
        ltf_range_cache: dict[tuple[int, str], object] = {}
        live_ltf_keys: set[tuple[int, str]] = set()

        for htf_range in htf_ranges:
            htf_key = (htf_range.timestamp, htf_range.bos_direction.value)
            zone_start = htf_range.broken_at or htf_range.timestamp
            lo = bisect.bisect_left(ltf_timestamps, zone_start)
            lr = SwingDetector.find_ltf_range(
                ltf_visible[lo:hi], htf_range, ltf_visible
            )
            ltf_range_cache[htf_key] = lr
            if lr:
                live_ltf_keys.add((lr.timestamp, lr.direction.value))

        self._last_ltf_ranges[pair_key] = {
            ts: lr for ts, lr in ltf_range_cache.items() if lr is not None
        }

        # Invalidate stale zones
        invalidated = {
            k
            for k in self._pending_emitted
            if (
                k.symbol == symbol
                and k.htf_interval == htf_interval
                and k.ltf_interval == ltf_interval
                and (k.ltf_ts, k.direction) not in live_ltf_keys
            )
        }
        for inv_key in invalidated:
            del self._pending_emitted[inv_key]
            self._emit(
                SignalEvent.SIGNAL_INVALIDATED,
                {
                    "symbol": inv_key.symbol,
                    "htfInterval": inv_key.htf_interval,
                    "ltfInterval": inv_key.ltf_interval,
                    "timestamp": inv_key.ltf_ts,
                    "direction": inv_key.direction,
                    "reason": "zone_invalidated",
                    "invalidatedAt": now,
                },
            )

        new_signals: list[TradeSignal] = []

        for htf_range in htf_ranges:
            htf_key = (htf_range.timestamp, htf_range.bos_direction.value)
            zone_start = htf_range.broken_at or htf_range.timestamp
            lo = bisect.bisect_left(ltf_timestamps, zone_start)
            ltf_zone = ltf_visible[lo:hi]

            ltf_range = ltf_range_cache.get(htf_key)
            if not ltf_range:
                if self._metrics:
                    self._metrics.increment("signals.no_ltf_range")
                logger.info(
                    "[%s] Zone htf_ts=%s dir=%s — no LTF range",
                    pair_label,
                    self._cfg.dt_ms(htf_range.timestamp),
                    htf_range.bos_direction.value,
                )
                continue
            if structure is not None and not structure.allows(
                ltf_range.direction.value
            ):
                if self._metrics:
                    self._metrics.increment("signals.trend_blocked")
                    self._metrics.increment(
                        f"signals.trend_blocked.{ltf_range.direction.value.lower()}"
                    )
                continue

            # Pending signal
            pending_key = _PendingKey(
                symbol=symbol,
                htf_interval=htf_interval,
                ltf_interval=ltf_interval,
                ltf_ts=ltf_range.timestamp,
                direction=ltf_range.direction.value,
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

            # Entry
            rej_result = find_entry(
                ltf_zone,
                ltf_range,
                htf_range,
                entry_model,
                self._cfg.min_wick_ratio,
                self._cfg.crt_mode,
            )
            if not rej_result:
                if self._metrics:
                    self._metrics.increment("signals.no_rejection")
                logger.info(
                    "[%s] Zone htf_ts=%s ltf_ts=%s dir=%s — no entry pattern",
                    pair_label,
                    self._cfg.dt_ms(htf_range.timestamp),
                    self._cfg.dt_ms(ltf_range.timestamp),
                    ltf_range.direction.value,
                )
                continue
            rejection, _ = rej_result
            if self._metrics:
                self._metrics.signal_rejection_found(symbol, fired_at)
            real_now = self._cfg.now_ms()
            max_emit_lag_ms = self._cfg.max_emit_lag_ms
            rejection_closed_at = rejection.timestamp + ltf_interval_ms
            emit_lag_ms = real_now - rejection_closed_at
            if self._metrics:
                self._metrics.set_gauge("latency.candidate_emit_lag_ms", emit_lag_ms)
                self._metrics.set_gauge(
                    f"latency.{symbol}.{htf_interval}_{ltf_interval}.candidate_emit_lag_ms",
                    emit_lag_ms,
                )
            if emit_lag_ms > max_emit_lag_ms:
                if self._metrics:
                    self._metrics.increment("signals.stale_skipped")
                    self._metrics.set_gauge("signals.last_stale_lag_ms", emit_lag_ms)
                    self._metrics.set_gauge(
                        "signals.last_stale_rejection_open", rejection.timestamp
                    )
                logger.info(
                    "[%s] Skipping stale rejection rejection_open=%s rejection_close=%s real_now=%s lag=%.1fs max_lag=%.1fs",
                    pair_label,
                    self._cfg.dt_ms(rejection.timestamp),
                    self._cfg.dt_ms(rejection_closed_at),
                    self._cfg.dt_ms(real_now),
                    emit_lag_ms / 1000,
                    max_emit_lag_ms / 1000,
                )
                continue

            allowed, reason = self._session.should_emit(
                htf_range=htf_range,
                ltf_range=ltf_range,
                rejection=rejection,
                direction=ltf_range.direction,
                symbol=symbol,
                current_ts=pair_fired_at,
                htf_interval=htf_interval,
                ltf_interval=ltf_interval,
            )
            if not allowed:
                if self._metrics:
                    self._metrics.increment("signals.dedup_blocked")
                    reason_key = reason.split(":", 1)[0].strip().lower() or "unknown"
                    self._metrics.increment(f"signals.dedup_blocked.{reason_key}")
                logger.info("[%s] Dedup blocked: %s", pair_label, reason)
                continue

            signal_id = (
                f"{symbol}_{htf_interval}_{ltf_interval}_"
                f"{rejection.timestamp}_{ltf_range.direction.value}"
            )
            if signal_id in self._watchlist:
                if self._metrics:
                    self._metrics.increment("signals.watchlist_duplicate")
                continue

            decision = self._decision_engine.evaluate_setup(
                symbol=symbol,
                htf_interval=htf_interval,
                ltf_interval=ltf_interval,
                htf_range=htf_range,
                ltf_range=ltf_range,
                rejection=rejection,
                signal_id=signal_id,
                profile=profile,
            )
            signal = decision.signal
            if signal is None:
                if self._metrics:
                    self._metrics.increment("signals.decision_blocked")
                    if decision.blocked_reason:
                        self._metrics.increment(
                            f"signals.decision_blocked.{decision.blocked_reason}"
                        )
                logger.debug("[%s] Blocked: %s", pair_label, decision.blocked_reason)
                continue

            detected_at = self._cfg.now_ms()
            signal.setup_candle_open_at = rejection.timestamp
            signal.setup_candle_close_at = rejection.timestamp + ltf_interval_ms
            signal.detected_at = detected_at
            signal.emitted_at = detected_at
            emit_lag_ms = detected_at - signal.setup_candle_close_at
            if self._metrics:
                self._metrics.set_gauge("latency.emit_lag_ms", emit_lag_ms)
                self._metrics.set_gauge("latency.last_signal_emit_lag_ms", emit_lag_ms)
                self._metrics.set_gauge(
                    "signals.last_signal_rr", signal.risk_reward_ratio
                )

            signal.zone_attempt = self._session.register_signal(
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

            try:
                self._store.upsert_open(signal, now)
            except Exception as exc:
                logger.error("Failed to persist new signal %s: %s", signal.id, exc)

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

            logger.info(
                "[%s] Signal emit",
                pair_label,
                extra={
                    "signal_id": signal.id,
                    "symbol": signal.symbol,
                    "direction": signal.direction.value,
                    "setup_candle_open_at": signal.setup_candle_open_at,
                    "setup_candle_close_at": signal.setup_candle_close_at,
                    "detected_at": signal.detected_at,
                    "emitted_at": signal.emitted_at,
                    "emit_lag_ms": emit_lag_ms,
                },
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

            break  # one signal per pair per analyze call

        return new_signals

    # ── Watchlist Update ─────────────────────────────────────────────────────

    async def update_watchlist(self, symbol: str) -> None:
        from config.settings import interval_to_minutes

        open_signals = [s for s in list(self._watchlist.values()) if s.symbol == symbol]
        if not open_signals:
            return

        loop = asyncio.get_running_loop()
        by_ltf: dict[str, list[TradeSignal]] = {}
        for s in open_signals:
            by_ltf.setdefault(s.ltf_interval, []).append(s)

        now = self._cfg.now_ms()

        async def _fetch_and_evaluate(
            ltf_interval: str, signals: list[TradeSignal]
        ) -> None:
            oldest_ts = min(s.created_at for s in signals)
            ltf_ms = interval_to_minutes(ltf_interval) * 60 * 1000
            last_closed_open = (now // ltf_ms) * ltf_ms - ltf_ms
            fetch_until = last_closed_open + ltf_ms
            try:
                candles = await loop.run_in_executor(
                    None,
                    lambda iv=ltf_interval: self._md.fetch_candles_range(
                        symbol, iv, oldest_ts, fetch_until
                    ),
                )
                candles = [c for c in candles if c.timestamp <= last_closed_open]
                if len(candles) < 2:
                    return
            except Exception as exc:
                logger.error(
                    "[%s] Price fetch failed (%s): %s", symbol, ltf_interval, exc
                )
                return

            for signal in signals:
                signal_candles = [
                    c for c in candles if c.timestamp > (signal.triggered_at or 0)
                ]
                for candle in signal_candles:
                    self._evaluate_signal(signal, candle, candle.timestamp)
                    if signal.status not in (
                        SignalStatus.TRIGGERED,
                        SignalStatus.TP1_HIT,
                    ):
                        break

        await asyncio.gather(
            *[_fetch_and_evaluate(ltf, sigs) for ltf, sigs in by_ltf.items()],
            return_exceptions=True,
        )

    # ── State Machine ────────────────────────────────────────────────────────

    @staticmethod
    def _step_signal_state(
        signal: TradeSignal, candle: Candle, profile: AssetProfile, now: int
    ) -> StepOutcome:
        """Compatibility wrapper around the canonical shared lifecycle."""
        return step_signal_state(signal, candle, profile, now)

    def _evaluate_signal(self, signal: TradeSignal, candle: Candle, now: int) -> None:
        profile = self._registry.get(
            signal.symbol, signal.htf_interval, signal.ltf_interval
        )
        prev_status = signal.status

        step = self._step_signal_state(signal, candle, profile, now)

        if step.emit_tp1:
            self._emit(
                SignalEvent.SIGNAL_TP1_HIT,
                self._update_payload(
                    signal, SignalEvent.SIGNAL_TP1_HIT, prev_status, candle.close
                ),
            )
            prev_status = SignalStatus.TP1_HIT

        if step.emit_inv_log:
            self._emit(
                SignalEvent.SIGNAL_INVALIDATED,
                self._update_payload(
                    signal, SignalEvent.SIGNAL_INVALIDATED, prev_status, candle.close
                ),
            )

        if step.terminal:
            self._close_signal(
                signal, _STATUS_TO_CLOSE_EVENT[signal.status], prev_status
            )
            return

        if step.emit_tp1:
            try:
                self._store.upsert_open(signal, now)
            except Exception as exc:
                logger.error("Failed to persist TP1 signal %s: %s", signal.id, exc)

    def _close_signal(
        self, signal: TradeSignal, event: SignalEvent, prev: SignalStatus
    ) -> None:
        assert (
            signal.realized_rr is not None
        ), f"realized_rr must be set for {signal.id}"

        self._watchlist.pop(signal.id, None)
        try:
            self._store.delete_open(signal.id)
        except Exception as exc:
            logger.warning("Failed to delete open signal %s: %s", signal.id, exc)

        rec = ClosedSignalRecord(
            signal_id=signal.id,
            symbol=signal.symbol,
            direction=signal.direction.value,
            outcome=signal.outcome.value,
            realized_rr=signal.realized_rr,
            closed_at=signal.closed_at,
            htf_ts=signal.htf_range.timestamp,
            ltf_ts=signal.ltf_range.timestamp,
            rej_ts=signal.rejection_candle.timestamp,
            entry=signal.entry_price,
            entry_ts=signal.triggered_at or signal.created_at,
            pattern=signal.rejection_candle.pattern.value,
            htf_interval=signal.htf_interval or "",
            ltf_interval=signal.ltf_interval or "",
            zone_attempt=signal.zone_attempt,
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
            signal.outcome.value,
            signal.realized_rr,
            self._session.status_line(),
        )
        self._emit(event, self._update_payload(signal, event, prev, signal.close_price))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _update_payload(
        self, signal: TradeSignal, event: SignalEvent, prev: SignalStatus, price: float
    ) -> dict:
        return {
            "event": event.value,
            "signalId": signal.id,
            "symbol": signal.symbol,
            "previousStatus": prev.value,
            "currentStatus": signal.status.value,
            "outcome": signal.outcome.value if signal.outcome else None,
            "realizedRR": signal.realized_rr,
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

    def get_armed_zones(self) -> list[dict]:
        armed: list[dict] = []
        fired_at = self._cfg.now_ms()

        for (
            symbol,
            htf_interval,
            ltf_interval,
        ), htf_ranges in self._last_ranges.items():
            ltf_range_map = self._last_ltf_ranges.get(
                (symbol, htf_interval, ltf_interval), {}
            )
            for htf_range in htf_ranges:
                lr = ltf_range_map.get(
                    (htf_range.timestamp, htf_range.bos_direction.value)
                )
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

    # ── Simulation ───────────────────────────────────────────────────────────

    def _simulate_lifecycle(self, probe: TradeSignal, candles: list[Candle]) -> None:
        """Pure replay used by query_signal_status."""
        profile = self._registry.get(
            probe.symbol, probe.htf_interval, probe.ltf_interval
        )
        for candle in candles:
            step = step_signal_state(probe, candle, profile, candle.timestamp)
            if step.terminal:
                return

    # ── Query Status ─────────────────────────────────────────────────────────

    async def query_signal_status(self, signal_dict: dict, request_id: str) -> dict:
        try:
            signal = TradeSignal.from_dict(signal_dict)
        except Exception as exc:
            return self._make_error_result(
                request_id, signal_dict, f"deserialise error: {exc}"
            )

        if not signal.ltf_interval:
            return self._make_error_result(
                request_id, signal_dict, "signal has no ltf_interval"
            )

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
            return self._make_error_result(
                request_id, signal_dict, f"candle fetch failed: {exc}"
            )

        if not candles:
            return self._make_error_result(
                request_id, signal_dict, "no candles returned"
            )

        probe = copy.deepcopy(signal)
        probe.status = SignalStatus.TRIGGERED
        probe.tp1_hit_at = None
        probe.tp2_hit_at = None
        probe.sl_hit_at = None
        probe.outcome = None
        probe.realized_rr = None
        probe.closed_at = None
        probe.close_price = None
        probe.expired_at = None
        probe.invalidated_at = None
        probe.invalidation_logged_at = None

        replay_from = signal.triggered_at or signal.created_at
        self._simulate_lifecycle(
            probe, [c for c in candles if c.timestamp > replay_from]
        )

        return {
            "requestId": request_id,
            "signalId": signal.id,
            "status": probe.status.value,
            "outcome": probe.outcome.value if probe.outcome else None,
            "realizedRR": probe.realized_rr,
            "tp1HitAt": probe.tp1_hit_at,
            "tp2HitAt": probe.tp2_hit_at,
            "slHitAt": probe.sl_hit_at,
            "closePrice": probe.close_price,
            "candlesScanned": len(candles),
        }

    def _make_error_result(self, request_id: str, signal_dict: dict, msg: str) -> dict:
        return {
            "requestId": request_id,
            "error": msg,
            "signalId": signal_dict.get("id", ""),
            "status": None,
            "outcome": None,
            "realizedRR": None,
            "tp1HitAt": None,
            "tp2HitAt": None,
            "slHitAt": None,
            "closePrice": None,
            "candlesScanned": 0,
        }
