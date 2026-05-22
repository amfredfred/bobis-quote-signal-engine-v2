"""
domain/entities/trade.py — TradeSignal: the fully-qualified trade setup.

tp2 = htf_range.tp_level  (BOS target swing — NOT the zone edge)
tp1 = halfway between entry and tp2 (partial close level)

Serialisation lives here so every layer uses the same wire format.
No config imports — tz/formatting concerns stay in the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from domain.entities.enums import (
    BosDirection,
    CandlePattern,
    SignalDirection,
    SignalOutcome,
    SignalStatus,
)
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle


def _interval_to_ms(interval: str) -> int:
    s = interval.strip().lower()
    if s.endswith("min"):
        return int(s[:-3]) * 60 * 1000
    if s.endswith("h"):
        return int(s[:-1]) * 60 * 60 * 1000
    if s.endswith("day"):
        return int(s[:-3]) * 24 * 60 * 60 * 1000
    if s.endswith("week"):
        return int(s[:-4]) * 7 * 24 * 60 * 60 * 1000
    if s.endswith("month"):
        return int(s[:-5]) * 30 * 24 * 60 * 60 * 1000
    return 0


@dataclass
class TradeSignal:
    """
    A fully-qualified trade setup emitted by the signal engine.

    Mutable by design — the watchlist manager updates status, hit timestamps,
    and realized_rr in-place as the trade progresses through its lifecycle.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    id:        str
    symbol:    str
    direction: SignalDirection
    status:    SignalStatus

    # ── Levels ────────────────────────────────────────────────────────────────
    entry_price: float
    stop_loss:   float
    tp1:         float          # 50% of the way to tp2
    tp2:         float          # == htf_range.tp_level (BOS measured-move target)

    # ── Structure ─────────────────────────────────────────────────────────────
    htf_range:        HtfRange
    ltf_range:        LtfRange
    rejection_candle: RejectionCandle

    # ── Risk metrics ─────────────────────────────────────────────────────────
    risk_reward_ratio: float    # abs(tp2 - entry) / abs(entry - stop_loss)
    risk_pips:         float    # abs(entry - stop_loss)

    # ── Timeframe pair ────────────────────────────────────────────────────────
    htf_interval: str = "1h"
    ltf_interval: str = "5min"

    # ── Lifecycle timestamps (ms UTC) ─────────────────────────────────────────
    created_at:   int           = 0
    pending_at:   Optional[int] = None
    triggered_at: Optional[int] = None
    detected_at:  Optional[int] = None
    emitted_at:   Optional[int] = None
    tp1_hit_at:   Optional[int] = None
    tp2_hit_at:   Optional[int] = None
    sl_hit_at:    Optional[int] = None

    # invalidated_at         — trade closed because price crossed LTF range
    #                          (USE_INVALIDATION=True)
    # invalidation_logged_at — price crossed but trade kept open
    #                          (USE_INVALIDATION=False; only SL/TP closes it)
    invalidated_at:         Optional[int] = None
    invalidation_logged_at: Optional[int] = None

    expired_at:  Optional[int] = None
    closed_at:   Optional[int] = None

    # ── Result ────────────────────────────────────────────────────────────────
    outcome:     Optional[SignalOutcome] = None
    realized_rr: Optional[float]        = None
    close_price: Optional[float]        = None

    # ── Optional chart artefacts ──────────────────────────────────────────────
    chart_path: Optional[str]  = None
    chart_data: Optional[dict] = None

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        rejection_candle_close_at = self.rejection_candle.timestamp + _interval_to_ms(
            self.ltf_interval
        )
        return {
            "id":              self.id,
            "symbol":          self.symbol,
            "direction":       self.direction.value,
            "status":          self.status.value,
            "entryPrice":      self.entry_price,
            "stopLoss":        self.stop_loss,
            "tp1":             self.tp1,
            "tp2":             self.tp2,
            "riskRewardRatio": round(self.risk_reward_ratio, 4),
            "riskPips":        round(self.risk_pips, 6),
            "htfInterval":     self.htf_interval,
            "ltfInterval":     self.ltf_interval,
            "htfRange": {
                "rangeHigh":      self.htf_range.range_high,
                "rangeLow":       self.htf_range.range_low,
                "bosDirection":   self.htf_range.bos_direction.value,
                "timestamp":      self.htf_range.timestamp,
                "brokenAt":       self.htf_range.broken_at,
                "tpLevel":        self.htf_range.tp_level,
                "midpoint":       self.htf_range.midpoint,
                "height":         round(self.htf_range.height, 6),
                "htfCandleOpen":  self.htf_range.htf_candle_open,
                "htfCandleClose": self.htf_range.htf_candle_close,
            },
            "ltfRange": {
                "rangeHigh": self.ltf_range.range_high,
                "rangeLow":  self.ltf_range.range_low,
                "timestamp": self.ltf_range.timestamp,
                "direction": self.ltf_range.direction.value,
                "slLevel":   self.ltf_range.sl_level,
            },
            "rejectionCandle": {
                "open":      self.rejection_candle.open,
                "high":      self.rejection_candle.high,
                "low":       self.rejection_candle.low,
                "close":     self.rejection_candle.close,
                "timestamp": self.rejection_candle.timestamp,
                "closeAt":   rejection_candle_close_at,
                "wickRatio": round(self.rejection_candle.wick_ratio, 4),
                "pattern":   self.rejection_candle.pattern.value,
                "wickTip":   self.rejection_candle.wick_tip,
            },
            "createdAt":            self.created_at,
            "pendingAt":            self.pending_at,
            "triggeredAt":          self.triggered_at,
            "detectedAt":           self.detected_at,
            "emittedAt":            self.emitted_at,
            "rejectionCandleCloseAt": rejection_candle_close_at,
            "tp1HitAt":             self.tp1_hit_at,
            "tp2HitAt":             self.tp2_hit_at,
            "slHitAt":              self.sl_hit_at,
            "invalidatedAt":        self.invalidated_at,
            "invalidationLoggedAt": self.invalidation_logged_at,
            "expiredAt":            self.expired_at,
            "closedAt":             self.closed_at,
            "outcome":              self.outcome.value if self.outcome else None,
            "realizedRR":           self.realized_rr,
            "closePrice":           self.close_price,
            "chartPath":            self.chart_path,
            "chartData":            self.chart_data,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TradeSignal:
        hr = d["htfRange"]
        lr = d["ltfRange"]
        rc = d["rejectionCandle"]
        return cls(
            id                     = d["id"],
            symbol                 = d["symbol"],
            direction              = SignalDirection(d["direction"]),
            status                 = SignalStatus(d["status"]),
            entry_price            = d["entryPrice"],
            stop_loss              = d["stopLoss"],
            tp1                    = d["tp1"],
            tp2                    = d["tp2"],
            risk_reward_ratio      = d["riskRewardRatio"],
            risk_pips              = d["riskPips"],
            htf_interval           = d.get("htfInterval", "1h"),
            ltf_interval           = d.get("ltfInterval", "5min"),
            htf_range = HtfRange(
                range_high       = hr["rangeHigh"],
                range_low        = hr["rangeLow"],
                bos_direction    = BosDirection(hr["bosDirection"]),
                timestamp        = hr["timestamp"],
                broken_at        = hr.get("brokenAt")       or 0,
                tp_level         = hr.get("tpLevel")        or 0.0,
                htf_candle_open  = hr.get("htfCandleOpen")  or 0,
                htf_candle_close = hr.get("htfCandleClose") or 0,
            ),
            ltf_range = LtfRange(
                range_high = lr["rangeHigh"],
                range_low  = lr["rangeLow"],
                timestamp  = lr["timestamp"],
                direction  = SignalDirection(lr["direction"]),
            ),
            rejection_candle = RejectionCandle(
                open       = rc["open"],
                high       = rc["high"],
                low        = rc["low"],
                close      = rc["close"],
                timestamp  = rc["timestamp"],
                wick_ratio = rc["wickRatio"],
                pattern    = CandlePattern(rc["pattern"]),
            ),
            created_at             = d["createdAt"],
            pending_at             = d.get("pendingAt"),
            triggered_at           = d.get("triggeredAt"),
            detected_at            = d.get("detectedAt"),
            emitted_at             = d.get("emittedAt"),
            tp1_hit_at             = d.get("tp1HitAt"),
            tp2_hit_at             = d.get("tp2HitAt"),
            sl_hit_at              = d.get("slHitAt"),
            invalidated_at         = d.get("invalidatedAt"),
            invalidation_logged_at = d.get("invalidationLoggedAt"),
            expired_at             = d.get("expiredAt"),
            closed_at              = d.get("closedAt"),
            outcome    = SignalOutcome(d["outcome"]) if d.get("outcome") else None,
            realized_rr = d.get("realizedRR"),
            close_price = d.get("closePrice"),
            chart_path  = d.get("chartPath"),
            chart_data  = d.get("chartData"),
        )
