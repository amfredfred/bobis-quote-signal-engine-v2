"""
domain/market/swings.py — pivot-based BOS detection and swing range finders.

Pure domain logic — no config import, no external deps.
All tuning parameters are passed as explicit arguments.
"""

from __future__ import annotations

import logging
from typing import Optional

from domain.entities.candle import Candle
from domain.entities.enums import BosDirection
from domain.entities.ranges import HtfRange

logger = logging.getLogger(__name__)


# ── Pivot helpers ─────────────────────────────────────────────────────────────

def _find_pivot_highs(
    candles: list[Candle], left: int, right: int
) -> list[Candle]:
    """Candles whose high is strictly higher than `left` before and `right` after."""
    pivots, n = [], len(candles)
    for i in range(left, n - right):
        c = candles[i]
        if all(candles[i - j].high < c.high for j in range(1, left + 1)) and \
           all(candles[i + j].high < c.high for j in range(1, right + 1)):
            pivots.append(c)
    return pivots


def _find_pivot_lows(
    candles: list[Candle], left: int, right: int
) -> list[Candle]:
    """Candles whose low is strictly lower than `left` before and `right` after."""
    pivots, n = [], len(candles)
    for i in range(left, n - right):
        c = candles[i]
        if all(candles[i - j].low > c.low for j in range(1, left + 1)) and \
           all(candles[i + j].low > c.low for j in range(1, right + 1)):
            pivots.append(c)
    return pivots


# ── BOS detector ─────────────────────────────────────────────────────────────

def detect_bos_events(
    candles: list[Candle],
    pivot_bars: int = 1,
) -> list[tuple[int, BosDirection, float, Candle]]:
    """
    Pivot-based Break of Structure detector.

    1. Confirm pivot highs/lows using `pivot_bars` left + right neighbours.
    2. Walk candles chronologically tracking the last confirmed pivots.
    3. BULLISH BOS: close > last pivot high  → zone candle = last pivot LOW.
       BEARISH BOS: close < last pivot low   → zone candle = last pivot HIGH.
    4. After BOS: reset both references so stale levels never carry forward.

    Returns list of (bos_timestamp, direction, broken_level, swing_candle).
    """
    pivot_high_ts: set[int] = {
        c.timestamp for c in _find_pivot_highs(candles, pivot_bars, pivot_bars)
    }
    pivot_low_ts: set[int] = {
        c.timestamp for c in _find_pivot_lows(candles, pivot_bars, pivot_bars)
    }

    events: list[tuple[int, BosDirection, float, Candle]] = []
    last_pivot_high: Optional[Candle] = None
    last_pivot_low:  Optional[Candle] = None

    for c in candles:
        if c.timestamp in pivot_high_ts:
            last_pivot_high = c
        if c.timestamp in pivot_low_ts:
            last_pivot_low = c

        if last_pivot_high is None or last_pivot_low is None:
            continue

        if c.timestamp > last_pivot_high.timestamp and c.close > last_pivot_high.high:
            events.append((c.timestamp, BosDirection.BULLISH, last_pivot_high.high, last_pivot_low))
            last_pivot_high = last_pivot_low = None

        elif c.timestamp > last_pivot_low.timestamp and c.close < last_pivot_low.low:
            events.append((c.timestamp, BosDirection.BEARISH, last_pivot_low.low, last_pivot_high))
            last_pivot_high = last_pivot_low = None

    return events


# ── SwingDetector ─────────────────────────────────────────────────────────────

# ── Displacement detector ─────────────────────────────────────────────────────

def detect_displacement(
    candles: list[Candle],
    bos_ts: int,
    atr_period: int = 10,
    atr_mult: float = 1.2,
) -> bool:
    """
    Return True when the BOS candle shows genuine displacement (impulse).

    Displacement is defined as: the BOS candle's body size (|close - open|)
    must be >= atr_mult × the average body size of the prior ``atr_period``
    candles.

    A small or doji-like BOS candle signals a ranging market grinding through
    a level rather than an impulsive break — those zones are suppressed.

    Parameters
    ──────────
    candles    — HTF candle list (chronological).
    bos_ts     — timestamp of the BOS candle (HtfRange.broken_at).
    atr_period — how many candles before the BOS to use for the avg body.
    atr_mult   — required multiple of avg body (1.2 = 20 % above average).
    """
    bos_idx: Optional[int] = None
    for i, c in enumerate(candles):
        if c.timestamp == bos_ts:
            bos_idx = i
            break

    if bos_idx is None or bos_idx == 0:
        # BOS candle not found or is the very first — can't measure; allow.
        logger.debug("detect_displacement: BOS candle ts=%d not found — allowing", bos_ts)
        return True

    bos_candle = candles[bos_idx]
    bos_body   = abs(bos_candle.close - bos_candle.open)

    start  = max(0, bos_idx - atr_period)
    prior  = candles[start:bos_idx]
    if not prior:
        return True  # not enough history — allow

    avg_body = sum(abs(c.close - c.open) for c in prior) / len(prior)
    if avg_body < 1e-10:
        return True  # degenerate identical candles — allow

    displaced = bos_body >= atr_mult * avg_body
    logger.debug(
        "detect_displacement: bos_ts=%d  body=%.6f  avg=%.6f  mult=%.2f → %s",
        bos_ts, bos_body, avg_body, atr_mult, "DISPLACED" if displaced else "RANGING",
    )
    return displaced


