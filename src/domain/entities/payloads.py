"""
domain/entities/payloads.py — typed WebSocket payload shapes.

TypedDicts provide static-analysis safety without runtime overhead.
They are the wire contract between the signal engine and WebSocket clients.
"""

from __future__ import annotations

from typing import Literal, TypedDict


class HtfRangePendingPayload(TypedDict):
    rangeHigh:      float
    rangeLow:       float
    bosDirection:   str       # "BULLISH" | "BEARISH"
    timestamp:      int
    htfCandleOpen:  int
    htfCandleClose: int
    brokenAt:       int
    tpLevel:        float


class LtfRangePendingPayload(TypedDict):
    rangeHigh: float
    rangeLow:  float
    slLevel:   float
    timestamp: int


class SignalPendingPayload(TypedDict):
    symbol:       str
    direction:    str
    status:       Literal["PENDING"]
    htfRange:     HtfRangePendingPayload
    ltfRange:     LtfRangePendingPayload
    pendingAt:    int
    ltfTimestamp: int
    htfInterval:  str
    ltfInterval:  str
