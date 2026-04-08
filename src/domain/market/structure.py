"""
domain/market/structure.py — HTF trend bias from BOS sequence.

Pure domain logic. Accepts a cfg pivot_bars parameter directly
rather than importing the config singleton.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domain.entities.candle import Candle
from domain.entities.enums import BosDirection, TrendBias
from domain.market.swings import detect_bos_events

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StructureResult:
    """Result of HTF market structure analysis."""

    bias:           TrendBias
    last_bos:       Optional[BosDirection] = None
    last_bos_ts:    Optional[int]          = None
    last_bos_level: Optional[float]        = None
    bos_count:      int                    = 0
    reason:         str                    = ""

    def allows(self, direction: str) -> bool:
        """True when the bias permits trading in the given direction."""
        return self.bias.value == direction


class MarketStructure:

    @staticmethod
    def detect(
        candles: list[Candle],
        pivot_bars: int = 1,
    ) -> StructureResult:
        return MarketStructure._run(candles, pivot_bars)

    @staticmethod
    def detect_at(
        candles: list[Candle],
        current_ts: int,
        pivot_bars: int = 1,
    ) -> StructureResult:
        return MarketStructure._run(
            [c for c in candles if c.timestamp <= current_ts],
            pivot_bars,
        )

    @staticmethod
    def _run(candles: list[Candle], pivot_bars: int) -> StructureResult:
        if len(candles) < 3:
            return StructureResult(
                TrendBias.NEUTRAL,
                reason=f"Too few candles ({len(candles)})",
            )

        bos_events = detect_bos_events(candles, pivot_bars=pivot_bars)
        if not bos_events:
            return StructureResult(TrendBias.NEUTRAL, reason="No BOS detected")

        last_ts, last_dir, last_level, _ = max(bos_events, key=lambda x: x[0])
        bias = TrendBias.LONG if last_dir == BosDirection.BULLISH else TrendBias.SHORT

        logger.debug(
            "MarketStructure: %d BOS events → bias=%s  last=%s @ %.5f",
            len(bos_events),
            bias.value,
            last_dir.value,
            last_level,
        )

        return StructureResult(
            bias           = bias,
            last_bos       = last_dir,
            last_bos_ts    = last_ts,
            last_bos_level = last_level,
            bos_count      = len(bos_events),
            reason         = f"Last {last_dir.value} BOS broke {last_level:.5f}",
        )