class SwingDetector:

    @staticmethod
    def find_htf_ranges(
        candles: list[Candle],
        pivot_bars: int       = 1,
        htf_interval_ms: int  = 3_600_000,
        max_zones_per_dir: int = 3,
    ) -> list[HtfRange]:
        """
        Return fresh, untested HTF supply/demand zones.

        Parameters
        ──────────
        pivot_bars        — neighbour count for pivot confirmation (default 1).
        htf_interval_ms   — one HTF bar in milliseconds (for htf_candle_close).
        max_zones_per_dir — keep only the N most recent zones per direction.
        """
        if len(candles) < 3:
            return []

        fresh_zones:   list[HtfRange] = []
        seen_bearish:  set[int]       = set()
        seen_bullish:  set[int]       = set()

        for bos_ts, direction, broken_level, swing_candle in detect_bos_events(
            candles, pivot_bars=pivot_bars
        ):
            swing_ts = swing_candle.timestamp

            if direction == BosDirection.BEARISH:
                if swing_ts in seen_bearish:
                    continue
                seen_bearish.add(swing_ts)
                zone = HtfRange(
                    range_high       = swing_candle.high,
                    range_low        = swing_candle.low,
                    bos_direction    = BosDirection.BEARISH,
                    timestamp        = swing_ts,
                    broken_at        = bos_ts,
                    htf_candle_open  = swing_ts,
                    htf_candle_close = swing_ts + htf_interval_ms,
                )
                taken_out = any(
                    c.timestamp > bos_ts and c.close > zone.range_high
                    for c in candles
                )
                if taken_out:
                    logger.debug("BEARISH zone [%.5f,%.5f] taken out", zone.range_low, zone.range_high)
                    continue
                tp_candidates = [c for c in candles if c.timestamp > bos_ts and c.low < zone.range_low]
                zone.tp_level = (
                    min(tp_candidates, key=lambda c: c.low).low
                    if tp_candidates else zone.range_low
                )
                fresh_zones.append(zone)

            else:  # BULLISH
                if swing_ts in seen_bullish:
                    continue
                seen_bullish.add(swing_ts)
                zone = HtfRange(
                    range_high       = swing_candle.high,
                    range_low        = swing_candle.low,
                    bos_direction    = BosDirection.BULLISH,
                    timestamp        = swing_ts,
                    broken_at        = bos_ts,
                    htf_candle_open  = swing_ts,
                    htf_candle_close = swing_ts + htf_interval_ms,
                )
                taken_out = any(
                    c.timestamp > bos_ts and c.close < zone.range_low
                    for c in candles
                )
                if taken_out:
                    logger.debug("BULLISH zone [%.5f,%.5f] taken out", zone.range_low, zone.range_high)
                    continue
                tp_candidates = [c for c in candles if c.timestamp > bos_ts and c.high > zone.range_high]
                zone.tp_level = (
                    max(tp_candidates, key=lambda c: c.high).high
                    if tp_candidates else zone.range_high
                )
                fresh_zones.append(zone)

        bearish = sorted(
            [z for z in fresh_zones if z.bos_direction == BosDirection.BEARISH],
            key=lambda z: z.timestamp, reverse=True,
        )[:max_zones_per_dir]

        bullish = sorted(
            [z for z in fresh_zones if z.bos_direction == BosDirection.BULLISH],
            key=lambda z: z.timestamp, reverse=True,
        )[:max_zones_per_dir]

        result = bearish + bullish
        logger.info(
            "Found %d fresh untested HTF zones (%d SHORT, %d LONG) — "
            "%d taken out of %d total BOS events",
            len(result), len(bearish), len(bullish),
            (len(seen_bearish) - len(bearish)) + (len(seen_bullish) - len(bullish)),
            len(seen_bearish) + len(seen_bullish),
        )
        return result

