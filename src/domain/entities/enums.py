"""
domain/entities/enums.py — all trading domain enumerations.

Kept in one file so every layer imports from a single canonical source.
str-subclassing preserves JSON serialisability (enum.value == the string).
"""

from __future__ import annotations

from enum import Enum


class SignalDirection(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    PENDING     = "PENDING"
    TRIGGERED   = "TRIGGERED"
    TP1_HIT     = "TP1_HIT"
    TP2_HIT     = "TP2_HIT"
    SL_HIT      = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED     = "EXPIRED"


class SignalOutcome(str, Enum):
    WIN_FULL    = "WIN_FULL"
    BREAKEVEN   = "BREAKEVEN"
    LOSS        = "LOSS"
    INVALIDATED = "INVALIDATED"
    EXPIRED     = "EXPIRED"


class SignalEvent(str, Enum):
    SIGNAL_PENDING     = "signal.pending"
    SIGNAL_TRIGGERED   = "signal.triggered"
    SIGNAL_TP1_HIT     = "signal.tp1_hit"
    SIGNAL_TP2_HIT     = "signal.tp2_hit"
    SIGNAL_SL_HIT      = "signal.sl_hit"
    SIGNAL_INVALIDATED = "signal.invalidated"
    SIGNAL_EXPIRED     = "signal.expired"
    SIGNAL_UPDATED     = "signal.updated"


class BosDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class CandlePattern(str, Enum):
    SHOOTING_STAR = "SHOOTING_STAR"
    HAMMER        = "HAMMER"
    CRT_SELL      = "CRT_SELL"   # sweep above range_high, close back inside → sell
    CRT_BUY       = "CRT_BUY"   # sweep below range_low,  close back inside → buy


class EntryModel(str, Enum):
    """Controls which entry trigger(s) are active inside the rejection class."""
    CANDLE_PATTERN = "candle_pattern"   # classic HAMMER / SHOOTING_STAR only
    CRT            = "crt"             # CRT sweep-and-reverse only
    ALL            = "all"             # either model may trigger


class TrendBias(str, Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


# ── Convenience groupings (used by SessionMemory / backtest stats) ─────────────

WIN_OUTCOMES:    frozenset[str] = frozenset({SignalOutcome.WIN_FULL.value,
                                              SignalOutcome.BREAKEVEN.value})
CLOSED_OUTCOMES: frozenset[str] = frozenset({*WIN_OUTCOMES,
                                               SignalOutcome.LOSS.value})
VOID_OUTCOMES:   frozenset[str] = frozenset({SignalOutcome.INVALIDATED.value,
                                               SignalOutcome.EXPIRED.value})
