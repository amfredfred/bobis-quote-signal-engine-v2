"""
domain/signals/entry.py — shared entry model dispatcher.

Extracted from MultiPairBacktester and SignalService so that the entry
detection logic lives in exactly one place.  Both callers import
find_entry() and pass their local config values as explicit arguments.

Entry models
────────────
CANDLE_PATTERN
  Uses candles_entering_ltf() — price must leave the zone first, then
  wick back in and close back out.  Fires later, wider SL.

CRT
  Uses ltf_zone directly — fires the first time a candle sweeps the LTF
  range boundary and closes back inside before price ever leaves.
  Earlier entry, tighter SL.

ALL
  Both detectors run on their respective inputs.
  Most-recent rejection timestamp wins.
"""

from __future__ import annotations

from typing import Optional

from domain.entities.candle import Candle
from domain.entities.ranges import HtfRange, LtfRange
from domain.market.rejection import CrtDetector, RejectionDetector
from domain.market.swings import SwingDetector


def find_entry(
    ltf_zone: list[Candle],
    ltf_range: LtfRange,
    htf_range: HtfRange,
    entry_model: str,
    min_wick_ratio: float,
) -> Optional[tuple]:
    """
    Route to the correct entry detector(s) based on *entry_model*.

    Parameters
    ──────────
    ltf_zone        — LTF candles from zone_start to current tick (already sliced).
    ltf_range       — The LTF supply/demand range found inside the HTF zone.
    htf_range       — The HTF BOS-confirmed range.
    entry_model     — One of 'candle_pattern', 'crt', or 'all'.
    min_wick_ratio  — Minimum wick-to-range ratio for candle_pattern detection.

    Returns (RejectionCandle, RejectionScore) or None.
    """
    candidates: list[tuple] = []

    if entry_model in ("candle_pattern", "all"):
        entries = SwingDetector.candles_entering_ltf(ltf_zone, ltf_range, htf_range)
        if entries:
            result = RejectionDetector.find_most_recent(
                entries,
                ltf_range,
                min_wick_ratio=min_wick_ratio,
            )
            if result:
                candidates.append(result)

    if entry_model in ("crt", "all"):
        result = CrtDetector.find_most_recent(ltf_zone, ltf_range, htf_range)
        if result:
            candidates.append(result)

    if not candidates:
        return None
    return max(candidates, key=lambda r: r[0].timestamp)
