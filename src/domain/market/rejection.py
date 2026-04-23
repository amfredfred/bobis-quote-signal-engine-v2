"""
domain/market/rejection.py — rejection candle scoring and detection.

Pure domain logic — no config import, no external deps.
All quality gates are passed as explicit parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domain.entities.candle import Candle
from domain.entities.enums import CandlePattern, SignalDirection
from domain.entities.ranges import LtfRange, RejectionCandle
from domain.entities.ranges import HtfRange, LtfRange, RejectionCandle

logger = logging.getLogger(__name__)


# ── Score ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RejectionScore:
    wick_penetration: float  # how deep wick entered the zone  (0 → 1+)
    close_proximity: float  # signed: + = escaped, − = still inside
    wick_ratio: float  # wick / total_range
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


# ── Detector ─────────────────────────────────────────────────────────────────


class RejectionDetector:

    @staticmethod
    def check(
        candle: Candle,
        ltf_range: LtfRange,
        min_wick_ratio: float = 0.65,
        min_score: float = 0.0,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        if ltf_range.direction == SignalDirection.SHORT:
            return RejectionDetector._shooting_star(
                candle, ltf_range, min_wick_ratio, min_score
            )
        return RejectionDetector._hammer(candle, ltf_range, min_wick_ratio, min_score)

    @staticmethod
    def _shooting_star(
        candle: Candle,
        ltf_range: LtfRange,
        min_wick_ratio: float,
        min_score: float,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        total_range = candle.total_range
        if total_range < 1e-8:
            return None
        zone_size = ltf_range.range_high - ltf_range.range_low
        if zone_size < 1e-8:
            return None

        wick = candle.upper_wick
        wick_ratio = wick / total_range

        if candle.high < ltf_range.range_low:
            return None
        if wick_ratio < min_wick_ratio:
            return None

        wick_penetration = (candle.high - ltf_range.range_low) / zone_size
        close_proximity = (ltf_range.range_low - candle.close) / zone_size

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)
        if score.total < min_score:
            return None

        logger.debug(
            "SHOOTING STAR @ %s  score=%.3f  pen=%.2f  prox=%.2f  wr=%.2f",
            candle.timestamp,
            score.total,
            score.wick_penetration,
            score.close_proximity,
            score.wick_ratio,
        )
        return (
            RejectionCandle(
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                timestamp=candle.timestamp,
                wick_ratio=wick_ratio,
                pattern=CandlePattern.SHOOTING_STAR,
            ),
            score,
        )

    @staticmethod
    def _hammer(
        candle: Candle,
        ltf_range: LtfRange,
        min_wick_ratio: float,
        min_score: float,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        total_range = candle.total_range
        if total_range < 1e-8:
            return None
        zone_size = ltf_range.range_high - ltf_range.range_low
        if zone_size < 1e-8:
            return None

        wick = candle.lower_wick
        wick_ratio = wick / total_range

        if candle.low > ltf_range.range_high:
            return None
        if wick_ratio < min_wick_ratio:
            return None

        wick_penetration = (ltf_range.range_high - candle.low) / zone_size
        close_proximity = (candle.close - ltf_range.range_high) / zone_size

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)
        if score.total < min_score:
            return None

        logger.debug(
            "HAMMER @ %s  score=%.3f  pen=%.2f  prox=%.2f  wr=%.2f",
            candle.timestamp,
            score.total,
            score.wick_penetration,
            score.close_proximity,
            score.wick_ratio,
        )
        return (
            RejectionCandle(
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                timestamp=candle.timestamp,
                wick_ratio=wick_ratio,
                pattern=CandlePattern.HAMMER,
            ),
            score,
        )

    @staticmethod
    def find_most_recent(
        candles: list[Candle],
        ltf_range: LtfRange,
        min_wick_ratio: float = 0.65,
        min_score: float = 0.0,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        """Scan candles in reverse and return the first qualifying rejection."""
        for candle in reversed(candles):
            result = RejectionDetector.check(
                candle, ltf_range, min_wick_ratio, min_score
            )
            if result:
                return result
        return None

    @staticmethod
    def find_all_scored(
        candles: list[Candle],
        ltf_range: LtfRange,
        min_wick_ratio: float = 0.65,
        min_score: float = 0.0,
    ) -> list[tuple[RejectionCandle, RejectionScore]]:
        """Return all qualifying rejections sorted by score descending."""
        results = []
        for candle in candles:
            result = RejectionDetector.check(
                candle, ltf_range, min_wick_ratio, min_score
            )
            if result:
                results.append(result)
        return sorted(results, key=lambda x: x[1].total, reverse=True)


# ── CRT Detector ──────────────────────────────────────────────────────────────


class CrtDetector:
    """
    CRT (Candle Range Theory) entry detector.

    Trigger condition — current candle vs previous candle's range:
      SELL  (SHORT): wick sweeps ABOVE prev_candle.high, close comes back BELOW it.
      BUY   (LONG):  wick sweeps BELOW prev_candle.low,  close comes back ABOVE it.

    HTF containment rule (when htf_range is supplied):
      The trigger candle must have touched the HTF range price box.
      i.e. candle.low <= htf_range.range_high AND candle.high >= htf_range.range_low
      This ensures the sweep is happening AT the zone, giving momentum to push back.
    """

    @staticmethod
    def _inside_htf(candle: Candle, htf_range: HtfRange) -> bool:
        """True when the trigger candle has at least touched the HTF range box."""
        return candle.low <= htf_range.range_high and candle.high >= htf_range.range_low

    @staticmethod
    def check(
        candle: Candle,
        ltf_range: LtfRange,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        if ltf_range.direction == SignalDirection.SHORT:
            return CrtDetector._sell(candle, ltf_range)
        return CrtDetector._buy(candle, ltf_range)

    @staticmethod
    def _sell(
        candle: Candle,
        ltf_range: LtfRange,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        level = ltf_range.range_high
        zone_size = ltf_range.range_high - ltf_range.range_low
        if zone_size < 1e-8:
            return None

        # Must sweep above the level (wick) and close back below it
        if candle.high <= level:
            return None
        if candle.close >= level:
            return None

        total_range = candle.total_range
        sweep_wick = candle.high - max(candle.open, candle.close)
        wick_ratio = sweep_wick / total_range if total_range > 1e-8 else 0.0

        wick_penetration = (candle.high - level) / zone_size
        close_proximity = (level - candle.close) / zone_size

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)

        logger.debug(
            "CRT SELL @ %s  score=%.3f  sweep=%.5f→%.5f  close=%.5f",
            candle.timestamp,
            score.total,
            level,
            candle.high,
            candle.close,
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
        ltf_range: LtfRange,
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        level = ltf_range.range_low
        zone_size = ltf_range.range_high - ltf_range.range_low
        if zone_size < 1e-8:
            return None

        # Must sweep below the level (wick) and close back above it
        if candle.low >= level:
            return None
        if candle.close <= level:
            return None

        total_range = candle.total_range
        sweep_wick = min(candle.open, candle.close) - candle.low
        wick_ratio = sweep_wick / total_range if total_range > 1e-8 else 0.0

        wick_penetration = (level - candle.low) / zone_size
        close_proximity = (candle.close - level) / zone_size

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)

        logger.debug(
            "CRT BUY @ %s  score=%.3f  sweep=%.5f→%.5f  close=%.5f",
            candle.timestamp,
            score.total,
            level,
            candle.low,
            candle.close,
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
        ltf_range: LtfRange,
        htf_range: Optional[HtfRange] = None,   # ← NEW
    ) -> Optional[tuple[RejectionCandle, RejectionScore]]:
        if len(candles) < 2:
            return None

        for i in range(len(candles) - 1, 0, -1):
            current     = candles[i]
            prev_candle = candles[i - 1]

            # ── HTF containment gate ──────────────────────────────────────────
            if htf_range is not None and not CrtDetector._inside_htf(current, htf_range):
                continue

            prev_range = LtfRange(
                range_high=prev_candle.high,
                range_low=prev_candle.low,
                direction=ltf_range.direction,
                timestamp=prev_candle.timestamp,
            )
            result = CrtDetector.check(current, prev_range)
            if result:
                return result

        return None

    @staticmethod
    def find_all_scored(
        candles: list[Candle],
        ltf_range: LtfRange,
        htf_range: Optional[HtfRange] = None,   # ← NEW
    ) -> list[tuple[RejectionCandle, RejectionScore]]:
        if len(candles) < 2:
            return []

        results = []
        for i in range(1, len(candles)):
            current     = candles[i]
            prev_candle = candles[i - 1]

            # ── HTF containment gate ──────────────────────────────────────────
            if htf_range is not None and not CrtDetector._inside_htf(current, htf_range):
                continue

            prev_range = LtfRange(
                range_high=prev_candle.high,
                range_low=prev_candle.low,
                direction=ltf_range.direction,
                timestamp=prev_candle.timestamp,
            )
            result = CrtDetector.check(current, prev_range)
            if result:
                results.append(result)

        return sorted(results, key=lambda x: x[1].total, reverse=True)
