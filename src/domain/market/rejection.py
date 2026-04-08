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

logger = logging.getLogger(__name__)


# ── Score ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RejectionScore:
    wick_penetration: float   # how deep wick entered the zone  (0 → 1+)
    close_proximity:  float   # signed: + = escaped, − = still inside
    wick_ratio:       float   # wick / total_range
    total:            float   # weighted composite

    @classmethod
    def compute(
        cls,
        wick_penetration: float,
        close_proximity: float,
        wick_ratio: float,
    ) -> RejectionScore:
        total = wick_penetration * 0.5 + wick_ratio * 0.3 + close_proximity * 0.2
        return cls(
            wick_penetration = wick_penetration,
            close_proximity  = close_proximity,
            wick_ratio       = wick_ratio,
            total            = total,
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
            return RejectionDetector._shooting_star(candle, ltf_range, min_wick_ratio, min_score)
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

        wick       = candle.upper_wick
        wick_ratio = wick / total_range

        if candle.high < ltf_range.range_low:
            return None
        if wick_ratio < min_wick_ratio:
            return None

        wick_penetration = (candle.high - ltf_range.range_low) / zone_size
        close_proximity  = (ltf_range.range_low - candle.close) / zone_size

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)
        if score.total < min_score:
            return None

        logger.debug(
            "SHOOTING STAR @ %s  score=%.3f  pen=%.2f  prox=%.2f  wr=%.2f",
            candle.timestamp, score.total,
            score.wick_penetration, score.close_proximity, score.wick_ratio,
        )
        return (
            RejectionCandle(
                open=candle.open, high=candle.high, low=candle.low,
                close=candle.close, timestamp=candle.timestamp,
                wick_ratio=wick_ratio, pattern=CandlePattern.SHOOTING_STAR,
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

        wick       = candle.lower_wick
        wick_ratio = wick / total_range

        if candle.low > ltf_range.range_high:
            return None
        if wick_ratio < min_wick_ratio:
            return None

        wick_penetration = (ltf_range.range_high - candle.low) / zone_size
        close_proximity  = (candle.close - ltf_range.range_high) / zone_size

        score = RejectionScore.compute(wick_penetration, close_proximity, wick_ratio)
        if score.total < min_score:
            return None

        logger.debug(
            "HAMMER @ %s  score=%.3f  pen=%.2f  prox=%.2f  wr=%.2f",
            candle.timestamp, score.total,
            score.wick_penetration, score.close_proximity, score.wick_ratio,
        )
        return (
            RejectionCandle(
                open=candle.open, high=candle.high, low=candle.low,
                close=candle.close, timestamp=candle.timestamp,
                wick_ratio=wick_ratio, pattern=CandlePattern.HAMMER,
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
            result = RejectionDetector.check(candle, ltf_range, min_wick_ratio, min_score)
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
            result = RejectionDetector.check(candle, ltf_range, min_wick_ratio, min_score)
            if result:
                results.append(result)
        return sorted(results, key=lambda x: x[1].total, reverse=True)
