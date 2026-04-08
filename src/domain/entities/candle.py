"""
domain/entities/candle.py — raw market data atom.

No external dependencies. No config imports.
The `dt` property requires a tzinfo — callers supply it; the domain
does NOT reach into config.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(slots=True)
class Candle:
    timestamp: int    # UTC milliseconds
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float = 0.0

    # ── Derived geometry ──────────────────────────────────────────────────────

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    def dt(self, tz: ZoneInfo) -> datetime.datetime:
        """Localise the UTC-ms timestamp to the given timezone."""
        return datetime.datetime.fromtimestamp(self.timestamp / 1000, tz=tz)
