"""Shared trade-management math for signal lifecycle and backtests."""

from __future__ import annotations

from domain.entities.enums import SignalDirection


def pct_fraction(value: float) -> float:
    return float(value) / 100.0


def tp1_level(
    *,
    direction: SignalDirection,
    entry_price: float,
    tp2: float,
    tp1_trigger_pct: float,
) -> float:
    fraction = pct_fraction(tp1_trigger_pct)
    if direction == SignalDirection.LONG:
        return entry_price + abs(tp2 - entry_price) * fraction
    return entry_price - abs(tp2 - entry_price) * fraction


def tp1_booked_rr(
    *,
    full_rr: float,
    tp1_trigger_pct: float,
    tp1_close_pct: float,
) -> float:
    return full_rr * pct_fraction(tp1_trigger_pct) * pct_fraction(tp1_close_pct)


def breakeven_price(
    *,
    direction: SignalDirection,
    entry_price: float,
) -> float:
    return entry_price


def protected_breakeven_rr(
    *,
    full_rr: float,
    tp1_trigger_pct: float,
    tp1_close_pct: float,
) -> float:
    return tp1_booked_rr(
        full_rr=full_rr,
        tp1_trigger_pct=tp1_trigger_pct,
        tp1_close_pct=tp1_close_pct,
    )


def tp2_weighted_rr(
    *,
    full_rr: float,
    tp1_trigger_pct: float,
    tp1_close_pct: float,
) -> float:
    close_fraction = pct_fraction(tp1_close_pct)
    tp1_rr = full_rr * pct_fraction(tp1_trigger_pct)
    return (tp1_rr * close_fraction) + (full_rr * (1.0 - close_fraction))
