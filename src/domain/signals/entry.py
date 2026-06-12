"""
domain/signals/entry.py — CRT entry detector dispatcher.

Extracted from MultiPairBacktester and SignalService so that entry
detection lives in exactly one place.
"""

from __future__ import annotations

from typing import Optional

from domain.entities.candle import Candle
from domain.entities.enums import SignalDirection
from domain.entities.ranges import HtfRange
from domain.market.rejection import CrtDetector


def find_entry(
    ltf_zone: list[Candle],
    direction: SignalDirection,
    htf_range: HtfRange,
) -> Optional[tuple]:
    """
    Find the most recent CRT entry in ltf_zone for the given direction.

    Parameters
    ──────────
    ltf_zone  — LTF candles from zone_start to current tick (already sliced).
    direction — LONG or SHORT (derived from htf_range.signal_direction).
    htf_range — The HTF BOS-confirmed range (used for containment check).

    Returns (RejectionCandle, RejectionScore) or None.
    """
    return CrtDetector.find_most_recent(ltf_zone, direction, htf_range)
