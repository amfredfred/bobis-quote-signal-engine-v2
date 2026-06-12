"""
domain/market/rejection.py — CRT (Candle Range Theory) entry detection.

Pure domain logic — no config import, no external deps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domain.entities.candle import Candle
from domain.entities.enums import CandlePattern, SignalDirection
from domain.entities.ranges import HtfRange, RejectionCandle

logger = logging.getLogger(__name__)


# ── Score ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RejectionScore:
    wick_penetration: float  # how far wick swept past the level  (0 → 1+)
    close_proximity: float  # signed: + = escaped back, − = still inside
    wick_ratio: float  # sweep wick / total_range
    total: float  # weighted composite

    @classmethod
    def compute(
        cls,
        wick_penetration: float,
        close_proximity: float,
        wick_ratio: float,
    ) -> RejectionScore:
        total = wick_penetration * 0.5 + wick_ratio * 0.3 + close_proximity * 0.2
        return cls(
            wick_penetration=wick_penetration,
            close_proximity=close_proximity,
            wick_ratio=wick_ratio,
            total=total,
        )


# ── CRT Detector ──────────────────────────────────────────────────────────────


class CrtDetector:
    """
    CRT (Candle Range Theory) entry detector — previous_candle mode only.

    Trigger condition: current candle vs previous candle's range:
      SELL (SHORT): wick sweeps ABOVE prev_candle.high, close comes back BELOW it.
      BUY  (LONG):  wick sweeps BELOW prev_candle.low,  close comes back ABOVE it.

    HTF containment rule (when htf_range is supplied):
      The trigger candle must have touched the HTF range price box,
      ensuring the sweep is happening AT the zone.
    """

    @staticmethod
    def _inside_htf(candle: Candle, htf_range: HtfRange) -> bool:
        return candle.low <= htf_range.range_high and candle.high >= htf_range.range_low

    @staticmethod
    def check(
        candle: Candle,
        prev_candle: Candle,
        direction: SignalDirection,
        htf_range: Optional[HtfRange] = None,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        if htf_range is not None and not CrtDetector._inside_htf(candle, htf_range):
            return None
        if direction == SignalDirection.SHORT:
            return CrtDetector._sell(candle, prev_candle.high)
        return CrtDetector._buy(candle, prev_candle.low)

    @staticmethod
    def _sell(
        candle: Candle,
        level: float,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        if candle.high <= level:
            return None
        if candle.close >= level:
            return None

        total_range = candle.total_range
        sweep_wick = candle.high - max(candle.open, candle.close)
        wick_ratio = sweep_wick / total_range if total_range > 1e-8 else 0.0

        zone_size = candle.high - level
        if zone_size < 1e-8:
            return None

        wick_penetration = zone_size / zone_size  # always 1.0 by definition
        close_proximity = (level - candle.close) / candle.total_range if total_range > 1e-8 else 0.0

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)

        logger.debug(
            "CRT SELL @ %s  score=%.3f  sweep=%.5f→%.5f  close=%.5f",
            candle.timestamp, score.total, level, candle.high, candle.close,
        )
        return (
            RejectionCandle(
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                timestamp=candle.timestamp,
                wick_ratio=wick_ratio,
                pattern=CandlePattern.CRT_SELL,
            ),
            score,
        )

    @staticmethod
    def _buy(
        candle: Candle,
        level: float,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        if candle.low >= level:
            return None
        if candle.close <= level:
            return None

        total_range = candle.total_range
        sweep_wick = min(candle.open, candle.close) - candle.low
        wick_ratio = sweep_wick / total_range if total_range > 1e-8 else 0.0

        zone_size = level - candle.low
        if zone_size < 1e-8:
            return None

        wick_penetration = zone_size / zone_size  # always 1.0 by definition
        close_proximity = (candle.close - level) / candle.total_range if total_range > 1e-8 else 0.0

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)

        logger.debug(
            "CRT BUY @ %s  score=%.3f  sweep=%.5f→%.5f  close=%.5f",
            candle.timestamp, score.total, level, candle.low, candle.close,
        )
        return (
            RejectionCandle(
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                timestamp=candle.timestamp,
                wick_ratio=wick_ratio,
                pattern=CandlePattern.CRT_BUY,
            ),
            score,
        )

    @staticmethod
    def find_most_recent(
        candles: list[Candle],
        direction: SignalDirection,
        htf_range: Optional[HtfRange] = None,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        """Scan candles in reverse, return the most recent qualifying CRT trigger."""
        for i in range(len(candles) - 1, 0, -1):
            result = CrtDetector.check(candles[i], candles[i - 1], direction, htf_range)
            if result:
                return result
        return None
