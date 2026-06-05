"""Shared trade-management math for signal lifecycle and backtests."""

from __future__ import annotations

from domain.entities.enums import SignalDirection


def pct_fraction(value: float) -> float:
    return float(value) / 100.0


def breakeven_buffer(
    *,
    risk_pips: float,
    spread_price_units: float,
    spread_multiplier: float,
    max_buffer_pct_of_risk: float,
) -> float:
    multiplier = max(spread_multiplier, 0.0)
    if multiplier == 0.0:
        return 0.0
    spread = max(spread_price_units, 0.0)
    risk_cap = risk_pips * pct_fraction(max(max_buffer_pct_of_risk, 0.0))
    return max(spread, min(spread * multiplier, risk_cap))


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
    risk_pips: float,
    spread_price_units: float,
    spread_multiplier: float,
    max_buffer_pct_of_risk: float,
) -> float:
    buffer = breakeven_buffer(
        risk_pips=risk_pips,
        spread_price_units=spread_price_units,
        spread_multiplier=spread_multiplier,
        max_buffer_pct_of_risk=max_buffer_pct_of_risk,
    )
    if direction == SignalDirection.LONG:
        return entry_price + buffer
    return entry_price - buffer


def protected_breakeven_rr(
    *,
    full_rr: float,
    tp1_trigger_pct: float,
    tp1_close_pct: float,
    risk_pips: float,
    spread_price_units: float,
    spread_multiplier: float,
    max_buffer_pct_of_risk: float,
) -> float:
    close_fraction = pct_fraction(tp1_close_pct)
    booked_rr = tp1_booked_rr(
        full_rr=full_rr,
        tp1_trigger_pct=tp1_trigger_pct,
        tp1_close_pct=tp1_close_pct,
    )
    buffer = breakeven_buffer(
        risk_pips=risk_pips,
        spread_price_units=spread_price_units,
        spread_multiplier=spread_multiplier,
        max_buffer_pct_of_risk=max_buffer_pct_of_risk,
    )
    buffer_rr = buffer / risk_pips if risk_pips > 0.0 else 0.0
    return booked_rr + (buffer_rr * (1.0 - close_fraction))


def tp2_weighted_rr(
    *,
    full_rr: float,
    tp1_trigger_pct: float,
    tp1_close_pct: float,
) -> float:
    close_fraction = pct_fraction(tp1_close_pct)
    tp1_rr = full_rr * pct_fraction(tp1_trigger_pct)
    return (tp1_rr * close_fraction) + (full_rr * (1.0 - close_fraction))
