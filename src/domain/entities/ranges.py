"""
domain/entities/ranges.py — structural range types produced by swing detection.

HTF Range:  One HTF swing candle confirmed by a Break of Structure.
            Defines the supply/demand zone box (range_high / range_low)
            and the far TP target (tp_level = the broken swing level).

RejectionCandle:
            A candle that tapped into the HTF zone and closed back out
            via a CRT sweep of the previous candle's range.

No external imports — pure value objects.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    @property
    def signal_direction(self) -> SignalDirection:
        return (
            SignalDirection.SHORT
            if self.bos_direction == BosDirection.BEARISH
            else SignalDirection.LONG
        )


# ── Rejection Candle ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class RejectionCandle:
    """
    A candle that swept the previous candle's range and closed back inside it
    (CRT entry pattern).

    CRT_SELL (SHORT): wick sweeps above prev high, close returns below it.
    CRT_BUY  (LONG):  wick sweeps below prev low,  close returns above it.

    wick_tip — SL reference for the wick stop-placement method:
        CRT_SELL → candle.high
        CRT_BUY  → candle.low
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
        if self.pattern in (CandlePattern.SHOOTING_STAR, CandlePattern.CRT_SELL):
            return self.high
        return self.low
