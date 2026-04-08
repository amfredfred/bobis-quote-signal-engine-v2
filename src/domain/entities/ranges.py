"""
domain/entities/ranges.py — structural range types produced by swing detection.

HTF Range:  One HTF swing candle confirmed by a Break of Structure.
            Defines the supply/demand zone box (range_high / range_low)
            and the far TP target (tp_level = the broken swing level).

LTF Range:  The most extreme LTF swing that formed INSIDE the HTF swing
            candle's time window. Entry price returns to this level.

RejectionCandle:
            A candle that tapped into the LTF range and closed back out.
            Confirms the re-test and becomes the entry bar.

No external imports — pure value objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.entities.enums import BosDirection, CandlePattern, SignalDirection


# ── HTF Range ─────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class HtfRange:
    """
    Supply/demand zone produced by a BOS-confirmed HTF swing candle.

    Geometry
    ────────
    range_high / range_low  — the swing candle's wick extremes (zone box).
    htf_candle_open         — swing candle open timestamp (== timestamp).
    htf_candle_close        — open + one HTF bar duration in ms.

    BOS target
    ──────────
    tp_level — the HTF swing level that was BROKEN to confirm the BOS.
               This is the swing-to-BOS measured-move target, producing
               avg 1:4 R:R rather than the ~1:1 you'd get from the zone edge.

    Never use range_low (SHORT) or range_high (LONG) as TP2.
    """

    range_high:      float
    range_low:       float
    bos_direction:   BosDirection
    timestamp:       int            # HTF swing candle open timestamp (ms)
    broken_at:       int   = 0      # candle that confirmed the BOS (ms)
    tp_level:        float = 0.0    # broken swing level — the real TP target
    htf_candle_open:  int  = 0      # alias of timestamp
    htf_candle_close: int  = 0      # timestamp + HTF interval in ms

    @property
    def midpoint(self) -> float:
        return (self.range_high + self.range_low) / 2.0

    @property
    def height(self) -> float:
        return self.range_high - self.range_low


# ── LTF Range ─────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class LtfRange:
    """
    The LTF swing that formed INSIDE the HTF swing candle's time window.

    sl_level / invalidation_level
    ─────────────────────────────
    SHORT → range_high  (stop above the swing high)
    LONG  → range_low   (stop below the swing low)

    A confirmed close beyond sl_level invalidates the trade.
    """

    range_high: float
    range_low:  float
    timestamp:  int
    direction:  SignalDirection

    @property
    def sl_level(self) -> float:
        return (
            self.range_high
            if self.direction == SignalDirection.SHORT
            else self.range_low
        )

    @property
    def invalidation_level(self) -> float:
        """Alias — a closed candle beyond this voids the setup."""
        return self.sl_level


# ── Rejection Candle ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class RejectionCandle:
    """
    A candle that tapped into the LTF range and closed back outside it.

    SHOOTING_STAR (SHORT): upper wick into zone, close < range_low.
    HAMMER        (LONG):  lower wick into zone, close > range_high.

    wick_tip — SL reference for the "wick" stop-placement method:
        SHOOTING_STAR → candle.high
        HAMMER        → candle.low
    """

    open:       float
    high:       float
    low:        float
    close:      float
    timestamp:  int
    wick_ratio: float
    pattern:    CandlePattern

    @property
    def wick_tip(self) -> float:
        return (
            self.high
            if self.pattern == CandlePattern.SHOOTING_STAR
            else self.low
        )
